"""Terraform backend.

What Argus does here is deliberately narrower than what it does for
CloudFormation, because Terraform already does most of the job:

* Inside one root module, ``terraform apply`` builds its own dependency graph
  and parallelises independent resources. Argus does not re-implement that and
  does not try to improve on it. It reads that graph with ``terraform graph``
  and renders it more legibly than the raw DOT output -- a visualisation, not
  an orchestration.
* Between several root modules with separate states, Terraform has no native
  ordering: two states share nothing Argus could read. That ordering has to be
  declared under ``modules:`` in ``argus.yaml``, and sequencing those modules
  is the one place Argus adds execution behaviour on this backend. Terragrunt
  already solves this problem and is more mature at it.

Deployment is delegated to the ``terraform`` binary. Argus never writes state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Iterable, Sequence

import networkx as nx

from ..config import ModuleSpec
from .base import AdapterError, DeployableUnit, DeploymentResult, InfrastructureAdapter, UnitStatus

#: Nodes ``terraform graph`` emits that describe the run rather than the
#: infrastructure. Showing them buries the resources the reader came for.
_NOISE_PREFIXES = (
    "provider[",
    "provider.",
    "meta.count-boundary",
    "root",
)

_NOISE_SUFFIXES = (" (close)", " (expand)")


class TerraformAdapter(InfrastructureAdapter):
    """Discover Terraform root modules, order them, and drive ``terraform apply``."""

    backend = "terraform"

    def __init__(
        self,
        modules: Iterable[ModuleSpec] | None = None,
        *,
        binary: str = "terraform",
        timeout: int = 3600,
        runner: Any = None,
    ) -> None:
        self.modules = list(modules or [])
        self.binary = binary
        self.timeout = timeout
        # Seam for tests: anything with the signature of ``_run_terraform``.
        self._runner = runner or self._run_terraform

    # -- discovery ---------------------------------------------------------

    def discover_units(self, path: str) -> list[DeployableUnit]:
        """One unit per declared root module, or one unit for ``path`` itself.

        A module provides its own name and requires the names it declares in
        ``depends_on``, which lets the base class build the graph from the same
        provides/requires matching used for CloudFormation exports.
        """
        if self.modules:
            target = os.path.abspath(path.rstrip("/\\"))
            selected = [
                module for module in self.modules if os.path.abspath(module.path) == target
            ]
            if len(selected) == 1:
                # The user pointed at one declared module rather than at the
                # project: deploy that module alone, and say so, because
                # Terraform handles everything inside it by itself.
                unit = self._module_unit(selected[0])
                return [_as_single_module(unit)]
            return [self._module_unit(module) for module in self.modules]

        if not os.path.isdir(path):
            raise AdapterError(path + ": expected a directory containing a Terraform root module")
        if not any(entry.endswith((".tf", ".tf.json")) for entry in os.listdir(path)):
            raise AdapterError(
                path + ": no .tf file found. For a project with several root modules, "
                "declare them under modules: in argus.yaml."
            )

        name = os.path.basename(os.path.abspath(path.rstrip("/\\"))) or "root"
        return [
            DeployableUnit(
                name=name,
                path=os.path.abspath(path),
                backend=self.backend,
                metadata={
                    "single_module": True,
                    "graph_source": "terraform graph -type=plan",
                },
            )
        ]

    def _module_unit(self, module: ModuleSpec) -> DeployableUnit:
        if not os.path.isdir(module.path):
            raise AdapterError(
                "module '" + module.name + "' points at " + module.path + ", which is not a directory"
            )
        return DeployableUnit(
            name=module.name,
            path=module.path,
            backend=self.backend,
            provides=frozenset({module.name}),
            requires=frozenset(module.depends_on),
            metadata={
                "single_module": False,
                "var_files": list(module.var_files),
                "variables": dict(module.variables),
                "graph_source": "argus.yaml",
            },
        )

    # -- graphs ------------------------------------------------------------

    def inspect_unit(self, unit: DeployableUnit) -> nx.DiGraph | None:
        """Terraform's own resource graph for one module, for display only.

        This is never used to order anything: inside a single state Terraform
        schedules its own resources. It exists so the front end can show what
        Terraform is going to do, instead of a single opaque box.
        """
        try:
            completed = self._runner(["graph", "-type=plan"], cwd=unit.path)
        except AdapterError:
            return None
        if completed.returncode != 0:
            return None
        return parse_terraform_graph(completed.stdout)

    # -- deployment --------------------------------------------------------

    def deploy_unit(self, unit: DeployableUnit) -> DeploymentResult:
        started = time.monotonic()

        init = self._runner(["init", "-input=false", "-no-color"], cwd=unit.path)
        if init.returncode != 0:
            return DeploymentResult.timed(
                unit.name, UnitStatus.FAILED, started, _tail(init.stderr or init.stdout)
            )

        arguments = ["apply", "-auto-approve", "-input=false", "-no-color"]
        for var_file in unit.metadata.get("var_files", []):
            arguments.append("-var-file=" + var_file)
        for key, value in sorted(unit.metadata.get("variables", {}).items()):
            arguments.append("-var")
            arguments.append(key + "=" + value)

        apply = self._runner(arguments, cwd=unit.path)
        if apply.returncode != 0:
            return DeploymentResult.timed(
                unit.name, UnitStatus.FAILED, started, _tail(apply.stderr or apply.stdout)
            )
        return DeploymentResult.timed(
            unit.name, UnitStatus.COMPLETE, started, _summary_line(apply.stdout)
        )

    def get_unit_status(self, unit: DeployableUnit) -> UnitStatus:
        """COMPLETE once the module's state holds at least one resource.

        Terraform has no per-module status to read, so this is a statement
        about the state file, not about drift. It cannot tell an applied module
        from one that has drifted since.
        """
        try:
            completed = self._runner(["state", "list"], cwd=unit.path)
        except AdapterError:
            return UnitStatus.UNKNOWN
        if completed.returncode != 0:
            return UnitStatus.UNKNOWN
        return UnitStatus.COMPLETE if completed.stdout.strip() else UnitStatus.PENDING

    # -- process -----------------------------------------------------------

    def _run_terraform(self, arguments: Sequence[str], cwd: str) -> subprocess.CompletedProcess:
        if shutil.which(self.binary) is None:
            raise AdapterError(
                "the '" + self.binary + "' binary was not found on PATH; Argus delegates every "
                "apply to Terraform rather than re-implementing it"
            )
        return subprocess.run(  # noqa: S603 - arguments are built here, not user shell input
            [self.binary, *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )


# ---------------------------------------------------------------------------
# DOT parsing
# ---------------------------------------------------------------------------


def parse_terraform_graph(dot: str) -> nx.DiGraph:
    """Turn ``terraform graph`` output into a graph Argus renders like any other.

    Two adjustments are made to the raw output:

    * Run bookkeeping nodes (providers, the root node, the count boundary) are
      dropped, because they crowd out the resources.
    * Edges are reversed. Terraform draws ``A -> B`` to mean "A depends on B";
      everywhere else in Argus an edge points from the provider to the
      consumer, so a topological order reads as a creation order.
    """
    graph = nx.DiGraph()
    if not dot.strip():
        return graph

    try:
        import pydot
    except ImportError as exc:
        raise AdapterError("reading terraform graph output needs pydot: pip install pydot") from exc

    parsed = pydot.graph_from_dot_data(dot)
    if not parsed:
        raise AdapterError("could not parse the output of `terraform graph`")

    for element in parsed:
        _collect(element, graph)
    return graph


def _collect(element, graph: nx.DiGraph) -> None:
    for node in element.get_nodes():
        name = _clean(node.get_name())
        if name and not _is_noise(name):
            graph.add_node(name, kind=_kind(name))

    for edge in element.get_edges():
        source = _clean(edge.get_source())
        target = _clean(edge.get_destination())
        if not source or not target or _is_noise(source) or _is_noise(target):
            continue
        graph.add_node(source, kind=_kind(source))
        graph.add_node(target, kind=_kind(target))
        # Reversed: terraform points at what a resource needs.
        graph.add_edge(target, source)

    for subgraph in element.get_subgraphs():
        _collect(subgraph, graph)


def _clean(name: str | None) -> str:
    if not name:
        return ""
    name = name.strip().strip('"')
    if name.startswith("[root] "):
        name = name[len("[root] ") :]
    for suffix in _NOISE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def _is_noise(name: str) -> bool:
    if not name or name in ("graph", "{", "}"):
        return True
    return any(name.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _kind(name: str) -> str:
    """A coarse label the front end can colour by."""
    if name.startswith("var."):
        return "variable"
    if name.startswith("local."):
        return "local"
    if name.startswith("output."):
        return "output"
    if name.startswith("data."):
        return "data"
    if name.startswith("module."):
        return "module"
    return "resource"


def _tail(text: str, lines: int = 6) -> str:
    """The end of Terraform's output, which is where its error lives."""
    stripped = [line for line in (text or "").splitlines() if line.strip()]
    return " | ".join(stripped[-lines:]) if stripped else "terraform failed with no output"


def _summary_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if "Apply complete!" in line or "No changes" in line:
            return line.strip()
    return "apply finished"


def _as_single_module(unit: DeployableUnit) -> DeployableUnit:
    """Re-label a declared module that is being deployed on its own.

    Its declared dependencies are dropped along with the rest of the project:
    Argus is not ordering anything here, so claiming an edge to a module that
    is not part of this run would be misleading.
    """
    metadata = dict(unit.metadata)
    metadata["single_module"] = True
    metadata["graph_source"] = "terraform graph -type=plan"
    return DeployableUnit(
        name=unit.name,
        path=unit.path,
        backend=unit.backend,
        parameters=unit.parameters,
        metadata=metadata,
    )
