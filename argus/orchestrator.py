"""Module 1 -- dependency-aware deployment.

The orchestrator knows nothing about CloudFormation or Terraform. It asks an
:class:`~argus.adapters.base.InfrastructureAdapter` for units and a graph, and
deploys them respecting that graph.

Two scheduling strategies, because they measure different things:

* ``rolling`` (default) starts a unit the moment *its own* predecessors are
  done. It is what you want in practice.
* ``waves`` deploys the graph's topological generations one after another,
  with a barrier between them. It is the textbook presentation, it is what the
  UI draws, and it is the baseline ``rolling`` is compared against.

The difference is real, not cosmetic. In a wave, every unit waits for the
slowest one before anything in the next wave starts; a fast stack whose only
dependency finished early sits idle behind an unrelated slow one. Rolling
removes that barrier, so its floor is the graph's critical path rather than
the sum of per-wave maxima.

Three numbers are reported, all from measurements rather than estimates:
the wall clock, the sequential equivalent (the sum of per-unit durations), and
the critical path (the longest dependency chain weighted by those durations) --
which is the fastest any scheduler could possibly have gone.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import networkx as nx

from .adapters.base import DeployableUnit, DeploymentResult, InfrastructureAdapter, UnitStatus

#: Signature of the progress callback: (unit name, new status).
StatusCallback = Callable[[str, UnitStatus], None]

STRATEGIES = ("rolling", "waves")


class PlanError(RuntimeError):
    """Raised when the discovered units cannot be turned into a deployment order."""


def longest_path(graph: nx.DiGraph, weight: Callable[[str], float]) -> tuple[list[str], float]:
    """The heaviest chain through a DAG, with weights on the nodes.

    ``networkx.dag_longest_path`` weights edges; what matters here is time
    spent in the units themselves, so this walks the topological order once and
    keeps the best predecessor for each node. O(V + E).
    """
    best: dict[str, float] = {}
    previous: dict[str, str | None] = {}

    for name in nx.topological_sort(graph):
        incoming = list(graph.predecessors(name))
        if not incoming:
            best[name], previous[name] = weight(name), None
            continue
        parent = max(incoming, key=lambda candidate: best[candidate])
        best[name] = best[parent] + weight(name)
        previous[name] = parent

    if not best:
        return [], 0.0

    end = max(best, key=lambda name: best[name])
    path: list[str] = []
    cursor: str | None = end
    while cursor is not None:
        path.append(cursor)
        cursor = previous[cursor]
    path.reverse()
    return path, best[end]


@dataclass
class DeploymentPlan:
    """What Argus intends to do, before anything is deployed."""

    backend: str
    units: list[DeployableUnit]
    graph: nx.DiGraph
    waves: list[list[str]]
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    declared_edges: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_name = {unit.name: unit for unit in self.units}

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def critical_path(self) -> list[str]:
        """The longest dependency chain, counting units.

        No deployment can be shorter than this chain, whatever the level of
        parallelism. It is the structural limit of the project, and shortening
        it means changing the infrastructure, not the scheduler.
        """
        path, _ = longest_path(self.graph, lambda _: 1.0)
        return path

    @property
    def max_concurrency(self) -> int:
        """The widest wave: the most units that could ever run at once."""
        return max((len(wave) for wave in self.waves), default=0)

    def unit(self, name: str) -> DeployableUnit:
        return self._by_name[name]

    def describe(self) -> str:
        critical = self.critical_path
        lines = [
            "backend: " + self.backend,
            str(self.unit_count) + " unit(s), " + str(self.graph.number_of_edges())
            + " dependency edge(s), " + str(len(self.waves)) + " wave(s)",
        ]
        for index, wave in enumerate(self.waves):
            lines.append("  wave " + str(index + 1) + ": " + ", ".join(wave))
        lines.append(
            "critical path (" + str(len(critical)) + " units): " + " -> ".join(critical)
        )
        lines.append("peak concurrency: " + str(self.max_concurrency))
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
    strategy: str = "rolling"
    cancelled: bool = False

    @property
    def sequential_estimate(self) -> float:
        """Time the same units would have taken one after another.

        A sum of real per-unit measurements, not a model: what a sequential run
        costs assuming each unit takes as long on its own as it did here.
        """
        return sum(result.duration for result in self.results)

    @property
    def durations(self) -> dict[str, float]:
        return {result.unit: result.duration for result in self.results}

    @property
    def critical_path_duration(self) -> float:
        """The floor: the longest chain, weighted by measured durations.

        No scheduler could have finished faster than this, so it is the honest
        yardstick for the wall clock -- more informative than the sequential
        comparison, which flatters any parallel run.
        """
        durations = self.durations
        _, total = longest_path(self.plan.graph, lambda name: durations.get(name, 0.0))
        return total

    @property
    def critical_path(self) -> list[str]:
        durations = self.durations
        path, _ = longest_path(self.plan.graph, lambda name: durations.get(name, 0.0))
        return path

    @property
    def speedup(self) -> float:
        return self.sequential_estimate / self.wall_clock if self.wall_clock > 0 else 0.0

    @property
    def efficiency(self) -> float:
        """How close the run got to the critical path. 1.0 is optimal.

        Below 1.0 means time was spent waiting on something other than a real
        dependency -- a wave barrier, or the max_parallel cap.
        """
        floor = self.critical_path_duration
        return floor / self.wall_clock if self.wall_clock > 0 else 0.0

    @property
    def time_saved(self) -> float:
        return max(0.0, self.sequential_estimate - self.wall_clock)

    @property
    def succeeded(self) -> bool:
        return bool(self.results) and all(
            result.status == UnitStatus.COMPLETE for result in self.results
        )

    def by_status(self, status: UnitStatus) -> list[DeploymentResult]:
        return [result for result in self.results if result.status == status]

    def summary(self) -> str:
        lines = []
        for result in self.results:
            lines.append(
                "  %-24s %-12s %6.1fs  %s"
                % (result.unit, result.status.value, result.duration, result.detail[:60])
            )
        prefix = "SIMULATED " if self.dry_run else ""
        lines += [
            "",
            prefix + "strategy                    : " + self.strategy,
            prefix + "wall clock                  : %.1fs" % self.wall_clock,
            prefix + "sequential equivalent       : %.1fs" % self.sequential_estimate,
            prefix + "critical path (the floor)   : %.1fs  [%s]"
            % (self.critical_path_duration, " -> ".join(self.critical_path)),
            prefix + "saved vs sequential         : %.1fs (x%.2f)" % (self.time_saved, self.speedup),
            prefix + "efficiency vs the floor     : %.0f%%" % (self.efficiency * 100),
        ]
        if self.cancelled:
            lines.append("run was cancelled before every unit was attempted")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        """Machine-readable report, for CI or for a later diff."""
        return {
            "backend": self.backend,
            "strategy": self.strategy,
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
            "succeeded": self.succeeded,
            "wall_clock": round(self.wall_clock, 3),
            "sequential_estimate": round(self.sequential_estimate, 3),
            "critical_path": self.critical_path,
            "critical_path_duration": round(self.critical_path_duration, 3),
            "speedup": round(self.speedup, 3),
            "efficiency": round(self.efficiency, 3),
            "waves": self.plan.waves,
            "units": [
                {
                    "name": result.unit,
                    "status": result.status.value,
                    "duration": round(result.duration, 3),
                    "detail": result.detail,
                }
                for result in self.results
            ],
        }


class Orchestrator:
    """Plan and run a deployment on top of any adapter."""

    def __init__(
        self,
        adapter: InfrastructureAdapter,
        *,
        max_parallel: int = 8,
        extra_dependencies: dict[str, list[str]] | None = None,
        strategy: str = "rolling",
    ) -> None:
        if strategy not in STRATEGIES:
            raise ValueError("unknown strategy '" + strategy + "'; expected " + " or ".join(STRATEGIES))
        self.adapter = adapter
        self.max_parallel = max(1, max_parallel)
        self.extra_dependencies = extra_dependencies or {}
        self.strategy = strategy
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Stop scheduling new units. In-flight ones are left to finish.

        Killing a deployment mid-``create_stack`` does not stop CloudFormation;
        it only loses track of it. So cancellation stops the *scheduler* and
        waits, which leaves the account in a state Argus can still describe.
        """
        self._cancelled.set()

    # -- planning ----------------------------------------------------------

    def plan(self, path: str) -> DeploymentPlan:
        units = self.adapter.discover_units(path)
        self._reject_duplicate_names(units)
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

    @staticmethod
    def _reject_duplicate_names(units: Sequence[DeployableUnit]) -> None:
        """Two units with one name would silently collapse into one graph node.

        That is worse than an error: the plan would look complete while one of
        the two templates never deployed.
        """
        seen: set[str] = set()
        duplicates: set[str] = set()
        for unit in units:
            if unit.name in seen:
                duplicates.add(unit.name)
            seen.add(unit.name)
        if duplicates:
            raise PlanError(
                "two units share a name, which the graph cannot represent: "
                + ", ".join(sorted(duplicates))
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
        strategy: str | None = None,
    ) -> DeploymentReport:
        """Deploy every unit, respecting the graph.

        A unit whose dependency failed is not attempted: it is marked SKIPPED,
        because deploying it would fail on a missing export anyway and would
        add a confusing second error to the report.
        """
        strategy = strategy or self.strategy
        if strategy not in STRATEGIES:
            raise ValueError("unknown strategy '" + strategy + "'")

        self._cancelled.clear()
        statuses: dict[str, UnitStatus] = {}
        results: list[DeploymentResult] = []

        def announce(name: str, status: UnitStatus) -> None:
            with self._lock:
                statuses[name] = status
                if name in plan.graph:
                    plan.graph.nodes[name]["status"] = status
            if on_status is not None:
                on_status(name, status)

        for unit in plan.units:
            announce(unit.name, UnitStatus.PENDING)

        started = time.monotonic()
        try:
            if strategy == "waves":
                self._run_waves(plan, dry_run, announce, statuses, results)
            else:
                self._run_rolling(plan, dry_run, announce, statuses, results)
        except KeyboardInterrupt:
            # Reached only if the interrupt lands outside the scheduler's own
            # handling; the in-flight units have already been waited for.
            self.cancel()
        wall_clock = time.monotonic() - started

        self._record_unattempted(plan, statuses, results)

        # Report in plan order rather than completion order, so two runs of the
        # same plan produce diffable output.
        order = {name: index for index, wave in enumerate(plan.waves) for name in wave}
        results.sort(key=lambda result: (order.get(result.unit, 0), result.unit))

        return DeploymentReport(
            backend=plan.backend,
            plan=plan,
            results=results,
            wall_clock=wall_clock,
            dry_run=dry_run,
            strategy=strategy,
            cancelled=self._cancelled.is_set(),
        )

    # -- strategies --------------------------------------------------------

    def _run_rolling(
        self,
        plan: DeploymentPlan,
        dry_run: bool,
        announce: StatusCallback,
        statuses: dict[str, UnitStatus],
        results: list[DeploymentResult],
    ) -> None:
        """Start each unit as soon as its own predecessors have completed."""
        pending = {unit.name for unit in plan.units}
        blocking = {name: set(plan.graph.predecessors(name)) for name in pending}
        inflight: dict[Future, str] = {}

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            while pending or inflight:
                if not self._cancelled.is_set():
                    for name in sorted(name for name in pending if not blocking[name]):
                        if len(inflight) >= self.max_parallel:
                            break
                        pending.discard(name)
                        future = pool.submit(
                            self._deploy_one, plan.unit(name), dry_run, announce
                        )
                        inflight[future] = name

                if not inflight:
                    # Nothing running and nothing runnable: either cancelled, or
                    # everything left is blocked behind a failure.
                    break

                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for future in done:
                    name = inflight.pop(future)
                    result = future.result()
                    results.append(result)

                    if result.status == UnitStatus.COMPLETE:
                        for successor in plan.graph.successors(name):
                            blocking.get(successor, set()).discard(name)
                    else:
                        self._skip_descendants(plan, name, pending, announce, statuses, results)

    def _run_waves(
        self,
        plan: DeploymentPlan,
        dry_run: bool,
        announce: StatusCallback,
        statuses: dict[str, UnitStatus],
        results: list[DeploymentResult],
    ) -> None:
        """Deploy topological generations with a barrier between them."""
        pending = {unit.name for unit in plan.units}

        for wave in plan.waves:
            if self._cancelled.is_set():
                break

            runnable = []
            for name in wave:
                if name not in pending:
                    continue  # already skipped as the descendant of a failure
                if all(
                    statuses.get(dep) == UnitStatus.COMPLETE
                    for dep in plan.graph.predecessors(name)
                ):
                    runnable.append(name)

            if not runnable:
                continue

            for name in runnable:
                pending.discard(name)

            workers = min(self.max_parallel, len(runnable))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._deploy_one, plan.unit(name), dry_run, announce): name
                    for name in runnable
                }
                for future in futures:
                    result = future.result()
                    results.append(result)
                    if result.status != UnitStatus.COMPLETE:
                        self._skip_descendants(
                            plan, result.unit, pending, announce, statuses, results
                        )

    # -- bookkeeping -------------------------------------------------------

    def _skip_descendants(
        self,
        plan: DeploymentPlan,
        failed: str,
        pending: set[str],
        announce: StatusCallback,
        statuses: dict[str, UnitStatus],
        results: list[DeploymentResult],
    ) -> None:
        """Mark everything downstream of a failure as SKIPPED, transitively."""
        for name in sorted(nx.descendants(plan.graph, failed)):
            if name not in pending:
                continue
            pending.discard(name)
            announce(name, UnitStatus.SKIPPED)
            now = time.monotonic()
            results.append(
                DeploymentResult(
                    unit=name,
                    status=UnitStatus.SKIPPED,
                    started_at=now,
                    finished_at=now,
                    detail="skipped: " + failed + " did not complete",
                )
            )

    def _record_unattempted(
        self,
        plan: DeploymentPlan,
        statuses: dict[str, UnitStatus],
        results: list[DeploymentResult],
    ) -> None:
        """Give every unit a row, including ones cancellation never reached.

        A report that silently omits units is the one thing worse than a
        report full of failures.
        """
        recorded = {result.unit for result in results}
        for unit in plan.units:
            if unit.name in recorded:
                continue
            now = time.monotonic()
            detail = "not attempted: run cancelled" if self._cancelled.is_set() else "not attempted"
            statuses[unit.name] = UnitStatus.SKIPPED
            results.append(
                DeploymentResult(
                    unit=unit.name,
                    status=UnitStatus.SKIPPED,
                    started_at=now,
                    finished_at=now,
                    detail=detail,
                )
            )

    def _deploy_one(
        self, unit: DeployableUnit, dry_run: bool, announce: StatusCallback
    ) -> DeploymentResult:
        announce(unit.name, UnitStatus.IN_PROGRESS)
        try:
            result = self._simulate(unit) if dry_run else self.adapter.deploy_unit(unit)
        except Exception as exc:  # an adapter bug must not lose the whole run
            result = DeploymentResult.timed(unit.name, UnitStatus.FAILED, time.monotonic(), str(exc))
        announce(unit.name, result.status)
        return result

    @staticmethod
    def _simulate(unit: DeployableUnit) -> DeploymentResult:
        """Fake a deployment so the plan and the UI can be exercised without AWS.

        The duration is derived from the resource count purely so that units
        take visibly different times, the way real ones do. It is not a
        prediction, and every report built from it is labelled SIMULATED.
        """
        started = time.monotonic()
        resources = int(unit.metadata.get("resource_count", 1) or 1)
        time.sleep(min(2.0, 0.05 + resources * 0.02))
        return DeploymentResult.timed(unit.name, UnitStatus.COMPLETE, started, "simulated")
