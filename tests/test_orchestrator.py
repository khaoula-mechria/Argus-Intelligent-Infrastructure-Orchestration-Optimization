"""Tests for the backend-neutral orchestrator, driven by a fake adapter."""

from __future__ import annotations

import threading
import time

import pytest

from argus.adapters.base import DeployableUnit, DeploymentResult, InfrastructureAdapter, UnitStatus
from argus.orchestrator import Orchestrator, PlanError


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
