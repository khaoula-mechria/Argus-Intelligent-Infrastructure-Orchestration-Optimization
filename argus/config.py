"""Loading of the optional ``argus.yaml`` project file.

The file is genuinely optional for CloudFormation: exports and imports carry
the dependency information on their own. It becomes mandatory for Terraform as
soon as a project has more than one root module, because two separate states
share no information Argus could read.

It is also the escape hatch for a CloudFormation dependency that exists in
reality but not in the templates -- a value passed as a stack parameter
instead of ``Fn::ImportValue``. Argus never guesses those; you declare them
under ``depends_on``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

CONFIG_FILENAMES = ("argus.yaml", "argus.yml")


class ConfigError(ValueError):
    """Raised when ``argus.yaml`` exists but cannot be used as written."""


@dataclass
class ModuleSpec:
    """One Terraform root module declared in ``argus.yaml``."""

    name: str
    path: str
    depends_on: list[str] = field(default_factory=list)
    var_files: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class ArgusConfig:
    """Everything ``argus.yaml`` can say about a project.

    Every field has a usable default, so an absent file yields a config that
    behaves exactly like passing nothing on the command line.
    """

    backend: str | None = None
    path: str | None = None
    region: str | None = None
    profile: str | None = None
    #: Alternative service endpoint. Point it at LocalStack to run the whole
    #: tool without a cloud account -- see docs/guide-local.md.
    endpoint_url: str | None = None
    parameters: dict[str, str] = field(default_factory=dict)
    stack_name_prefix: str = ""
    max_parallel: int = 8
    #: Extra edges Argus cannot infer, as ``unit -> [units it needs first]``.
    depends_on: dict[str, list[str]] = field(default_factory=dict)
    #: Terraform root modules; empty means the target path is a single module.
    modules: list[ModuleSpec] = field(default_factory=list)
    source: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.source is None

    def aws_settings(self):
        """The account/endpoint half of this config, for the AWS clients."""
        from .aws import AwsSettings

        return AwsSettings(
            region=self.region, profile=self.profile, endpoint_url=self.endpoint_url
        )


def find_config(path: str) -> str | None:
    """Return the ``argus.yaml`` governing ``path``, if there is one.

    Walks up from ``path`` so both layouts work: the file next to the
    templates, or at the repository root several directories above them. The
    search stops at the repository root -- a directory containing ``.git`` --
    so a stray argus.yaml elsewhere on the machine is never picked up.
    """
    directory = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path)))
    while True:
        for filename in CONFIG_FILENAMES:
            candidate = os.path.join(directory, filename)
            if os.path.isfile(candidate):
                return candidate
        if os.path.isdir(os.path.join(directory, ".git")):
            return None
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def load_config(path: str) -> ArgusConfig:
    """Load the config governing ``path``; an absent file is not an error."""
    config_path = find_config(path)
    if config_path is None:
        return ArgusConfig()
    return parse_config_file(config_path)


def parse_config_file(config_path: str) -> ArgusConfig:
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError(config_path + ": top level must be a mapping")
    config = parse_config(raw, base_dir=os.path.dirname(os.path.abspath(config_path)))
    config.source = config_path
    return config


def parse_config(raw: dict[str, Any], base_dir: str = ".") -> ArgusConfig:
    config = ArgusConfig()

    config.backend = _optional_str(raw, "backend")
    config.region = _optional_str(raw, "region")
    config.profile = _optional_str(raw, "profile")
    config.endpoint_url = _optional_str(raw, "endpoint_url")
    config.stack_name_prefix = _optional_str(raw, "stack_name_prefix") or ""

    target = _optional_str(raw, "path")
    if target:
        config.path = target if os.path.isabs(target) else os.path.normpath(os.path.join(base_dir, target))

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ConfigError("parameters must be a mapping of name to value")
    # CloudFormation only accepts strings; numbers and booleans in YAML are a
    # common and otherwise silent source of ValidationError at deploy time.
    config.parameters = {str(key): _scalar_to_str(value) for key, value in parameters.items()}

    max_parallel = raw.get("max_parallel")
    if max_parallel is not None:
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise ConfigError("max_parallel must be a positive integer")
        config.max_parallel = max_parallel

    depends_on = raw.get("depends_on") or {}
    if not isinstance(depends_on, dict):
        raise ConfigError("depends_on must be a mapping of unit to a list of units")
    for unit, requirements in depends_on.items():
        config.depends_on[str(unit)] = _as_str_list(requirements, "depends_on." + str(unit))

    modules = raw.get("modules") or []
    if modules:
        if not isinstance(modules, list):
            raise ConfigError("modules must be a list")
        seen: set[str] = set()
        for entry in modules:
            module = _parse_module(entry, base_dir)
            if module.name in seen:
                raise ConfigError("duplicate module name: " + module.name)
            seen.add(module.name)
            config.modules.append(module)
        _check_module_references(config.modules)

    return config


def _parse_module(entry: Any, base_dir: str) -> ModuleSpec:
    if isinstance(entry, str):
        return ModuleSpec(name=os.path.basename(entry.rstrip("/\\")) or entry,
                          path=_resolve(entry, base_dir))
    if not isinstance(entry, dict):
        raise ConfigError("each module must be a string path or a mapping")

    path = entry.get("path")
    if not isinstance(path, str) or not path:
        raise ConfigError("module entry is missing a path")
    name = entry.get("name") or os.path.basename(path.rstrip("/\\")) or path

    variables = entry.get("variables") or {}
    if not isinstance(variables, dict):
        raise ConfigError("module " + str(name) + ": variables must be a mapping")

    return ModuleSpec(
        name=str(name),
        path=_resolve(path, base_dir),
        depends_on=_as_str_list(entry.get("depends_on"), "module " + str(name) + ".depends_on"),
        var_files=[_resolve(f, base_dir) for f in _as_str_list(entry.get("var_files"), "var_files")],
        variables={str(key): _scalar_to_str(value) for key, value in variables.items()},
    )


def _check_module_references(modules: list[ModuleSpec]) -> None:
    """Fail loudly on a dependency pointing at a module that is not declared.

    Silently dropping it would produce a plausible-looking graph with a
    missing edge, which is the one failure mode an ordering tool must not have.
    """
    names = {module.name for module in modules}
    for module in modules:
        unknown = [dep for dep in module.depends_on if dep not in names]
        if unknown:
            raise ConfigError(
                "module " + module.name + " depends on undeclared module(s): " + ", ".join(unknown)
            )


def _resolve(path: str, base_dir: str) -> str:
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(key + " must be a string")
    return value


def _as_str_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ConfigError(label + " must be a string or a list of strings")


def _scalar_to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
