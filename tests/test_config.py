"""Tests for argus.yaml parsing and discovery."""

from __future__ import annotations

import os

import pytest

from argus.config import ConfigError, find_config, load_config, parse_config, parse_config_file

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_absent_config_is_not_an_error(tmp_path):
    config = load_config(str(tmp_path))
    assert config.is_empty
    assert config.parameters == {}
    assert config.max_parallel == 8


def test_config_is_found_from_a_nested_directory(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "argus.yaml").write_text("backend: cloudformation\n", encoding="utf-8")
    nested = tmp_path / "infra" / "cloudformation"
    nested.mkdir(parents=True)

    assert find_config(str(nested)) == str(tmp_path / "argus.yaml")


def test_search_stops_at_the_repository_root(tmp_path):
    # An argus.yaml above the repo root belongs to another project.
    (tmp_path / "argus.yaml").write_text("backend: terraform\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert find_config(str(repo)) is None


def test_this_repository_config_is_usable():
    config = parse_config_file(os.path.join(REPO_ROOT, "argus.yaml"))
    assert config.backend == "cloudformation"
    assert config.parameters["ProjectName"] == "taskmanager"
    # alb.yaml takes the VPC id as a parameter, so this edge cannot be inferred.
    assert config.depends_on == {"alb": ["vpc"]}


def test_parameters_are_coerced_to_strings():
    # CloudFormation rejects a non-string ParameterValue, and YAML turns
    # `DesiredCount: 2` into an int without asking.
    config = parse_config({"parameters": {"DesiredCount": 2, "Enabled": True}})
    assert config.parameters == {"DesiredCount": "2", "Enabled": "true"}


def test_module_paths_resolve_against_the_config_file(tmp_path):
    (tmp_path / "argus.yaml").write_text(
        "backend: terraform\nmodules:\n  - name: network\n    path: ./network\n", encoding="utf-8"
    )
    config = parse_config_file(str(tmp_path / "argus.yaml"))
    assert config.modules[0].path == os.path.normpath(str(tmp_path / "network"))


def test_a_module_string_entry_takes_its_name_from_the_path():
    config = parse_config({"modules": ["./infra/network"]})
    assert config.modules[0].name == "network"


def test_module_dependency_on_an_undeclared_module_is_rejected():
    with pytest.raises(ConfigError, match="undeclared"):
        parse_config({"modules": [{"name": "app", "path": "./app", "depends_on": ["network"]}]})


def test_duplicate_module_names_are_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({"modules": [{"name": "a", "path": "./x"}, {"name": "a", "path": "./y"}]})


def test_module_without_a_path_is_rejected():
    with pytest.raises(ConfigError, match="path"):
        parse_config({"modules": [{"name": "a"}]})


def test_bad_max_parallel_is_rejected():
    with pytest.raises(ConfigError, match="max_parallel"):
        parse_config({"max_parallel": 0})


def test_bad_depends_on_shape_is_rejected():
    with pytest.raises(ConfigError, match="depends_on"):
        parse_config({"depends_on": ["alb"]})


def test_depends_on_accepts_a_bare_string():
    assert parse_config({"depends_on": {"alb": "vpc"}}).depends_on == {"alb": ["vpc"]}
