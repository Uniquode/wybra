from __future__ import annotations

from typing import Final, Literal, cast, get_args

from wybra.core.exceptions import ConfigurationError

DeploymentEnvironment = Literal["local", "staging", "production"]
LOCAL_ENVIRONMENT: Final[DeploymentEnvironment] = "local"
DEFAULT_DEPLOYMENT_ENVIRONMENT: Final[DeploymentEnvironment] = LOCAL_ENVIRONMENT
ALLOWED_DEPLOYMENT_ENVIRONMENTS: Final[tuple[DeploymentEnvironment, ...]] = cast(
    tuple[DeploymentEnvironment, ...],
    get_args(DeploymentEnvironment),
)
DEPLOYMENT_ENVIRONMENT_ERROR: Final = (
    "Deployment environment must be one of: "
    + ", ".join(ALLOWED_DEPLOYMENT_ENVIRONMENTS)
    + "."
)


def normalise_deployment_environment(
    deployment_environment: DeploymentEnvironment | str | None,
) -> DeploymentEnvironment:
    if deployment_environment is None:
        return DEFAULT_DEPLOYMENT_ENVIRONMENT
    if deployment_environment in ALLOWED_DEPLOYMENT_ENVIRONMENTS:
        return cast(DeploymentEnvironment, deployment_environment)
    raise ConfigurationError(DEPLOYMENT_ENVIRONMENT_ERROR)


__all__ = (
    "ALLOWED_DEPLOYMENT_ENVIRONMENTS",
    "DEFAULT_DEPLOYMENT_ENVIRONMENT",
    "DEPLOYMENT_ENVIRONMENT_ERROR",
    "DeploymentEnvironment",
    "LOCAL_ENVIRONMENT",
    "normalise_deployment_environment",
)
