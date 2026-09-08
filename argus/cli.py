"""The ``argus`` command line."""

from __future__ import annotations

import json
import os
import sys

import click

from . import __version__
from .adapters import BACKENDS, AdapterError, build_adapter
from .adapters.base import UnitStatus
from .aws import AwsSettings
from .config import ArgusConfig, ConfigError, load_config, parse_config_file
from .explainer import DEFAULT_MODEL, Explainer, ExplainerError, render_apply_plan
from .optimizer import DEFAULT_PERIOD_DAYS, RESOURCE_KINDS, Optimizer, OptimizerError
from .orchestrator import STRATEGIES, Orchestrator, PlanError

_STATUS_COLOURS = {
    UnitStatus.PENDING: "white",
    UnitStatus.IN_PROGRESS: "yellow",
    UnitStatus.COMPLETE: "green",
    UnitStatus.FAILED: "red",
    UnitStatus.SKIPPED: "magenta",
    UnitStatus.UNKNOWN: "white",
}


def _load(path: str, config_path: str | None, backend: str | None) -> tuple[ArgusConfig, str]:
    """Resolve the config and the backend, CLI flags winning over the file."""
    try:
        config = parse_config_file(config_path) if config_path else load_config(path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    resolved = backend or config.backend
    if not resolved:
        raise click.ClickException(
            "no backend given: pass --backend (" + "|".join(BACKENDS) + ") or set it in argus.yaml"
        )
    if resolved not in BACKENDS:
        raise click.ClickException("unknown backend '" + resolved + "'; expected " + " or ".join(BACKENDS))
    return config, resolved


def _parse_parameters(values: tuple[str, ...]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise click.ClickException("--parameter expects Key=Value, got '" + item + "'")
        key, value = item.split("=", 1)
        parameters[key.strip()] = value
    return parameters


def _preflight(adapter, plan) -> None:
    """Validate every template before the first resource is created.

    The worst outcome of a parallel deploy is a half-built environment: three
    waves succeed, then a malformed template five stacks in aborts the run. One
    validation pass up front turns that into an error before anything exists.
    """
    validate = getattr(adapter, "validate", None)
    if validate is None:
        return

    try:
        failures = validate(plan.units)
    except Exception as exc:  # no credentials, no endpoint: not a reason to stop
        click.secho("skipping pre-flight validation: " + str(exc), fg="yellow")
        return

    if not failures:
        click.secho("pre-flight: " + str(plan.unit_count) + " template(s) validated", fg="green")
        return

    for name, message in sorted(failures.items()):
        click.secho("invalid template " + name + ": " + message, fg="red")
    raise click.ClickException(
        str(len(failures)) + " template(s) failed validation; nothing was deployed"
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="argus")
def cli() -> None:
    """Argus -- deploy, visualise and optimise AWS infrastructure."""


# ---------------------------------------------------------------------------
# Module 1 -- orchestration
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--backend", type=click.Choice(BACKENDS), help="Infrastructure backend to use.")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Explicit argus.yaml.")
@click.option("--parameter", "-p", multiple=True, help="Key=Value, repeatable. Overrides argus.yaml.")
@click.option("--region", help="AWS region. Overrides argus.yaml and AWS_REGION.")
@click.option("--profile", help="AWS named profile.")
@click.option(
    "--endpoint-url",
    help="Alternative service endpoint, e.g. http://localhost:4566 for LocalStack.",
)
@click.option("--max-parallel", type=int, help="Cap on units deployed at once.")
@click.option(
    "--strategy",
    type=click.Choice(STRATEGIES),
    help="rolling starts each unit as soon as its own dependencies are done (default); "
    "waves adds a barrier between topological generations.",
)
@click.option("--plan-only", is_flag=True, help="Show the plan and exit without deploying.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Walk the plan with simulated deployments. Touches no cloud account; timings are fake.",
)
@click.option(
    "--validate/--no-validate",
    default=True,
    show_default=True,
    help="Ask the backend to check every template before creating anything.",
)
@click.option("--report-json", type=click.Path(), help="Write the run report as JSON to this path.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt before a real deployment.")
def deploy(
    path: str,
    backend: str | None,
    config_path: str | None,
    parameter: tuple[str, ...],
    region: str | None,
    profile: str | None,
    endpoint_url: str | None,
    max_parallel: int | None,
    strategy: str | None,
    plan_only: bool,
    dry_run: bool,
    validate: bool,
    report_json: str | None,
    yes: bool,
) -> None:
    """Discover the units under PATH, order them, and deploy them."""
    config, resolved_backend = _load(path, config_path, backend)
    config.parameters.update(_parse_parameters(parameter))
    if region:
        config.region = region
    if profile:
        config.profile = profile
    if endpoint_url:
        config.endpoint_url = endpoint_url
    if max_parallel:
        config.max_parallel = max_parallel

    try:
        adapter = build_adapter(resolved_backend, config)
        orchestrator = Orchestrator(
            adapter,
            max_parallel=config.max_parallel,
            extra_dependencies=config.depends_on,
            strategy=strategy or "rolling",
        )
        plan = orchestrator.plan(path)
    except (AdapterError, PlanError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(plan.describe())

    if plan.declared_edges:
        click.echo("")
        click.secho("declared in argus.yaml (not inferable from the source):", fg="cyan")
        for provider, consumer in plan.declared_edges:
            click.echo("  " + provider + " -> " + consumer)

    if plan.unresolved:
        click.echo("")
        click.secho(
            "warning: the requirements above are provided by nothing in this project. "
            "They must already exist, or the order is incomplete -- declare it under "
            "depends_on in argus.yaml.",
            fg="yellow",
        )

    if plan_only:
        return

    if not dry_run and validate:
        _preflight(adapter, plan)

    if not dry_run and not yes:
        settings = config.aws_settings()
        target = "LocalStack" if settings.is_local else "AWS"
        click.echo("")
        click.secho("target: " + target + " (" + settings.describe() + ")", fg="cyan")
        click.confirm("Deploy " + str(plan.unit_count) + " unit(s) to " + target + "?", abort=True)

    click.echo("")
    label = "simulating" if dry_run else "deploying"
    click.echo(label + " " + str(plan.unit_count) + " unit(s), strategy=" + orchestrator.strategy)
    if not dry_run:
        click.secho("Ctrl-C stops scheduling; units already started are left to finish.", fg="cyan")

    def on_status(name: str, status: UnitStatus) -> None:
        if status in (UnitStatus.PENDING,):
            return
        click.secho("  %-24s %s" % (name, status.value), fg=_STATUS_COLOURS.get(status, "white"))

    try:
        report = orchestrator.run(plan, dry_run=dry_run, on_status=on_status)
    except KeyboardInterrupt:
        orchestrator.cancel()
        raise click.ClickException("interrupted before the run could report") from None

    click.echo("")
    click.echo(report.summary())

    if report_json:
        with open(report_json, "w", encoding="utf-8") as handle:
            json.dump(report.to_dict(), handle, indent=2)
        click.echo("")
        click.echo("report written to " + report_json)

    if not report.succeeded:
        failed = report.by_status(UnitStatus.FAILED)
        skipped = report.by_status(UnitStatus.SKIPPED)
        click.echo("")
        for result in failed:
            click.secho("FAILED " + result.unit + ": " + result.detail, fg="red")
        for result in skipped:
            click.secho("SKIPPED " + result.unit, fg="magenta")
        sys.exit(1)


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--backend", type=click.Choice(BACKENDS), help="Infrastructure backend to use.")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Explicit argus.yaml.")
@click.option("--parameter", "-p", multiple=True, help="Key=Value, repeatable.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "dot", "json"]),
    default="text",
    help="text for the waves, dot for Graphviz, json for tooling.",
)
def graph(
    path: str,
    backend: str | None,
    config_path: str | None,
    parameter: tuple[str, ...],
    output_format: str,
) -> None:
    """Print the dependency graph Argus derives from PATH."""
    config, resolved_backend = _load(path, config_path, backend)
    config.parameters.update(_parse_parameters(parameter))

    try:
        adapter = build_adapter(resolved_backend, config)
        plan = Orchestrator(adapter, extra_dependencies=config.depends_on).plan(path)
    except (AdapterError, PlanError) as exc:
        raise click.ClickException(str(exc)) from exc

    if output_format == "text":
        click.echo(plan.describe())
        return

    if output_format == "dot":
        import networkx as nx

        click.echo(nx.nx_pydot.to_pydot(plan.graph).to_string())
        return

    click.echo(
        json.dumps(
            {
                "backend": plan.backend,
                "waves": plan.waves,
                "units": [
                    {
                        "name": unit.name,
                        "path": unit.path,
                        "provides": sorted(unit.provides),
                        "requires": sorted(unit.requires),
                    }
                    for unit in plan.units
                ],
                "edges": [
                    {"from": source, "to": target, "via": data.get("via", [])}
                    for source, target, data in plan.graph.edges(data=True)
                ],
                "unresolved": plan.unresolved,
            },
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# Module 2 -- optimization
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--service", help="ECS service name. Shorthand for --resource ecs-service.")
@click.option("--resource", "resource_id", help="Identifier of the resource to analyse.")
@click.option(
    "--type",
    "resource_type",
    type=click.Choice(sorted(RESOURCE_KINDS)),
    default="ecs-service",
    show_default=True,
    help="Kind of resource --resource names.",
)
@click.option("--cluster", help="ECS cluster. Discovered automatically when omitted.")
@click.option("--region", help="AWS region. Falls back to AWS_REGION.")
@click.option("--profile", help="AWS named profile.")
@click.option("--endpoint-url", help="Alternative service endpoint, e.g. LocalStack.")
@click.option("--days", default=DEFAULT_PERIOD_DAYS, show_default=True, help="Analysis window.")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
def optimize(
    service: str | None,
    resource_id: str | None,
    resource_type: str,
    cluster: str | None,
    region: str | None,
    profile: str | None,
    endpoint_url: str | None,
    days: int,
    as_json: bool,
) -> None:
    """Compare CloudWatch usage against the current size of a resource."""
    identifier = service or resource_id
    if not identifier:
        raise click.ClickException("pass --service <name> or --resource <id>")
    if service and resource_id:
        raise click.ClickException("--service and --resource are two names for the same argument")
    if service:
        resource_type = "ecs-service"

    try:
        settings = AwsSettings(region=region, profile=profile, endpoint_url=endpoint_url)
        report = Optimizer(settings=settings, period_days=days).analyse(
            identifier, resource_type=resource_type, cluster=cluster
        )
    except OptimizerError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        click.echo(report.summary())


# ---------------------------------------------------------------------------
# Module 3 -- explanation
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--service", help="ECS service name to analyse and then explain.")
@click.option("--resource", "resource_id", help="Identifier of the resource to analyse.")
@click.option(
    "--type",
    "resource_type",
    type=click.Choice(sorted(RESOURCE_KINDS)),
    default="ecs-service",
    show_default=True,
)
@click.option("--cluster", help="ECS cluster. Discovered automatically when omitted.")
@click.option("--region", help="AWS region.")
@click.option("--days", default=DEFAULT_PERIOD_DAYS, show_default=True, help="Analysis window.")
@click.option(
    "--report",
    "report_path",
    type=click.Path(exists=True),
    help="Explain a report saved earlier by `argus optimize --json` instead of querying AWS.",
)
@click.option("--model", default=DEFAULT_MODEL, show_default=True, help="Anthropic model.")
@click.option("--ask", help="An extra question to answer about the report.")
@click.option(
    "--apply",
    "show_apply",
    is_flag=True,
    help="Also print the commands that would apply the recommendation. Argus never runs them.",
)
def explain(
    service: str | None,
    resource_id: str | None,
    resource_type: str,
    cluster: str | None,
    region: str | None,
    days: int,
    report_path: str | None,
    model: str,
    ask: str | None,
    show_apply: bool,
) -> None:
    """Explain an optimization report in plain language."""
    if report_path:
        with open(report_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        identifier = service or resource_id
        if not identifier:
            raise click.ClickException("pass --service, --resource, or --report <file>")
        if service:
            resource_type = "ecs-service"
        try:
            payload = (
                Optimizer(settings=AwsSettings(region=region), period_days=days)
                .analyse(identifier, resource_type=resource_type, cluster=cluster)
                .to_dict()
            )
        except OptimizerError as exc:
            raise click.ClickException(str(exc)) from exc

    try:
        explanation = Explainer(model=model).explain(payload, question=ask)
    except ExplainerError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(explanation.text)
    click.echo("")
    click.secho(
        "-- explained by " + explanation.model
        + (" (%d in / %d out tokens)" % (explanation.input_tokens or 0, explanation.output_tokens or 0)),
        fg="cyan",
    )

    if show_apply:
        commands = render_apply_plan(payload)
        click.echo("")
        if not commands:
            click.echo("No change is proposed, so there is nothing to apply.")
            return
        click.secho(
            "Argus does not apply anything. Review these commands and run them yourself:",
            fg="yellow",
        )
        click.echo("")
        for line in commands:
            click.echo("  " + line)


# ---------------------------------------------------------------------------
# Module 4 -- dashboard
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--port", default=8501, show_default=True, help="Port for the Streamlit server.")
def dashboard(port: int) -> None:
    """Launch the Streamlit front end."""
    from streamlit.web import cli as streamlit_cli

    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
    sys.argv = ["streamlit", "run", app, "--server.port", str(port)]
    sys.exit(streamlit_cli.main())


def main() -> None:  # console-script entry point
    cli()


if __name__ == "__main__":
    main()
