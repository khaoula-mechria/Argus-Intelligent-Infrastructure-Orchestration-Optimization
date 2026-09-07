"""Infrastructure backends and the factory that selects one."""

from __future__ import annotations

from ..config import ArgusConfig
from .base import (
    AdapterError,
    DeployableUnit,
    DeploymentResult,
    InfrastructureAdapter,
    UnitStatus,
)

BACKENDS = ("cloudformation", "terraform")


def build_adapter(backend: str, config: ArgusConfig | None = None) -> InfrastructureAdapter:
    """Instantiate the adapter for ``backend``, configured from ``argus.yaml``.

    Imports are done lazily so that a project using one backend never pays for
    the other one's dependencies.
    """
    config = config or ArgusConfig()

    if backend == "cloudformation":
        from .cloudformation import CloudFormationAdapter

        return CloudFormationAdapter(
            parameters=config.parameters,
            stack_name_prefix=config.stack_name_prefix,
            region=config.region,
        )

    if backend == "terraform":
        try:
            from .terraform import TerraformAdapter
        except ImportError as exc:  # optional dependency, or backend not installed
            raise AdapterError("the terraform backend is unavailable: " + str(exc)) from exc

        return TerraformAdapter(modules=config.modules)

    raise AdapterError("unknown backend '" + str(backend) + "'; expected one of " + ", ".join(BACKENDS))


__all__ = [
    "BACKENDS",
    "AdapterError",
    "DeployableUnit",
    "DeploymentResult",
    "InfrastructureAdapter",
    "UnitStatus",
    "build_adapter",
]
