"""Module 1 -- dependency-aware deployment.

The orchestrator knows nothing about CloudFormation or Terraform. It asks an
:class:`~argus.adapters.base.InfrastructureAdapter` for units and a graph,
slices the graph into waves of mutually independent units, and deploys each
wave in parallel.

The measured wall clock is compared against the sum of the individual unit
durations, which is what a strictly sequential run of the same units would
have cost. That comparison is the whole point of the module, so it is
computed from measurements rather than from an assumed per-unit duration.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

import networkx as nx

from .adapters.base import DeployableUnit, DeploymentResult, InfrastructureAdapter, UnitStatus

#: Signature of the progress callback: (unit name, new status).
StatusCallback = Callable[[str, UnitStatus], None]


class PlanError(RuntimeError):
    """Raised when the discovered units cannot be turned into a deployment order."""


@dataclass
class DeploymentPlan:
    """What Argus intends to do, before anything is deployed."""

    backend: str
    units: list[DeployableUnit]
    graph: nx.DiGraph
    waves: list[list[str]]
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    declared_edges: list[tuple[str, str]] = field(default_factory=list)

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def critical_path_length(self) -> int:
        """Number of waves, i.e. the shortest possible number of sequential steps."""
        return len(self.waves)

    def unit(self, name: str) -> DeployableUnit:
        for unit in self.units:
            if unit.name == name:
                return unit
        raise KeyError(name)

    def describe(self) -> str:
        lines = [
            "backend: " + self.backend,
            str(self.unit_count) + " unit(s), " + str(self.graph.number_of_edges()) + " dependency edge(s), "
            + str(len(self.waves)) + " wave(s)",
        ]
        for index, wave in enumerate(self.waves):
            lines.append("  wave " + str(index + 1) + ": " + ", ".join(wave))
        if self.unresolved:
            lines.append("  unresolved requirements (Argus cannot order these):")
            for unit, names in sorted(self.unresolved.items()):
                lines.append("    " + unit + " needs " + ", ".join(names))
        return "\n".join(lines)


@dataclass
class DeploymentReport:
    """What actually happened, with the timing comparison."""

    backend: str
    plan: DeploymentPlan
    results: list[DeploymentResult]
    wall_clock: float
    dry_run: bool = False

    @property
    def sequential_estimate(self) -> float:
        """Time the same units would have taken one after another.

        This is a sum of real per-unit measurements, not a model: it is what a
        sequential run costs assuming each unit takes as long on its own as it
        did inside its wave.
        """
        return sum(result.duration for result in self.results)

    @property
    def speedup(self) -> float:
        return self.sequential_estimate / self.wall_clock if self.wall_clock > 0 else 0.0

    @property
    def time_saved(self) -> float:
        return max(0.0, self.sequential_estimate - self.wall_clock)

    @property
    def succeeded(self) -> bool:
        return all(result.status == UnitStatus.COMPLETE for result in self.results)

    def by_status(self, status: UnitStatus) -> list[DeploymentResult]:
        return [result for result in self.results if result.status == status]

    def summary(self) -> str:
        lines = []
        for result in self.results:
            lines.append(
                "  %-24s %-12s %6.1fs  %s"
                % (result.unit, result.status.value, result.duration, result.detail[:60])
            )
        lines.append("")
        prefix = "SIMULATED " if self.dry_run else ""
        lines.append(prefix + "wall clock (parallel waves) : %.1fs" % self.wall_clock)
        lines.append(prefix + "sequential equivalent       : %.1fs" % self.sequential_estimate)
        lines.append(prefix + "saved                       : %.1fs (x%.2f)" % (self.time_saved, self.speedup))
        return "\n".join(lines)


class Orchestrator:
    """Plan and run a deployment on top of any adapter."""

    def __init__(
        self,
        adapter: InfrastructureAdapter,
        *,
        max_parallel: int = 8,
        extra_dependencies: dict[str, list[str]] | None = None,
    ) -> None:
        self.adapter = adapter
        self.max_parallel = max(1, max_parallel)
        self.extra_dependencies = extra_dependencies or {}
        self._lock = threading.Lock()

    # -- planning ----------------------------------------------------------

    def plan(self, path: str) -> DeploymentPlan:
        units = self.adapter.discover_units(path)
        graph = self.adapter.build_dependency_graph(units)
        declared = self._apply_declared_dependencies(graph, units)

        if not nx.is_directed_acyclic_graph(graph):
            cycle = nx.find_cycle(graph, orientation="original")
            readable = " -> ".join(edge[0] for edge in cycle) + " -> " + cycle[0][0]
            raise PlanError(
                "the dependency graph contains a cycle, so no deployment order exists: " + readable
            )

        waves = [sorted(generation) for generation in nx.topological_generations(graph)]

        return DeploymentPlan(
            backend=self.adapter.backend,
            units=units,
            graph=graph,
            waves=waves,
            unresolved=self.adapter.unresolved_requirements(units),
            declared_edges=declared,
        )

    def _apply_declared_dependencies(
        self, graph: nx.DiGraph, units: Iterable[DeployableUnit]
    ) -> list[tuple[str, str]]:
        """Add the edges the user declared in ``argus.yaml``.

        These cover dependencies that exist in reality but not in the source:
        typically a CloudFormation stack that receives another stack's output
        as a parameter instead of importing it. An unknown name is an error,
        not a no-op, because a typo would silently drop the ordering the user
        asked for.
        """
        known = {unit.name for unit in units}
        added: list[tuple[str, str]] = []
        for consumer, requirements in self.extra_dependencies.items():
            if consumer not in known:
                raise PlanError(
                    "argus.yaml declares a dependency for unknown unit '" + consumer + "'; "
                    "known units: " + ", ".join(sorted(known))
                )
            for provider in requirements:
                if provider not in known:
                    raise PlanError(
                        "argus.yaml: unit '" + consumer + "' depends on unknown unit '" + provider + "'"
                    )
                if not graph.has_edge(provider, consumer):
                    graph.add_edge(provider, consumer, via=["argus.yaml"])
                    added.append((provider, consumer))
        return added

    # -- execution ---------------------------------------------------------

    def run(
        self,
        plan: DeploymentPlan,
        *,
        dry_run: bool = False,
        on_status: StatusCallback | None = None,
    ) -> DeploymentReport:
        """Deploy every unit wave by wave, in parallel inside a wave.

        A unit whose dependency failed is not attempted: it is marked SKIPPED,
        because deploying it would fail on a missing export anyway and would
        add a confusing second error to the report.
        """
        results: list[DeploymentResult] = []
        statuses: dict[str, UnitStatus] = {unit.name: UnitStatus.PENDING for unit in plan.units}

        def announce(name: str, status: UnitStatus) -> None:
            with self._lock:
                statuses[name] = status
                if name in plan.graph:
                    plan.graph.nodes[name]["status"] = status
            if on_status is not None:
                on_status(name, status)

        for name in statuses:
            announce(name, UnitStatus.PENDING)

        started = time.monotonic()

        for wave in plan.waves:
            runnable, blocked = self._split_blocked(plan, wave, statuses)

            for name in blocked:
                announce(name, UnitStatus.SKIPPED)
                now = time.monotonic()
                results.append(
                    DeploymentResult(
                        unit=name,
                        status=UnitStatus.SKIPPED,
                        started_at=now,
                        finished_at=now,
                        detail="skipped: a dependency did not complete",
                    )
                )

            if not runnable:
                continue

            workers = min(self.max_parallel, len(runnable))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._deploy_one, plan.unit(name), dry_run, announce): name
                    for name in runnable
                }
                for future in futures:
                    results.append(future.result())

        wall_clock = time.monotonic() - started

        # Keep the report in plan order rather than completion order, so two
        # runs of the same plan produce diffable output.
        order = {name: index for index, wave in enumerate(plan.waves) for name in wave}
        results.sort(key=lambda result: (order.get(result.unit, 0), result.unit))

        return DeploymentReport(
            backend=plan.backend,
            plan=plan,
            results=results,
            wall_clock=wall_clock,
            dry_run=dry_run,
        )

    @staticmethod
    def _split_blocked(
        plan: DeploymentPlan, wave: list[str], statuses: dict[str, UnitStatus]
    ) -> tuple[list[str], list[str]]:
        runnable, blocked = [], []
        for name in wave:
            predecessors = list(plan.graph.predecessors(name))
            if any(statuses.get(dep) != UnitStatus.COMPLETE for dep in predecessors):
                blocked.append(name)
            else:
                runnable.append(name)
        return runnable, blocked

    def _deploy_one(
        self, unit: DeployableUnit, dry_run: bool, announce: StatusCallback
    ) -> DeploymentResult:
        announce(unit.name, UnitStatus.IN_PROGRESS)
        try:
            if dry_run:
                result = self._simulate(unit)
            else:
                result = self.adapter.deploy_unit(unit)
        except Exception as exc:  # an adapter bug must not lose the whole run
            result = DeploymentResult.timed(unit.name, UnitStatus.FAILED, time.monotonic(), str(exc))
        announce(unit.name, result.status)
        return result

    @staticmethod
    def _simulate(unit: DeployableUnit) -> DeploymentResult:
        """Fake a deployment so the plan and the UI can be exercised without AWS.

        The duration is derived from the resource count purely so that waves
        look uneven the way real ones do. It is not a prediction of anything,
        and every report built from it is labelled SIMULATED.
        """
        started = time.monotonic()
        resources = int(unit.metadata.get("resource_count", 1) or 1)
        time.sleep(min(2.0, 0.05 + resources * 0.02))
        return DeploymentResult.timed(unit.name, UnitStatus.COMPLETE, started, "simulated")
