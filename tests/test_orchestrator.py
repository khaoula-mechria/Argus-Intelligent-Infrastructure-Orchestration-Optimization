"""Tests for the backend-neutral orchestrator, driven by a fake adapter."""

from __future__ import annotations

import threading
import time

import networkx as nx
import pytest

from argus.adapters.base import DeployableUnit, DeploymentResult, InfrastructureAdapter, UnitStatus
from argus.orchestrator import Orchestrator, PlanError, longest_path


class FakeAdapter(InfrastructureAdapter):
    """An adapter over units declared inline, with controllable outcomes.

    ``failures`` names the units that must fail; ``delay`` is how long each
    deployment sleeps, which is what lets the parallelism assertions below be
    about real concurrency rather than about a mock call count.
    """

    backend = "fake"

    def __init__(self, units, failures=(), delay=0.0):
        self._units = list(units)
        self._failures = set(failures)
        self._delay = delay
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def discover_units(self, path):
        return list(self._units)

    def deploy_unit(self, unit):
        started = time.monotonic()
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self._delay)
        finally:
            with self._lock:
                self.concurrent -= 1
        status = UnitStatus.FAILED if unit.name in self._failures else UnitStatus.COMPLETE
        return DeploymentResult.timed(unit.name, status, started, "fake")

    def get_unit_status(self, unit):
        return UnitStatus.UNKNOWN


def unit(name, provides=(), requires=()):
    return DeployableUnit(
        name=name,
        path=name + ".yaml",
        backend="fake",
        provides=frozenset(provides),
        requires=frozenset(requires),
    )


def diamond():
    """a -> (b, c) -> d: the smallest graph with a genuinely parallel wave."""
    return [
        unit("a", provides=["A"]),
        unit("b", provides=["B"], requires=["A"]),
        unit("c", provides=["C"], requires=["A"]),
        unit("d", requires=["B", "C"]),
    ]


# -- planning ---------------------------------------------------------------


def test_plan_groups_independent_units_into_one_wave():
    plan = Orchestrator(FakeAdapter(diamond())).plan(".")
    assert plan.waves == [["a"], ["b", "c"], ["d"]]


def test_plan_rejects_a_cycle():
    units = [unit("a", provides=["A"], requires=["B"]), unit("b", provides=["B"], requires=["A"])]
    with pytest.raises(PlanError, match="cycle"):
        Orchestrator(FakeAdapter(units)).plan(".")


def test_declared_dependency_adds_an_edge_the_source_does_not_express():
    units = [unit("vpc", provides=["V"]), unit("alb")]
    orchestrator = Orchestrator(FakeAdapter(units), extra_dependencies={"alb": ["vpc"]})
    plan = orchestrator.plan(".")

    assert plan.waves == [["vpc"], ["alb"]]
    assert plan.declared_edges == [("vpc", "alb")]


def test_declared_dependency_on_an_unknown_unit_is_an_error():
    # A typo here must not silently degrade into "no extra ordering".
    orchestrator = Orchestrator(FakeAdapter([unit("alb")]), extra_dependencies={"alb": ["vpcc"]})
    with pytest.raises(PlanError, match="vpcc"):
        orchestrator.plan(".")


def test_declared_dependency_for_an_unknown_consumer_is_an_error():
    orchestrator = Orchestrator(FakeAdapter([unit("alb")]), extra_dependencies={"nope": ["alb"]})
    with pytest.raises(PlanError, match="nope"):
        orchestrator.plan(".")


def test_a_declared_edge_that_already_exists_is_not_duplicated():
    units = [unit("a", provides=["A"]), unit("b", requires=["A"])]
    plan = Orchestrator(FakeAdapter(units), extra_dependencies={"b": ["a"]}).plan(".")
    assert plan.graph.number_of_edges() == 1
    assert plan.declared_edges == []


# -- execution --------------------------------------------------------------


def test_a_wave_really_runs_in_parallel():
    adapter = FakeAdapter(diamond(), delay=0.15)
    orchestrator = Orchestrator(adapter, max_parallel=8)
    orchestrator.run(orchestrator.plan("."))
    assert adapter.max_concurrent == 2  # b and c, never a with b


def test_max_parallel_caps_concurrency():
    units = [unit(name) for name in "abcdef"]
    adapter = FakeAdapter(units, delay=0.1)
    orchestrator = Orchestrator(adapter, max_parallel=2)
    orchestrator.run(orchestrator.plan("."))
    assert adapter.max_concurrent <= 2


def test_report_compares_wall_clock_against_the_sequential_equivalent():
    adapter = FakeAdapter(diamond(), delay=0.15)
    orchestrator = Orchestrator(adapter)
    report = orchestrator.run(orchestrator.plan("."))

    assert report.succeeded
    # Four units at 0.15s each run sequentially in ~0.6s; three waves take ~0.45s.
    assert report.sequential_estimate > report.wall_clock
    assert report.speedup > 1.0
    assert report.time_saved == pytest.approx(report.sequential_estimate - report.wall_clock)


def test_a_failure_skips_its_dependents_instead_of_deploying_them():
    adapter = FakeAdapter(diamond(), failures={"b"})
    orchestrator = Orchestrator(adapter)
    report = orchestrator.run(orchestrator.plan("."))

    statuses = {result.unit: result.status for result in report.results}
    assert statuses["a"] == UnitStatus.COMPLETE
    assert statuses["b"] == UnitStatus.FAILED
    assert statuses["c"] == UnitStatus.COMPLETE  # independent of b, still deployed
    assert statuses["d"] == UnitStatus.SKIPPED
    assert not report.succeeded


def test_an_adapter_exception_becomes_a_failed_unit_not_a_crash():
    class Exploding(FakeAdapter):
        def deploy_unit(self, unit):
            raise RuntimeError("boom")

    adapter = Exploding([unit("a")])
    orchestrator = Orchestrator(adapter)
    report = orchestrator.run(orchestrator.plan("."))

    assert report.results[0].status == UnitStatus.FAILED
    assert "boom" in report.results[0].detail


def test_status_callback_sees_every_transition():
    seen = []
    adapter = FakeAdapter([unit("a")])
    orchestrator = Orchestrator(adapter)
    orchestrator.run(orchestrator.plan("."), on_status=lambda name, status: seen.append((name, status)))

    assert seen[0] == ("a", UnitStatus.PENDING)
    assert ("a", UnitStatus.IN_PROGRESS) in seen
    assert seen[-1] == ("a", UnitStatus.COMPLETE)


def test_dry_run_never_calls_the_adapter():
    class NeverDeploy(FakeAdapter):
        def deploy_unit(self, unit):
            raise AssertionError("dry run must not reach the backend")

    adapter = NeverDeploy(diamond())
    orchestrator = Orchestrator(adapter)
    report = orchestrator.run(orchestrator.plan("."), dry_run=True)

    assert report.dry_run
    assert report.succeeded


def test_results_are_reported_in_plan_order_not_completion_order():
    adapter = FakeAdapter(diamond(), delay=0.05)
    orchestrator = Orchestrator(adapter)
    report = orchestrator.run(orchestrator.plan("."))
    assert [result.unit for result in report.results] == ["a", "b", "c", "d"]


# -- critical path ----------------------------------------------------------


def test_longest_path_weights_nodes_not_edges():
    graph = nx.DiGraph([("a", "b"), ("b", "d"), ("a", "c"), ("c", "d")])
    weights = {"a": 1.0, "b": 5.0, "c": 1.0, "d": 1.0}
    path, total = longest_path(graph, lambda name: weights[name])

    assert path == ["a", "b", "d"]
    assert total == pytest.approx(7.0)


def test_longest_path_on_an_empty_graph():
    assert longest_path(nx.DiGraph(), lambda _: 1.0) == ([], 0.0)


def test_plan_reports_the_structural_critical_path():
    plan = Orchestrator(FakeAdapter(diamond())).plan(".")
    # a -> (b|c) -> d: three units deep whichever branch is taken.
    assert len(plan.critical_path) == 3
    assert plan.critical_path[0] == "a"
    assert plan.critical_path[-1] == "d"


def test_plan_reports_peak_concurrency():
    assert Orchestrator(FakeAdapter(diamond())).plan(".").max_concurrency == 2


# -- rolling vs waves -------------------------------------------------------


class VariableAdapter(FakeAdapter):
    """Deployments whose duration differs per unit, to expose wave barriers."""

    def __init__(self, units, durations):
        super().__init__(units)
        self._durations = durations
        self.started_at: dict[str, float] = {}
        self.origin = time.monotonic()

    def deploy_unit(self, unit):
        started = time.monotonic()
        with self._lock:
            self.started_at[unit.name] = started - self.origin
        time.sleep(self._durations.get(unit.name, 0.0))
        return DeploymentResult.timed(unit.name, UnitStatus.COMPLETE, started, "fake")


def barrier_shaped():
    """One slow unit and one fast unit in the first wave.

    `codebuild` depends only on the fast `ecr`, so a wave barrier makes it wait
    for the unrelated slow `vpc` before it can start.
    """
    units = [
        unit("vpc", provides=["V"]),
        unit("ecr", provides=["E"]),
        unit("codebuild", provides=["C"], requires=["E"]),
        unit("iam", requires=["C"]),
        unit("subnet", requires=["V"]),
    ]
    durations = {"vpc": 0.6, "ecr": 0.05, "codebuild": 0.05, "iam": 0.05, "subnet": 0.05}
    return units, durations


def test_rolling_starts_a_unit_without_waiting_for_an_unrelated_slow_sibling():
    units, durations = barrier_shaped()
    adapter = VariableAdapter(units, durations)
    orchestrator = Orchestrator(adapter, max_parallel=8, strategy="rolling")
    orchestrator.run(orchestrator.plan("."))

    # codebuild only needs ecr (0.05s). It must not have waited on vpc (0.6s).
    assert adapter.started_at["codebuild"] < 0.4


def test_waves_makes_that_same_unit_wait_for_the_barrier():
    units, durations = barrier_shaped()
    adapter = VariableAdapter(units, durations)
    orchestrator = Orchestrator(adapter, max_parallel=8, strategy="waves")
    orchestrator.run(orchestrator.plan("."))

    # This is the cost the rolling scheduler removes, stated as a test rather
    # than asserted in prose.
    assert adapter.started_at["codebuild"] >= 0.5


def test_rolling_reaches_the_critical_path_floor():
    units, durations = barrier_shaped()
    orchestrator = Orchestrator(VariableAdapter(units, durations), max_parallel=8)
    report = orchestrator.run(orchestrator.plan("."))

    # Nothing can be faster than the critical path; rolling should be close to it.
    assert report.wall_clock >= report.critical_path_duration - 0.01
    assert report.efficiency > 0.85


def test_max_parallel_one_degrades_to_a_sequential_run():
    units, durations = barrier_shaped()
    orchestrator = Orchestrator(VariableAdapter(units, durations), max_parallel=1)
    report = orchestrator.run(orchestrator.plan("."))

    assert report.efficiency < 1.0
    assert report.wall_clock >= report.sequential_estimate - 0.05


# -- robustness -------------------------------------------------------------


def test_duplicate_unit_names_are_rejected_rather_than_collapsed():
    # Two units on one graph node would look like a complete plan while one of
    # them never deployed.
    units = [unit("vpc"), unit("vpc")]
    with pytest.raises(PlanError, match="share a name"):
        Orchestrator(FakeAdapter(units)).plan(".")


def test_a_failure_skips_the_whole_downstream_chain_not_just_the_next_unit():
    units = [
        unit("a", provides=["A"]),
        unit("b", provides=["B"], requires=["A"]),
        unit("c", requires=["B"]),
    ]
    adapter = FakeAdapter(units, failures={"a"})
    orchestrator = Orchestrator(adapter)
    report = orchestrator.run(orchestrator.plan("."))

    statuses = {result.unit: result.status for result in report.results}
    assert statuses == {
        "a": UnitStatus.FAILED,
        "b": UnitStatus.SKIPPED,
        "c": UnitStatus.SKIPPED,  # transitively, not only the direct successor
    }


def test_cancelling_stops_scheduling_and_still_reports_every_unit():
    units = [unit(name) for name in "abcdef"]
    adapter = FakeAdapter(units, delay=0.1)
    orchestrator = Orchestrator(adapter, max_parallel=2)
    plan = orchestrator.plan(".")

    def cancel_soon(name, status):
        if status == UnitStatus.IN_PROGRESS:
            orchestrator.cancel()

    report = orchestrator.run(plan, on_status=cancel_soon)

    assert report.cancelled
    # Every unit has a row: a report that omits units is worse than a bad one.
    assert len(report.results) == len(units)
    assert any("cancelled" in result.detail for result in report.results)


def test_both_strategies_agree_on_the_final_statuses():
    units, durations = barrier_shaped()
    outcomes = []
    for strategy in ("rolling", "waves"):
        orchestrator = Orchestrator(VariableAdapter(units, durations), strategy=strategy)
        report = orchestrator.run(orchestrator.plan("."))
        outcomes.append({result.unit: result.status for result in report.results})

    assert outcomes[0] == outcomes[1]


def test_an_unknown_strategy_is_rejected_at_construction():
    with pytest.raises(ValueError, match="unknown strategy"):
        Orchestrator(FakeAdapter([]), strategy="greedy")


def test_report_serialises_for_ci():
    adapter = FakeAdapter(diamond(), delay=0.02)
    orchestrator = Orchestrator(adapter)
    payload = orchestrator.run(orchestrator.plan(".")).to_dict()

    assert payload["strategy"] == "rolling"
    assert payload["succeeded"] is True
    assert payload["critical_path"][0] == "a"
    assert len(payload["units"]) == 4
    assert set(payload["units"][0]) == {"name", "status", "duration", "detail"}
