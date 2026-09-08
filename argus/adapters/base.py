"""Common abstraction shared by every infrastructure backend.

Everything above this module (graph building, orchestration, the Streamlit
front, the optimizer, the explainer) talks to :class:`InfrastructureAdapter`
and never to CloudFormation or Terraform directly. Adding a third backend
means adding one file in this package, not touching the rest of Argus.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

import networkx as nx


class UnitStatus(str, Enum):
    """Lifecycle of a deployable unit, normalised across backends.

    Backends report wildly different vocabularies (``CREATE_COMPLETE``,
    ``Apply complete!``, ...); adapters translate them into these five values
    so the front end can colour a node without knowing the backend.
    """

    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        return self in (UnitStatus.COMPLETE, UnitStatus.FAILED, UnitStatus.SKIPPED)


@dataclass(frozen=True)
class DeployableUnit:
    """One thing Argus can deploy: a CloudFormation stack, a Terraform root module.

    ``provides`` and ``requires`` are the only inputs used to derive the
    dependency graph. They hold *symbolic* names: for CloudFormation the
    export name as written in the template (placeholders left unresolved when
    no parameter values are supplied), for Terraform the module names declared
    in ``argus.yaml``. Both sides of a dependency go through the same
    normalisation function, so unresolved placeholders still match.
    """

    name: str
    path: str
    backend: str
    provides: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()
    parameters: dict[str, str] = field(default_factory=dict, compare=False)
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.name


@dataclass
class DeploymentResult:
    """Outcome of deploying a single unit, with the timing Module 1 reports."""

    unit: str
    status: UnitStatus
    started_at: float
    finished_at: float
    detail: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @classmethod
    def timed(cls, unit: str, status: UnitStatus, started_at: float, detail: str = "") -> "DeploymentResult":
        return cls(unit=unit, status=status, started_at=started_at, finished_at=time.monotonic(), detail=detail)


class AdapterError(RuntimeError):
    """Raised when a backend cannot be read or driven."""


class InfrastructureAdapter(ABC):
    """Contract every backend implements.

    Implementations must be safe to call from several threads at once:
    :class:`argus.orchestrator.Orchestrator` deploys an independent wave in
    parallel, which means concurrent ``deploy_unit`` calls on one instance.
    """

    #: Short identifier used by the CLI (``--backend``) and shown in the UI.
    backend: str = "unknown"

    @abstractmethod
    def discover_units(self, path: str) -> list[DeployableUnit]:
        """Find every deployable unit under ``path``."""

    @abstractmethod
    def deploy_unit(self, unit: DeployableUnit) -> DeploymentResult:
        """Deploy one unit and block until it reaches a terminal state."""

    @abstractmethod
    def get_unit_status(self, unit: DeployableUnit) -> UnitStatus:
        """Return the current status of ``unit`` as the backend sees it."""

    def build_dependency_graph(self, units: Iterable[DeployableUnit]) -> nx.DiGraph:
        """Derive the dependency graph from ``provides`` / ``requires``.

        The default implementation matches produced names against consumed
        names, which covers CloudFormation exports/imports and the explicit
        module dependencies declared in ``argus.yaml``. Backends whose graph
        comes from somewhere else (Terraform's own ``terraform graph``)
        override this.

        Edges point from the provider to the consumer, so a topological order
        is a valid deployment order.
        """
        units = list(units)
        graph = nx.DiGraph()

        producers: dict[str, list[DeployableUnit]] = {}
        for unit in units:
            graph.add_node(unit.name, unit=unit, status=UnitStatus.PENDING, backend=self.backend)
            for provided in unit.provides:
                producers.setdefault(provided, []).append(unit)

        for unit in units:
            for required in unit.requires:
                for provider in producers.get(required, []):
                    if provider.name == unit.name:
                        continue  # a unit importing its own export is not a dependency
                    if graph.has_edge(provider.name, unit.name):
                        graph[provider.name][unit.name]["via"].append(required)
                    else:
                        graph.add_edge(provider.name, unit.name, via=[required])

        return graph

    def inspect_unit(self, unit: DeployableUnit) -> nx.DiGraph | None:
        """The backend's own internal graph for one unit, for display only.

        Some backends schedule work inside a unit themselves (Terraform inside
        one state). Returning that graph lets the front end show what will
        happen without Argus pretending to orchestrate it. ``None`` means the
        backend has nothing finer-grained to show.
        """
        return None

    def unresolved_requirements(self, units: Iterable[DeployableUnit]) -> dict[str, list[str]]:
        """Names a unit consumes that nothing in the project provides.

        These are the cases Argus cannot order automatically — an import
        satisfied by a stack deployed out-of-band, or a value hardcoded
        instead of exported. Surfaced to the user rather than silently
        ignored, because they are exactly where a wrong deployment order
        hides.
        """
        units = list(units)
        provided = {name for unit in units for name in unit.provides}
        missing: dict[str, list[str]] = {}
        for unit in units:
            gaps = sorted(name for name in unit.requires if name not in provided)
            if gaps:
                missing[unit.name] = gaps
        return missing
