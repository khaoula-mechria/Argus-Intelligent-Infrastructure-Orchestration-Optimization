"""Tests for the CloudFormation adapter, including a run on this repository."""

from __future__ import annotations

import os

import networkx as nx
import pytest

from argus.adapters.base import AdapterError
from argus.adapters.cloudformation import (
    CloudFormationAdapter,
    extract_exports,
    extract_imports,
    load_template,
    resolve_name,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO_ROOT, "infrastructure", "cloudformation")


# -- name resolution --------------------------------------------------------


def test_resolve_literal_name():
    assert resolve_name("taskmanager-dev-vpc-id") == "taskmanager-dev-vpc-id"


def test_resolve_sub_keeps_unknown_placeholders():
    node = {"Fn::Sub": "${ProjectName}-${Environment}-vpc-id"}
    assert resolve_name(node) == "${ProjectName}-${Environment}-vpc-id"


def test_resolve_sub_substitutes_known_parameters():
    node = {"Fn::Sub": "${ProjectName}-${Environment}-vpc-id"}
    parameters = {"ProjectName": "taskmanager", "Environment": "dev"}
    assert resolve_name(node, parameters) == "taskmanager-dev-vpc-id"


def test_resolve_sub_with_inline_substitution_map():
    node = {"Fn::Sub": ["${Prefix}-vpc-id", {"Prefix": "taskmanager-dev"}]}
    assert resolve_name(node) == "taskmanager-dev-vpc-id"


def test_resolve_sub_honours_the_literal_escape():
    assert resolve_name({"Fn::Sub": "${!NotAParameter}"}) == "${NotAParameter}"


def test_resolve_join():
    node = {"Fn::Join": ["-", [{"Ref": "ProjectName"}, "dev", "vpc-id"]]}
    assert resolve_name(node, {"ProjectName": "taskmanager"}) == "taskmanager-dev-vpc-id"


def test_resolve_returns_none_for_an_unreducible_expression():
    # A GetAtt cannot be reduced to a name without deploying, so the adapter
    # must report nothing rather than invent a dependency.
    assert resolve_name({"Fn::GetAtt": ["Resource", "Arn"]}) is None


# -- template parsing -------------------------------------------------------


SHORT_FORM = """
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub '${ProjectName}-bucket'
      Tags:
        - Key: Vpc
          Value: !ImportValue
            'Fn::Sub': '${ProjectName}-vpc-id'
Outputs:
  BucketName:
    Value: !Ref Bucket
    Export:
      Name: !Sub '${ProjectName}-bucket-name'
"""

LONG_FORM = """
Resources:
  Bucket:
    Type: AWS::S3::Bucket
    Properties:
      Tags:
        - Key: Vpc
          Value:
            Fn::ImportValue:
              Fn::Sub: '${ProjectName}-vpc-id'
Outputs:
  BucketName:
    Value:
      Ref: Bucket
    Export:
      Name:
        Fn::Sub: '${ProjectName}-bucket-name'
"""


@pytest.mark.parametrize("body", [SHORT_FORM, LONG_FORM], ids=["short-form", "long-form"])
def test_both_intrinsic_notations_yield_the_same_names(tmp_path, body):
    path = tmp_path / "stack.yaml"
    path.write_text(body, encoding="utf-8")
    template = load_template(str(path))

    assert extract_exports(template) == {"${ProjectName}-bucket-name"}
    assert extract_imports(template) == {"${ProjectName}-vpc-id"}


def test_getatt_short_form_becomes_a_list(tmp_path):
    path = tmp_path / "stack.yaml"
    path.write_text("Resources:\n  A:\n    Value: !GetAtt Thing.Arn\n", encoding="utf-8")
    assert load_template(str(path))["Resources"]["A"]["Value"] == {"Fn::GetAtt": ["Thing", "Arn"]}


def test_invalid_yaml_is_reported_with_the_path(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("Resources: [unclosed\n", encoding="utf-8")
    with pytest.raises(AdapterError) as excinfo:
        load_template(str(path))
    assert "broken.yaml" in str(excinfo.value)


# -- discovery and graph ----------------------------------------------------


def test_discovery_skips_files_without_resources(tmp_path):
    (tmp_path / "stack.yaml").write_text(
        "Resources:\n  A:\n    Type: AWS::S3::Bucket\n", encoding="utf-8"
    )
    (tmp_path / "params.yaml").write_text("ProjectName: taskmanager\n", encoding="utf-8")

    units = CloudFormationAdapter().discover_units(str(tmp_path))
    assert [unit.name for unit in units] == ["stack"]


def test_discovery_on_an_empty_directory_raises(tmp_path):
    with pytest.raises(AdapterError):
        CloudFormationAdapter().discover_units(str(tmp_path))


def test_graph_links_exporter_to_importer(tmp_path):
    (tmp_path / "net.yaml").write_text(
        "Resources:\n  V:\n    Type: AWS::EC2::VPC\n"
        "Outputs:\n  VpcId:\n    Value: !Ref V\n    Export:\n      Name: proj-vpc-id\n",
        encoding="utf-8",
    )
    (tmp_path / "app.yaml").write_text(
        "Resources:\n  S:\n    Type: AWS::EC2::SecurityGroup\n"
        "    Properties:\n      VpcId: !ImportValue proj-vpc-id\n",
        encoding="utf-8",
    )

    adapter = CloudFormationAdapter()
    units = adapter.discover_units(str(tmp_path))
    graph = adapter.build_dependency_graph(units)

    assert graph.has_edge("net", "app")
    assert graph["net"]["app"]["via"] == ["proj-vpc-id"]
    assert not graph.has_edge("app", "net")


def test_a_stack_importing_its_own_export_creates_no_self_loop(tmp_path):
    (tmp_path / "solo.yaml").write_text(
        "Resources:\n  A:\n    Type: AWS::S3::Bucket\n"
        "    Properties:\n      Name: !ImportValue proj-thing\n"
        "Outputs:\n  Thing:\n    Value: x\n    Export:\n      Name: proj-thing\n",
        encoding="utf-8",
    )
    adapter = CloudFormationAdapter()
    graph = adapter.build_dependency_graph(adapter.discover_units(str(tmp_path)))
    assert list(nx.selfloop_edges(graph)) == []


def test_unresolved_requirements_are_reported(tmp_path):
    (tmp_path / "app.yaml").write_text(
        "Resources:\n  S:\n    Type: AWS::EC2::SecurityGroup\n"
        "    Properties:\n      VpcId: !ImportValue somewhere-else-vpc-id\n",
        encoding="utf-8",
    )
    adapter = CloudFormationAdapter()
    units = adapter.discover_units(str(tmp_path))
    assert adapter.unresolved_requirements(units) == {"app": ["somewhere-else-vpc-id"]}


# -- against this repository's own templates --------------------------------


@pytest.fixture(scope="module")
def repo_plan():
    adapter = CloudFormationAdapter({"ProjectName": "taskmanager", "Environment": "dev"})
    units = adapter.discover_units(TEMPLATES)
    return adapter, units, adapter.build_dependency_graph(units)


def test_repo_templates_are_all_discovered(repo_plan):
    _, units, _ = repo_plan
    assert len(units) == 12


def test_repo_graph_is_acyclic(repo_plan):
    _, _, graph = repo_plan
    assert nx.is_directed_acyclic_graph(graph)


def test_repo_graph_has_no_unresolved_imports(repo_plan):
    adapter, units, _ = repo_plan
    # Every Fn::ImportValue in this repo is satisfied by a template in the same
    # directory. A regression here means a template imports something nothing
    # exports, which deploys to "No export named ... found".
    assert adapter.unresolved_requirements(units) == {}


def test_repo_order_respects_the_documented_constraints(repo_plan):
    """The three orderings infrastructure/README.md calls non-negotiable.

    They are stated there as hard-won: putting iam before codebuild really did
    produce "No export named taskmanager-dev-codebuild-arn found".
    """
    _, _, graph = repo_plan
    wave_of = {
        name: index for index, wave in enumerate(nx.topological_generations(graph)) for name in wave
    }

    assert wave_of["codebuild"] < wave_of["iam"]
    assert wave_of["secrets-manager"] < wave_of["codebuild"]
    assert wave_of["secrets-manager"] < wave_of["ecs-task-definition"]
    assert wave_of["ecs-service"] < wave_of["ecs-autoscaling"]
    assert wave_of["pipeline"] < wave_of["ecs-autoscaling"]


def test_repo_parallelism_beats_the_documented_sequence(repo_plan):
    """12 documented sequential steps collapse into fewer waves."""
    _, units, graph = repo_plan
    waves = list(nx.topological_generations(graph))
    assert len(waves) < len(units)


def test_parameters_are_filtered_to_what_each_template_declares():
    adapter = CloudFormationAdapter(
        {"ProjectName": "taskmanager", "Environment": "dev", "NotDeclaredAnywhere": "x"}
    )
    units = adapter.discover_units(TEMPLATES)
    # Passing a parameter a template does not declare makes CreateStack fail
    # with a ValidationError, so the adapter must drop it per stack.
    assert all("NotDeclaredAnywhere" not in unit.parameters for unit in units)
    assert any("ProjectName" in unit.parameters for unit in units)
