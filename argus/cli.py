"""The ``argus`` command line."""

from __future__ import annotations

import os
import sys

import click

from . import __version__
from .adapters import BACKENDS, AdapterError, build_adapter
from .adapters.base import UnitStatus
from .config import ArgusConfig, ConfigError, load_config, parse_config_file
from .orchestrator import Orchestrator, PlanError

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
@click.option("--max-parallel", type=int, help="Cap on units deployed at once inside a wave.")
@click.option("--plan-only", is_flag=True, help="Show the waves and exit without deploying.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Walk the plan with simulated deployments. Touches no cloud account; timings are fake.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt before a real deployment.")
def deploy(
    path: str,
    backend: str | None,
    config_path: str | None,
    parameter: tuple[str, ...],
    region: str | None,
    max_parallel: int | None,
    plan_only: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Discover the units under PATH, order them, and deploy them in waves."""
    config, resolved_backend = _load(path, config_path, backend)
    config.parameters.update(_parse_parameters(parameter))
    if region:
        config.region = region
    if max_parallel:
        config.max_parallel = max_parallel

    try:
        adapter = build_adapter(resolved_backend, config)
        orchestrator = Orchestrator(
            adapter, max_parallel=config.max_parallel, extra_dependencies=config.depends_on
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

    if not dry_run and not yes:
        click.confirm(
            "\nDeploy " + str(plan.unit_count) + " unit(s) to AWS in " + str(len(plan.waves)) + " wave(s)?",
            abort=True,
        )

    click.echo("")
    label = "simulating" if dry_run else "deploying"
    click.echo(label + " " + str(plan.unit_count) + " unit(s)...")

    def on_status(name: str, status: UnitStatus) -> None:
        if status in (UnitStatus.PENDING,):
            return
        click.secho("  %-24s %s" % (name, status.value), fg=_STATUS_COLOURS.get(status, "white"))

    report = orchestrator.run(plan, dry_run=dry_run, on_status=on_status)

    click.echo("")
    click.echo(report.summary())

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

    import json

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
