"""Module 4 -- the Streamlit front end.

The page consumes a plan produced by any adapter. Because every backend hands
back the same ``networkx.DiGraph``, none of the rendering below branches on
CloudFormation or Terraform; the only backend-specific thing on the page is an
honesty banner explaining what Argus is and is not doing for that backend.

Launch it with ``argus dashboard``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

import streamlit as st

# Streamlit executes this file as a top-level script, so the package it lives in
# is not importable by relative import. Put the repository root on the path and
# import absolutely, which works whether or not Argus is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argus.adapters import BACKENDS, AdapterError, build_adapter  # noqa: E402
from argus.adapters.base import UnitStatus  # noqa: E402
from argus.aws import AwsSettings  # noqa: E402
from argus.config import ConfigError, load_config  # noqa: E402
from argus.explainer import (  # noqa: E402
    DEFAULT_MODEL,
    Explainer,
    ExplainerError,
    render_apply_plan,
)
from argus.optimizer import (  # noqa: E402
    DEFAULT_PERIOD_DAYS,
    RESOURCE_KINDS,
    Optimizer,
    OptimizerError,
)
from argus.orchestrator import STRATEGIES, Orchestrator, PlanError  # noqa: E402

#: Node fill per status. Deliberately readable in both Streamlit themes.
STATUS_STYLE = {
    UnitStatus.PENDING: ("#e8eaed", "#5f6368"),
    UnitStatus.IN_PROGRESS: ("#fde293", "#b06000"),
    UnitStatus.COMPLETE: ("#ceead6", "#137333"),
    UnitStatus.FAILED: ("#fad2cf", "#a50e0e"),
    UnitStatus.SKIPPED: ("#e9d2fd", "#6b21a8"),
    UnitStatus.UNKNOWN: ("#f1f3f4", "#5f6368"),
}

HIGHLIGHT = "#b06000"


# ---------------------------------------------------------------------------
# Graph rendering
# ---------------------------------------------------------------------------


def build_dot(plan, statuses: dict[str, UnitStatus]) -> str:
    """Render the plan as DOT, coloured by status and grouped by wave.

    Edges pointing at a unit that is currently deploying are thickened, so the
    dependencies being satisfied right now stand out from the rest.
    """
    lines = [
        "digraph argus {",
        "  rankdir=LR;",
        "  bgcolor=transparent;",
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];',
        '  edge [color="#9aa0a6", fontname="Helvetica", fontsize=9];',
    ]

    for index, wave in enumerate(plan.waves):
        lines.append("  subgraph cluster_wave_" + str(index) + " {")
        lines.append('    label="wave ' + str(index + 1) + '";')
        lines.append('    style=dashed; color="#dadce0"; fontsize=10; fontcolor="#5f6368";')
        for name in wave:
            status = statuses.get(name, UnitStatus.PENDING)
            fill, border = STATUS_STYLE.get(status, STATUS_STYLE[UnitStatus.UNKNOWN])
            lines.append(
                '    "' + name + '" [fillcolor="' + fill + '", color="' + border
                + '", fontcolor="' + border + '"];'
            )
        lines.append("  }")

    for source, target, data in plan.graph.edges(data=True):
        declared = data.get("via") == ["argus.yaml"]
        active = statuses.get(target) == UnitStatus.IN_PROGRESS
        attributes = []
        if active:
            attributes.append('color="' + HIGHLIGHT + '"')
            attributes.append("penwidth=2.5")
        if declared:
            attributes.append("style=dashed")
            attributes.append('label="argus.yaml"')
        suffix = " [" + ", ".join(attributes) + "]" if attributes else ""
        lines.append('  "' + source + '" -> "' + target + '"' + suffix + ";")

    lines.append("}")
    return "\n".join(lines)


def status_legend() -> None:
    columns = st.columns(len(STATUS_STYLE))
    for column, (status, (fill, border)) in zip(columns, STATUS_STYLE.items()):
        column.markdown(
            '<div style="border-left:6px solid ' + border + ";background:" + fill
            + ';padding:2px 8px;border-radius:4px;color:#202124;font-size:12px">'
            + status.value + "</div>",
            unsafe_allow_html=True,
        )


def single_module_graph(adapter, plan):
    """Terraform's own resource graph, when there is exactly one root module.

    Returned only in that case: with several modules the unit-level graph is
    the one Argus actually orchestrates, and that is what belongs on screen.
    """
    if len(plan.units) != 1 or not plan.units[0].metadata.get("single_module"):
        return None
    graph = adapter.inspect_unit(plan.units[0])
    return graph if graph is not None and graph.number_of_nodes() else None


#: Fill per resource kind in a backend's internal graph.
KIND_STYLE = {
    "resource": ("#d2e3fc", "#1967d2"),
    "data": ("#e6f4ea", "#137333"),
    "variable": ("#f1f3f4", "#5f6368"),
    "local": ("#f1f3f4", "#5f6368"),
    "output": ("#fef7e0", "#b06000"),
    "module": ("#e9d2fd", "#6b21a8"),
}


def build_resource_dot(graph) -> str:
    """Render a backend-internal graph. No status: Argus does not drive these."""
    lines = [
        "digraph resources {",
        "  rankdir=LR;",
        "  bgcolor=transparent;",
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10];',
        '  edge [color="#9aa0a6"];',
    ]
    for name, data in graph.nodes(data=True):
        fill, border = KIND_STYLE.get(data.get("kind", "resource"), KIND_STYLE["resource"])
        lines.append(
            '  "' + name + '" [fillcolor="' + fill + '", color="' + border
            + '", fontcolor="' + border + '"];'
        )
    for source, target in graph.edges():
        lines.append('  "' + source + '" -> "' + target + '";')
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backend honesty banner
# ---------------------------------------------------------------------------


def backend_banner(backend: str, plan) -> None:
    """Say plainly what Argus contributes for this backend.

    Terraform already builds a dependency graph and parallelises inside a
    single state. Letting the page imply otherwise would be the easiest way to
    oversell the tool, so the single-module case says so explicitly.
    """
    if backend == "cloudformation":
        st.info(
            "**CloudFormation.** CloudFormation has no native way to deploy several "
            "independent stacks in parallel while inferring their order from "
            "exports and imports. The waves below, and the parallel execution, are "
            "Argus's contribution."
        )
        return

    single_module = len(plan.units) == 1
    if single_module:
        source = plan.units[0].metadata.get("graph_source", "terraform graph")
        st.warning(
            "**Terraform, single root module.** This graph comes from `" + source + "`, and "
            "`terraform apply` already parallelises independent resources inside this "
            "state on its own. Argus is **not** orchestrating that work here -- it is "
            "rendering Terraform's own graph more legibly. Argus starts contributing "
            "ordering when a project has several root modules with separate states, "
            "declared under `modules:` in `argus.yaml`."
        )
    else:
        st.info(
            "**Terraform, " + str(len(plan.units)) + " root modules.** Terraform has no "
            "native ordering between separate states; the edges below come from the "
            "`depends_on` you declared in `argus.yaml`. Argus sequences the modules and "
            "lets `terraform apply` parallelise inside each one. Terragrunt solves this "
            "same problem and is more mature at it -- Argus's angle is one graph and one "
            "UI across both backends."
        )


# ---------------------------------------------------------------------------
# Tab 1 -- deployment
# ---------------------------------------------------------------------------


def render_deployment_tab(backend: str, path: str, endpoint_url: str = "") -> None:
    st.subheader("Dependency graph and deployment")

    try:
        config = load_config(path)
        config.backend = backend
        if endpoint_url:
            config.endpoint_url = endpoint_url
        adapter = build_adapter(backend, config)
        orchestrator = Orchestrator(
            adapter, max_parallel=config.max_parallel, extra_dependencies=config.depends_on
        )
        plan = orchestrator.plan(path)
    except (AdapterError, PlanError, ConfigError) as exc:
        st.error(str(exc))
        return

    backend_banner(backend, plan)

    columns = st.columns(4)
    columns[0].metric("Units", plan.unit_count)
    columns[1].metric("Dependencies", plan.graph.number_of_edges())
    columns[2].metric("Waves", len(plan.waves))
    columns[3].metric(
        "Critical path",
        len(plan.critical_path),
        help="The longest dependency chain. No scheduler can be shorter than this, "
        "whatever the level of parallelism, so it is the structural limit of the project.",
    )
    st.caption(
        "critical path: " + " \u2192 ".join(plan.critical_path)
        + "  \u00b7  peak concurrency: " + str(plan.max_concurrency)
    )

    if plan.unresolved:
        st.warning(
            "Some requirements are provided by nothing in this project, so Argus cannot "
            "order them. They must already exist, or be declared under `depends_on` in "
            "`argus.yaml`:\n\n"
            + "\n".join(
                "- `" + unit + "` needs " + ", ".join("`" + name + "`" for name in names)
                for unit, names in sorted(plan.unresolved.items())
            )
        )

    statuses = st.session_state.get("statuses", {})

    inner = single_module_graph(adapter, plan)
    if inner is not None:
        st.caption(
            "Rendered from " + plan.units[0].metadata.get("graph_source", "the backend")
            + " -- " + str(inner.number_of_nodes()) + " resources, "
            + str(inner.number_of_edges()) + " dependencies. Terraform schedules these itself."
        )
        st.graphviz_chart(build_resource_dot(inner), width="stretch")
        graph_slot = st.empty()
    else:
        status_legend()
        graph_slot = st.empty()
        graph_slot.graphviz_chart(build_dot(plan, statuses), width="stretch")

    st.divider()
    st.markdown("**Run**")

    controls = st.columns([2, 2])
    dry_run = controls[0].toggle(
        "Dry run (no cloud call, simulated timings)",
        value=True,
        help="Leave this on to watch the graph animate without touching an AWS account.",
    )
    strategy = controls[1].selectbox(
        "Strategy",
        STRATEGIES,
        help="rolling starts each unit as soon as its own dependencies finish. "
        "waves adds a barrier between generations -- slower, but it is the baseline "
        "rolling is measured against.",
    )
    orchestrator.strategy = strategy

    confirmed = True
    if not dry_run:
        st.error(
            "A real run deploys " + str(plan.unit_count) + " unit(s) to AWS and cannot be "
            "undone from this page."
        )
        confirmed = (
            st.text_input("Type the backend name to confirm", placeholder=backend).strip() == backend
        )

    if st.button("Deploy", type="primary", disabled=not confirmed):
        run_deployment(orchestrator, plan, dry_run, graph_slot, strategy)

    report = st.session_state.get("report")
    if report is not None:
        render_timing(report)


def run_deployment(orchestrator, plan, dry_run: bool, graph_slot, strategy: str) -> None:
    """Deploy in a worker thread while the main thread repaints the graph."""
    statuses: dict[str, UnitStatus] = {unit.name: UnitStatus.PENDING for unit in plan.units}
    lock = threading.Lock()
    holder: dict[str, Any] = {}

    def on_status(name: str, status: UnitStatus) -> None:
        with lock:
            statuses[name] = status

    def worker() -> None:
        try:
            holder["report"] = orchestrator.run(
                plan, dry_run=dry_run, on_status=on_status, strategy=strategy
            )
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    progress = st.progress(0.0)
    while thread.is_alive():
        with lock:
            snapshot = dict(statuses)
        done = sum(1 for status in snapshot.values() if status.is_terminal)
        progress.progress(done / max(1, len(snapshot)))
        graph_slot.graphviz_chart(build_dot(plan, snapshot), width="stretch")
        time.sleep(0.4)
    thread.join()

    with lock:
        snapshot = dict(statuses)
    progress.progress(1.0)
    graph_slot.graphviz_chart(build_dot(plan, snapshot), width="stretch")

    st.session_state["statuses"] = snapshot
    if "error" in holder:
        st.error(str(holder["error"]))
        return
    st.session_state["report"] = holder.get("report")


def render_timing(report) -> None:
    st.divider()
    label = "Simulated timing" if report.dry_run else "Measured timing"
    st.markdown("**" + label + "**")
    if report.dry_run:
        st.caption(
            "These durations come from the dry run's simulated deployments, not from AWS. "
            "They demonstrate the wave structure; they do not predict a real deployment."
        )

    columns = st.columns(4)
    columns[0].metric("Wall clock", "%.1f s" % report.wall_clock, help="strategy: " + report.strategy)
    columns[1].metric("Sequential equivalent", "%.1f s" % report.sequential_estimate)
    columns[2].metric(
        "Critical path floor",
        "%.1f s" % report.critical_path_duration,
        help="The floor: the longest chain weighted by the measured durations. "
        "No scheduler could have been faster than this.",
    )
    columns[3].metric(
        "Efficiency",
        "%.0f%%" % (report.efficiency * 100),
        delta="x%.2f vs sequential" % report.speedup,
        help="Wall clock against the floor. Below 100% means time was spent waiting "
        "on something other than a real dependency.",
    )
    st.caption("critical path: " + " \u2192 ".join(report.critical_path))

    st.dataframe(
        [
            {
                "unit": result.unit,
                "status": result.status.value,
                "duration (s)": round(result.duration, 2),
                "detail": result.detail,
            }
            for result in report.results
        ],
        width="stretch",
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Tab 2 -- optimization
# ---------------------------------------------------------------------------


def render_optimization_tab(region: str, endpoint_url: str = "") -> None:
    st.subheader("Rightsizing analysis")
    st.caption(
        "Reads CloudWatch and AWS Compute Optimizer. Nothing on this tab changes any "
        "resource."
    )

    columns = st.columns([2, 2, 1])
    resource_type = columns[0].selectbox("Resource type", sorted(RESOURCE_KINDS), index=1)
    identifier = columns[1].text_input(
        "Identifier", placeholder="taskmanager-dev-service", key="optimize_id"
    )
    days = columns[2].number_input("Days", min_value=1, max_value=90, value=DEFAULT_PERIOD_DAYS)

    cluster = None
    if resource_type == "ecs-service":
        cluster = st.text_input(
            "ECS cluster (optional)", help="Discovered automatically when left empty."
        ) or None

    if st.button("Analyse", type="primary", disabled=not identifier):
        with st.spinner("Reading CloudWatch..."):
            try:
                settings = AwsSettings(
                    region=region or None, endpoint_url=endpoint_url or None
                )
                report = Optimizer(settings=settings, period_days=int(days)).analyse(
                    identifier, resource_type=resource_type, cluster=cluster
                )
            except OptimizerError as exc:
                st.error(str(exc))
                return
        st.session_state["optimization"] = report.to_dict()

    payload = st.session_state.get("optimization")
    if payload is None:
        st.info("Run an analysis to see metrics and verdicts here.")
        return

    render_optimization(payload)


def render_optimization(payload: dict[str, Any]) -> None:
    resource = payload["resource"]
    st.markdown(
        "**" + resource["type"] + " `" + resource["id"] + "`** in " + resource["region"]
        + " over the last " + str(resource["period_days"]) + " days"
    )

    rows = [
        {
            "metric": name,
            "p95": None if sample["p95"] is None else round(sample["p95"], 2),
            "average": None if sample["average"] is None else round(sample["average"], 2),
            "maximum": None if sample["maximum"] is None else round(sample["maximum"], 2),
            "unit": sample["unit"],
            "datapoints": sample["datapoints"],
        }
        for name, sample in payload["metrics"].items()
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    left, right = st.columns(2)
    with left:
        st.markdown("**Argus rule**")
        st.metric("Finding", payload["argus"]["finding"])
        st.caption(payload["argus"]["rationale"])
        if payload["argus"]["proposed"]:
            st.json({"current": payload["argus"]["current"], "proposed": payload["argus"]["proposed"]})

    with right:
        st.markdown("**AWS Compute Optimizer**")
        if payload["aws"] is None:
            st.metric("Finding", "no recommendation")
            st.caption(
                "Compute Optimizer needs about 14 days of metrics before it says anything."
            )
        else:
            st.metric("Finding", payload["aws"]["finding"])
            st.caption(payload["aws"]["rationale"])
            if payload["aws"]["proposed"]:
                st.json(payload["aws"]["proposed"])

    if payload["agreement"].startswith("disagree"):
        st.warning(payload["agreement"])
    else:
        st.success(payload["agreement"])

    for note in payload.get("notes", []):
        st.caption("note: " + note)


# ---------------------------------------------------------------------------
# Tab 3 -- explanation
# ---------------------------------------------------------------------------


def render_explanation_tab() -> None:
    st.subheader("Explanation")

    payload = st.session_state.get("optimization")
    if payload is None:
        st.info("Run an analysis on the Optimization tab first.")
        return

    model = st.text_input("Model", value=DEFAULT_MODEL)
    question = st.text_input("An extra question (optional)", placeholder="Is 512 CPU enough?")

    if st.button("Explain", type="primary"):
        with st.spinner("Asking Claude..."):
            try:
                explanation = Explainer(model=model).explain(payload, question=question or None)
            except ExplainerError as exc:
                st.error(str(exc))
                return
        st.session_state["explanation"] = explanation

    explanation = st.session_state.get("explanation")
    if explanation is None:
        return

    st.markdown(explanation.text)
    st.caption(
        "explained by " + explanation.model + " -- "
        + str(explanation.input_tokens or 0) + " in / " + str(explanation.output_tokens or 0)
        + " out tokens"
    )

    commands = render_apply_plan(payload)
    if not commands:
        return

    st.divider()
    st.markdown("**Applying this**")
    st.caption(
        "Argus has no code path that resizes a resource. Review these commands and run "
        "them yourself."
    )
    st.code("\n".join(commands), language="bash")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Argus", page_icon="👁", layout="wide")
    st.title("Argus")
    st.caption(
        "Deploy, visualise and optimise AWS infrastructure by discovering its real "
        "dependencies rather than following a fixed order."
    )

    header = st.columns([1, 3, 1, 1])
    backend = header[0].selectbox("Backend", BACKENDS)
    path = header[1].text_input("Project path", value="infrastructure/cloudformation")
    region = header[2].text_input("Region", placeholder="eu-west-3")
    endpoint_url = header[3].text_input(
        "Endpoint",
        placeholder="http://localhost:4566",
        help="Point this at LocalStack to run everything without a cloud account. "
        "Leave it empty to talk to AWS.",
    ).strip()

    if endpoint_url:
        st.caption(
            "\u26a1 Talking to " + endpoint_url + ", not to AWS. Nothing here touches a real account."
        )

    # Changing the target invalidates statuses recorded for the previous one.
    target = (backend, path, endpoint_url)
    if st.session_state.get("target") != target:
        st.session_state["target"] = target
        st.session_state.pop("statuses", None)
        st.session_state.pop("report", None)

    deployment, optimization, explanation = st.tabs(
        ["Deployment", "Optimization", "Explanation"]
    )

    with deployment:
        render_deployment_tab(backend, path, endpoint_url)
    with optimization:
        render_optimization_tab(region, endpoint_url)
    with explanation:
        render_explanation_tab()


if __name__ == "__main__":
    main()
