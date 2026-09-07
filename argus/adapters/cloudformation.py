"""CloudFormation backend.

Argus derives the deployment order from the standard AWS cross-stack
convention: a stack that writes ``Outputs.*.Export.Name`` provides a value,
a stack that reads it with ``Fn::ImportValue`` consumes it. This is the case
where Argus adds the most value, because CloudFormation itself has no way to
deploy several independent stacks in parallel while inferring their order.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable

import yaml

from .base import AdapterError, DeployableUnit, DeploymentResult, InfrastructureAdapter, UnitStatus

#: CreateStack refuses an inline body above this size; larger templates must
#: go through S3, which Argus does not manage for you.
MAX_INLINE_TEMPLATE_BYTES = 51_200

TEMPLATE_SUFFIXES = (".yaml", ".yml", ".json", ".template")

#: describe_stacks statuses mapped onto the backend-neutral vocabulary.
_STATUS_MAP = {
    "CREATE_COMPLETE": UnitStatus.COMPLETE,
    "UPDATE_COMPLETE": UnitStatus.COMPLETE,
    "IMPORT_COMPLETE": UnitStatus.COMPLETE,
    "CREATE_IN_PROGRESS": UnitStatus.IN_PROGRESS,
    "UPDATE_IN_PROGRESS": UnitStatus.IN_PROGRESS,
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS": UnitStatus.IN_PROGRESS,
    "REVIEW_IN_PROGRESS": UnitStatus.IN_PROGRESS,
    "DELETE_IN_PROGRESS": UnitStatus.IN_PROGRESS,
}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that understands the CloudFormation short-form intrinsics.

    ``yaml.safe_load`` chokes on ``!Ref`` / ``!Sub`` / ``!ImportValue``. Rather
    than stripping them, every short tag is rewritten into its long form
    (``!Sub x`` becomes ``{"Fn::Sub": "x"}``) so a single walker handles both
    notations, which real templates mix freely.
    """


def _multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict[str, Any]:
    key = tag_suffix if tag_suffix in ("Ref", "Condition") else "Fn::" + tag_suffix

    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        # GetAtt takes a dotted string in short form and a list in long form.
        if key == "Fn::GetAtt" and isinstance(value, str):
            value = value.split(".")
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)

    return {key: value}


_CfnLoader.add_multi_constructor("!", _multi_constructor)


def load_template(path: str) -> dict[str, Any]:
    """Parse a CloudFormation template, YAML or JSON, into a plain dict."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        # _CfnLoader derives from SafeLoader, so no arbitrary object construction.
        parsed = yaml.load(text, Loader=_CfnLoader)
    except yaml.YAMLError as exc:
        raise AdapterError(path + ": not valid YAML/JSON (" + str(exc) + ")") from exc
    if not isinstance(parsed, dict):
        raise AdapterError(path + ": template root is not a mapping")
    return parsed


def resolve_name(node: Any, parameters: dict[str, str] | None = None) -> str | None:
    """Turn an export or import name expression into one canonical string.

    Handles the shapes that actually appear in export names: a literal, a
    ``Fn::Sub`` (with or without its inline substitution map), and a
    ``Fn::Join``. Placeholders whose value is unknown are kept verbatim, which
    is what makes matching work without any AWS call: the exporting and the
    importing template are normalised the same way, so a placeholder-bearing
    name on one side matches the identical string on the other.

    Returns ``None`` for an expression Argus cannot reduce to a name, so the
    caller can report it instead of inventing a wrong dependency.
    """
    parameters = parameters or {}

    if isinstance(node, str):
        return _substitute(node, parameters)

    if not isinstance(node, dict) or len(node) != 1:
        return None

    (key, value), = node.items()

    if key == "Fn::Sub":
        if isinstance(value, str):
            return _substitute(value, parameters)
        if isinstance(value, list) and value:
            template, *rest = value
            local = dict(parameters)
            if rest and isinstance(rest[0], dict):
                for name, sub in rest[0].items():
                    resolved = resolve_name(sub, parameters)
                    if resolved is not None:
                        local[name] = resolved
            return _substitute(template, local) if isinstance(template, str) else None
        return None

    if key == "Fn::Join":
        if isinstance(value, list) and len(value) == 2 and isinstance(value[1], list):
            separator = value[0] if isinstance(value[0], str) else ""
            parts = [resolve_name(part, parameters) for part in value[1]]
            if any(part is None for part in parts):
                return None
            return separator.join(part for part in parts if part is not None)
        return None

    if key == "Ref":
        if not isinstance(value, str):
            return None
        return parameters.get(value, "${" + value + "}")

    return None


def _substitute(template: str, parameters: dict[str, str]) -> str:
    """Replace a ${Name} placeholder with its value when known, keep it otherwise."""
    out: list[str] = []
    index = 0
    while index < len(template):
        start = template.find("${", index)
        if start == -1:
            out.append(template[index:])
            break
        end = template.find("}", start)
        if end == -1:
            out.append(template[index:])
            break
        out.append(template[index:start])
        name = template[start + 2 : end]
        if name.startswith("!"):
            # ${!Literal} is CloudFormation's escape for a literal dollar-brace.
            out.append("${" + name[1:] + "}")
        else:
            out.append(parameters.get(name, "${" + name + "}"))
        index = end + 1
    return "".join(out)


def _walk(node: Any):
    """Yield every nested node of a parsed template, depth first."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def extract_exports(template: dict[str, Any], parameters: dict[str, str] | None = None) -> set[str]:
    """Export names published by the Outputs section of a template."""
    outputs = template.get("Outputs") or {}
    if not isinstance(outputs, dict):
        return set()
    exports: set[str] = set()
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        export = output.get("Export")
        if not isinstance(export, dict):
            continue
        name = resolve_name(export.get("Name"), parameters)
        if name:
            exports.add(name)
    return exports


def extract_imports(template: dict[str, Any], parameters: dict[str, str] | None = None) -> set[str]:
    """Export names consumed anywhere in a template through Fn::ImportValue."""
    imports: set[str] = set()
    for node in _walk(template):
        if isinstance(node, dict) and "Fn::ImportValue" in node:
            name = resolve_name(node["Fn::ImportValue"], parameters)
            if name:
                imports.add(name)
    return imports


def template_parameter_names(template: dict[str, Any]) -> set[str]:
    parameters = template.get("Parameters") or {}
    return set(parameters) if isinstance(parameters, dict) else set()


class CloudFormationAdapter(InfrastructureAdapter):
    """Discover, order and deploy CloudFormation stacks.

    ``parameters`` are the values passed to every stack that declares them;
    they also resolve the placeholders in export names, so passing them makes
    the graph keys match the real AWS export names. Leaving them out still
    produces a correct graph, the keys just keep their placeholders.
    """

    backend = "cloudformation"

    def __init__(
        self,
        parameters: dict[str, str] | None = None,
        *,
        stack_name_prefix: str = "",
        region: str | None = None,
        session: Any = None,
        capabilities: Iterable[str] = ("CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"),
        poll_interval: int = 10,
        timeout: int = 3600,
    ) -> None:
        self.parameters = dict(parameters or {})
        self.stack_name_prefix = stack_name_prefix
        self.region = region
        self.capabilities = list(capabilities)
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._session = session
        self._client = None

    # -- discovery ---------------------------------------------------------

    def discover_units(self, path: str) -> list[DeployableUnit]:
        if os.path.isfile(path):
            files = [path]
        elif os.path.isdir(path):
            files = sorted(
                os.path.join(path, entry)
                for entry in os.listdir(path)
                if entry.lower().endswith(TEMPLATE_SUFFIXES)
            )
        else:
            raise AdapterError(path + ": no such file or directory")

        units: list[DeployableUnit] = []
        for file_path in files:
            template = load_template(file_path)
            if "Resources" not in template:
                continue  # not a stack: parameter file, config, ...
            name = os.path.splitext(os.path.basename(file_path))[0]
            units.append(
                DeployableUnit(
                    name=name,
                    path=file_path,
                    backend=self.backend,
                    provides=frozenset(extract_exports(template, self.parameters)),
                    requires=frozenset(extract_imports(template, self.parameters)),
                    parameters={
                        key: value
                        for key, value in self.parameters.items()
                        if key in template_parameter_names(template)
                    },
                    metadata={
                        "stack_name": self.stack_name_prefix + name,
                        "description": template.get("Description", ""),
                        "resource_count": len(template.get("Resources") or {}),
                    },
                )
            )

        if not units:
            raise AdapterError(path + ": no CloudFormation template with a Resources section found")
        return units

    # -- deployment --------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            import boto3

            session = self._session or boto3.session.Session(region_name=self.region)
            self._client = session.client("cloudformation")
        return self._client

    def deploy_unit(self, unit: DeployableUnit) -> DeploymentResult:
        started = time.monotonic()
        stack_name = unit.metadata.get("stack_name", unit.name)

        with open(unit.path, "r", encoding="utf-8") as handle:
            body = handle.read()
        if len(body.encode("utf-8")) > MAX_INLINE_TEMPLATE_BYTES:
            return DeploymentResult.timed(
                unit.name,
                UnitStatus.FAILED,
                started,
                "template exceeds " + str(MAX_INLINE_TEMPLATE_BYTES) + " bytes; upload it to S3 and "
                "deploy it with TemplateURL (Argus does not manage that bucket)",
            )

        request = {
            "StackName": stack_name,
            "TemplateBody": body,
            "Parameters": [
                {"ParameterKey": key, "ParameterValue": value}
                for key, value in sorted(unit.parameters.items())
            ],
            "Capabilities": self.capabilities,
        }

        try:
            if self._stack_exists(stack_name):
                self.client.update_stack(**request)
                action = "update"
            else:
                self.client.create_stack(OnFailure="ROLLBACK", **request)
                action = "create"
        except Exception as exc:  # botocore client errors are not a stable class here
            if "No updates are to be performed" in str(exc):
                return DeploymentResult.timed(unit.name, UnitStatus.COMPLETE, started, "no changes")
            return DeploymentResult.timed(unit.name, UnitStatus.FAILED, started, str(exc))

        status, detail = self._wait_for_stack(stack_name)
        return DeploymentResult.timed(unit.name, status, started, detail or action)

    def _stack_exists(self, stack_name: str) -> bool:
        try:
            stacks = self.client.describe_stacks(StackName=stack_name)["Stacks"]
        except Exception:  # ValidationError when the stack does not exist
            return False
        return bool(stacks) and stacks[0]["StackStatus"] != "REVIEW_IN_PROGRESS"

    def _wait_for_stack(self, stack_name: str) -> tuple[UnitStatus, str]:
        """Poll describe_stacks until the stack settles, or the timeout hits."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                stack = self.client.describe_stacks(StackName=stack_name)["Stacks"][0]
            except Exception as exc:
                return UnitStatus.FAILED, str(exc)
            status = self._translate(stack["StackStatus"])
            if status.is_terminal:
                return status, stack.get("StackStatusReason", stack["StackStatus"])
            time.sleep(self.poll_interval)
        return UnitStatus.FAILED, "timed out after " + str(self.timeout) + "s waiting for " + stack_name

    @staticmethod
    def _translate(raw: str) -> UnitStatus:
        if raw in _STATUS_MAP:
            return _STATUS_MAP[raw]
        # Anything left that is not in progress is a failure or a rollback, both
        # of which mean the stack is not usable by the next wave.
        return UnitStatus.IN_PROGRESS if raw.endswith("_IN_PROGRESS") else UnitStatus.FAILED

    def get_unit_status(self, unit: DeployableUnit) -> UnitStatus:
        stack_name = unit.metadata.get("stack_name", unit.name)
        try:
            stack = self.client.describe_stacks(StackName=stack_name)["Stacks"][0]
        except Exception:  # absent stack, or no usable credentials
            return UnitStatus.UNKNOWN
        return self._translate(stack["StackStatus"])
