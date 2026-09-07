"""Tests for the Terraform adapter.

The ``terraform`` binary is not required: every process call goes through the
adapter's runner seam, and the DOT parsing is checked against a captured
``terraform graph -type=plan`` output.
"""

from __future__ import annotations

import os
import subprocess

import networkx as nx
import pytest

from argus.adapters.base import AdapterError, UnitStatus
from argus.adapters.terraform import TerraformAdapter, parse_terraform_graph
from argus.config import ModuleSpec, parse_config_file
from argus.orchestrator import Orchestrator

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO_ROOT, "examples", "terraform")
FIXTURE = os.path.join(HERE, "fixtures", "terraform-graph-plan.dot")


def fake_runner(stdout="", returncode=0, stderr="", record=None):
    def run(arguments, cwd):
        if record is not None:
            record.append((list(arguments), cwd))
        return subprocess.CompletedProcess(
            args=list(arguments), returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


# -- DOT parsing ------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed_graph():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return parse_terraform_graph(handle.read())


def test_resources_survive_the_parse(parsed_graph):
    assert "terraform_data.vpc" in parsed_graph
    assert "terraform_data.subnet_a" in parsed_graph
    assert "data.terraform_remote_state.shared" in parsed_graph


def test_run_bookkeeping_nodes_are_dropped(parsed_graph):
    # Providers, the root node and the close nodes crowd out the resources
    # the reader actually came for.
    assert not any(name.startswith("provider") for name in parsed_graph)
    assert "root" not in parsed_graph


def test_the_expand_suffix_is_stripped(parsed_graph):
    assert not any("(expand)" in name for name in parsed_graph)


def test_edges_are_reversed_into_creation_order(parsed_graph):
    # terraform draws subnet_a -> vpc to mean "subnet_a depends on vpc".
    # Argus points from provider to consumer, so a topological order is a
    # creation order.
    assert parsed_graph.has_edge("terraform_data.vpc", "terraform_data.subnet_a")
    assert not parsed_graph.has_edge("terraform_data.subnet_a", "terraform_data.vpc")


def test_the_parsed_graph_orders_the_vpc_before_its_subnets(parsed_graph):
    order = list(nx.topological_sort(parsed_graph))
    assert order.index("terraform_data.vpc") < order.index("terraform_data.subnet_a")
    assert order.index("terraform_data.vpc") < order.index("terraform_data.subnet_b")


def test_node_kinds_are_labelled(parsed_graph):
    assert parsed_graph.nodes["terraform_data.vpc"]["kind"] == "resource"
    assert parsed_graph.nodes["data.terraform_remote_state.shared"]["kind"] == "data"
    assert parsed_graph.nodes["var.region"]["kind"] == "variable"
    assert parsed_graph.nodes["output.vpc_id"]["kind"] == "output"


def test_empty_output_yields_an_empty_graph():
    assert parse_terraform_graph("   ").number_of_nodes() == 0


# -- discovery --------------------------------------------------------------


def test_a_single_root_module_is_one_unit(tmp_path):
    (tmp_path / "main.tf").write_text('resource "terraform_data" "a" {}\n', encoding="utf-8")
    units = TerraformAdapter().discover_units(str(tmp_path))

    assert len(units) == 1
    assert units[0].metadata["single_module"] is True
    assert units[0].metadata["graph_source"] == "terraform graph -type=plan"


def test_a_directory_without_tf_files_says_what_to_do(tmp_path):
    with pytest.raises(AdapterError, match="argus.yaml"):
        TerraformAdapter().discover_units(str(tmp_path))


def test_declared_modules_become_units_with_their_declared_order():
    config = parse_config_file(os.path.join(EXAMPLE, "argus.yaml"))
    adapter = TerraformAdapter(modules=config.modules)
    plan = Orchestrator(adapter).plan(EXAMPLE)

    assert plan.waves == [["network"], ["app"]]
    assert plan.graph.has_edge("network", "app")
    assert plan.units[1].metadata["variables"] == {"environment": "dev"}


def test_a_module_pointing_at_a_missing_directory_is_an_error():
    adapter = TerraformAdapter(modules=[ModuleSpec(name="ghost", path="/no/such/module")])
    with pytest.raises(AdapterError, match="not a directory"):
        adapter.discover_units(".")


def test_the_example_project_really_contains_two_modules():
    # The README quickstart points at this directory, so it has to exist.
    assert os.path.isfile(os.path.join(EXAMPLE, "network", "main.tf"))
    assert os.path.isfile(os.path.join(EXAMPLE, "app", "main.tf"))


# -- deployment -------------------------------------------------------------


def test_apply_passes_the_declared_variables_and_var_files(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    calls: list[tuple[list[str], str]] = []
    adapter = TerraformAdapter(
        modules=[
            ModuleSpec(
                name="app",
                path=str(tmp_path),
                variables={"environment": "dev"},
                var_files=["prod.tfvars"],
            )
        ],
        runner=fake_runner(stdout="Apply complete! Resources: 2 added.", record=calls),
    )
    unit = adapter.discover_units(".")[0]
    result = adapter.deploy_unit(unit)

    assert result.status == UnitStatus.COMPLETE
    assert result.detail == "Apply complete! Resources: 2 added."

    init_args, apply_args = calls[0][0], calls[1][0]
    assert init_args[0] == "init"
    assert apply_args[:2] == ["apply", "-auto-approve"]
    assert "-var-file=prod.tfvars" in apply_args
    assert apply_args[apply_args.index("-var") + 1] == "environment=dev"


def test_a_failed_init_does_not_run_apply(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    calls: list[tuple[list[str], str]] = []
    adapter = TerraformAdapter(
        runner=fake_runner(returncode=1, stderr="Error: backend not configured", record=calls)
    )
    unit = adapter.discover_units(str(tmp_path))[0]
    result = adapter.deploy_unit(unit)

    assert result.status == UnitStatus.FAILED
    assert "backend not configured" in result.detail
    assert [call[0][0] for call in calls] == ["init"]


def test_a_failed_apply_reports_the_end_of_the_output(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")

    def run(arguments, cwd):
        failed = arguments[0] == "apply"
        return subprocess.CompletedProcess(
            args=list(arguments),
            returncode=1 if failed else 0,
            stdout="",
            stderr="noise\n\nError: creating instance: quota exceeded" if failed else "",
        )

    adapter = TerraformAdapter(runner=run)
    unit = adapter.discover_units(str(tmp_path))[0]
    result = adapter.deploy_unit(unit)

    assert result.status == UnitStatus.FAILED
    assert "quota exceeded" in result.detail


def test_a_missing_terraform_binary_is_a_readable_error(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    adapter = TerraformAdapter(binary="terraform-that-does-not-exist")
    unit = adapter.discover_units(str(tmp_path))[0]

    with pytest.raises(AdapterError, match="was not found on PATH"):
        adapter._run_terraform(["version"], cwd=str(tmp_path))


# -- status and inspection --------------------------------------------------


def test_status_is_complete_when_the_state_holds_resources(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    adapter = TerraformAdapter(runner=fake_runner(stdout="terraform_data.vpc\n"))
    assert adapter.get_unit_status(adapter.discover_units(str(tmp_path))[0]) == UnitStatus.COMPLETE


def test_status_is_pending_on_an_empty_state(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    adapter = TerraformAdapter(runner=fake_runner(stdout="\n"))
    assert adapter.get_unit_status(adapter.discover_units(str(tmp_path))[0]) == UnitStatus.PENDING


def test_inspect_unit_returns_terraforms_own_graph(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        dot = handle.read()

    adapter = TerraformAdapter(runner=fake_runner(stdout=dot))
    graph = adapter.inspect_unit(adapter.discover_units(str(tmp_path))[0])

    assert graph is not None
    assert graph.has_edge("terraform_data.vpc", "terraform_data.subnet_a")


def test_inspect_unit_returns_none_when_terraform_cannot_run(tmp_path):
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    adapter = TerraformAdapter(runner=fake_runner(returncode=1, stderr="not initialised"))
    assert adapter.inspect_unit(adapter.discover_units(str(tmp_path))[0]) is None


def test_a_cloudformation_adapter_exposes_no_inner_graph():
    # inspect_unit is optional; only backends that schedule work inside a unit
    # have something to show.
    from argus.adapters.cloudformation import CloudFormationAdapter

    adapter = CloudFormationAdapter()
    units = adapter.discover_units(os.path.join(REPO_ROOT, "infrastructure", "cloudformation"))
    assert adapter.inspect_unit(units[0]) is None


def test_pointing_at_one_declared_module_deploys_only_that_module():
    config = parse_config_file(os.path.join(EXAMPLE, "argus.yaml"))
    adapter = TerraformAdapter(modules=config.modules)
    units = adapter.discover_units(os.path.join(EXAMPLE, "app"))

    assert [unit.name for unit in units] == ["app"]
    # No edge to `network` is claimed: that module is not part of this run.
    assert units[0].requires == frozenset()
    assert units[0].metadata["single_module"] is True


def test_pointing_at_the_project_root_deploys_every_declared_module():
    config = parse_config_file(os.path.join(EXAMPLE, "argus.yaml"))
    units = TerraformAdapter(modules=config.modules).discover_units(EXAMPLE)
    assert sorted(unit.name for unit in units) == ["app", "network"]
