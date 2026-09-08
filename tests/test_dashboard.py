"""Tests for the pure rendering helpers of the Streamlit front end."""

from __future__ import annotations

import os

import pytest

from argus.adapters.base import DeployableUnit, InfrastructureAdapter, UnitStatus
from argus.dashboard import build_dot
from argus.orchestrator import Orchestrator


class TinyAdapter(InfrastructureAdapter):
    backend = "fake"

    def __init__(self, units):
        self._units = units

    def discover_units(self, path):
        return list(self._units)

    def deploy_unit(self, unit):  # pragma: no cover - never called here
        raise NotImplementedError

    def get_unit_status(self, unit):  # pragma: no cover - never called here
        return UnitStatus.UNKNOWN


def unit(name, provides=(), requires=()):
    return DeployableUnit(
        name=name,
        path=name,
        backend="fake",
        provides=frozenset(provides),
        requires=frozenset(requires),
    )


def build_plan(extra=None):
    units = [unit("vpc", provides=["V"]), unit("app", requires=["V"]), unit("alb")]
    return Orchestrator(TinyAdapter(units), extra_dependencies=extra or {}).plan(".")


def test_dot_groups_nodes_by_wave():
    dot = build_dot(build_plan(), {})
    assert "subgraph cluster_wave_0" in dot
    assert 'label="wave 1"' in dot
    assert '"vpc" -> "app"' in dot


def test_nodes_are_coloured_by_status():
    plan = build_plan()
    dot = build_dot(plan, {"vpc": UnitStatus.COMPLETE, "app": UnitStatus.FAILED})

    complete_fill = "#ceead6"
    failed_fill = "#fad2cf"
    assert '"vpc" [fillcolor="' + complete_fill in dot
    assert '"app" [fillcolor="' + failed_fill in dot


def test_edges_into_an_in_progress_unit_are_highlighted():
    plan = build_plan()
    dot = build_dot(plan, {"app": UnitStatus.IN_PROGRESS})

    edge = next(line for line in dot.splitlines() if '"vpc" -> "app"' in line)
    assert "penwidth=2.5" in edge


def test_an_idle_edge_is_not_highlighted():
    dot = build_dot(build_plan(), {"app": UnitStatus.PENDING})
    edge = next(line for line in dot.splitlines() if '"vpc" -> "app"' in line)
    assert "penwidth" not in edge


def test_a_declared_edge_is_drawn_differently_from_an_inferred_one():
    # A reader must be able to tell an edge Argus discovered from one a human
    # asserted in argus.yaml.
    dot = build_dot(build_plan(extra={"alb": ["vpc"]}), {})
    edge = next(line for line in dot.splitlines() if '"vpc" -> "alb"' in line)

    assert "style=dashed" in edge
    assert 'label="argus.yaml"' in edge


def test_every_unit_appears_exactly_once():
    dot = build_dot(build_plan(), {})
    for name in ("vpc", "app", "alb"):
        assert dot.count('"' + name + '" [') == 1


# -- the page itself --------------------------------------------------------

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "argus", "dashboard.py")


@pytest.fixture(scope="module")
def app():
    """Run the real Streamlit script against this repository's templates."""
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    return at


def test_the_page_loads_without_an_exception(app):
    assert app.exception == []
    assert [element.value for element in app.title] == ["Argus"]
    assert len(app.tabs) == 3


def test_the_page_shows_the_real_plan_for_this_repository(app):
    metrics = {element.label: element.value for element in app.metric}
    assert metrics["Units"] == "12"
    assert metrics["Waves"] == "7"


def test_the_cloudformation_banner_states_what_argus_contributes(app):
    assert any("no native way" in element.value for element in app.info)


def test_a_dry_run_deployment_completes_and_reports_the_comparison(app):
    # The dry-run toggle defaults to on, so this touches no cloud account.
    button = next(element for element in app.button if element.label == "Deploy")
    result = button.click().run()

    assert result.exception == []
    metrics = {element.label: element.value for element in result.metric}
    assert "Wall clock" in metrics
    assert "Sequential equivalent" in metrics
    # The floor the run is measured against, and how close it got.
    assert "Critical path floor" in metrics
    assert metrics["Efficiency"].endswith("%")


def test_the_page_reports_the_critical_path_not_just_the_wave_count(app):
    metrics = {element.label: element.value for element in app.metric}
    # The wave count flatters the plan; the critical path is the real limit.
    assert metrics["Critical path"] == "7"


def test_the_page_offers_both_scheduling_strategies(app):
    options = [option for element in app.selectbox for option in element.options]
    assert "rolling" in options and "waves" in options


def test_an_endpoint_can_be_pointed_at_localstack():
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()

    endpoint = next(
        element for element in at.text_input if element.label == "Endpoint"
    )
    result = endpoint.set_value("http://localhost:4566").run()

    assert result.exception == []
    # The page must say when it is not talking to AWS.
    assert any("not to AWS" in caption.value for caption in result.caption)
