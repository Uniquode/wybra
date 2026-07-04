from __future__ import annotations

import logging

from wybra.core.exceptions import ConfigurationError
from wybra.forms import context as _context  # noqa: F401
from wybra.forms.capabilities import DefaultFormsCapability, FormsCapability
from wybra.forms.middleware import register_forms_response_finalisation_middleware
from wybra.forms.rotation import normalise_csrf_token_previous_secrets
from wybra.forms.settings import FormsSettings
from wybra.services.secrets import MissingSecretError, SecretsCapability, SecretsError
from wybra.site import Site

logger = logging.getLogger(__name__)


async def setup_site(site: Site) -> None:
    settings = FormsSettings.load_settings(
        site.config,
        deployment_environment=site.deployment_environment,
    )
    csrf = settings.protector(
        _csrf_token_secret(site, settings),
        _csrf_token_previous_secrets(site, settings),
    )
    site.app.state.csrf = csrf
    site.provide_capability(FormsCapability, DefaultFormsCapability(csrf))
    register_forms_response_finalisation_middleware(site)


def _csrf_token_secret(site: Site, settings: FormsSettings) -> str | None:
    reference = settings.csrf_token_secret_reference
    if reference is None:
        return None

    reference_source, reference_identifier = reference
    fallback = settings.fallback_token_secret
    secrets = site.optional_capability(SecretsCapability)
    if secrets is None:
        if fallback is not None:
            logger.warning(
                "Falling back to configured CSRF token secret because "
                "SecretsCapability is unavailable.",
            )
            return fallback
        local_fallback = _local_generated_fallback(site, settings)
        if local_fallback is not None:
            return local_fallback
        raise ConfigurationError(
            "Keychain-backed CSRF token secret requires SecretsCapability. "
            "Add `wybra.secrets` to app modules or configure CSRF_SECRET fallback."
        )

    try:
        secret = secrets.resolve(reference_source, reference_identifier).reveal()
    except SecretsError as exc:
        logger.warning(
            "Falling back from keychain-backed CSRF token secret resolution.",
            extra={"error_type": type(exc).__name__},
        )
        if fallback is not None:
            return fallback
        local_fallback = _local_generated_fallback(site, settings)
        if local_fallback is not None:
            return local_fallback
        raise ConfigurationError(
            "Keychain-backed CSRF token secret could not be resolved: "
            f"source={reference_source}, key={reference_identifier}. "
            "Configure CSRF_SECRET fallback or fix the keychain reference."
        ) from exc

    if secret.strip():
        return secret.strip()
    if fallback is not None:
        return fallback
    local_fallback = _local_generated_fallback(site, settings)
    if local_fallback is not None:
        return local_fallback
    raise ConfigurationError(
        "Keychain-backed CSRF token secret is blank: "
        f"source={reference_source}, key={reference_identifier}. "
        "Configure a non-blank secret value."
    )


def _csrf_token_previous_secrets(
    site: Site,
    settings: FormsSettings,
) -> tuple[str, ...]:
    reference = settings.csrf_token_secret_previous_reference
    if reference is None:
        return ()

    secrets = site.optional_capability(SecretsCapability)
    if secrets is None:
        return ()

    reference_source, reference_identifier = reference
    try:
        value = secrets.resolve(reference_source, reference_identifier).reveal()
    except MissingSecretError:
        return ()
    except SecretsError as exc:
        raise ConfigurationError(
            "Keychain-backed previous CSRF token secrets could not be resolved: "
            f"source={reference_source}, key={reference_identifier}."
        ) from exc

    try:
        return normalise_csrf_token_previous_secrets(value)
    except ValueError as exc:
        raise ConfigurationError(
            "Keychain-backed previous CSRF token secrets are invalid: "
            f"source={reference_source}, key={reference_identifier}. {exc}"
        ) from exc


def _local_generated_fallback(site: Site, settings: FormsSettings) -> str | None:
    if site.deployment_environment != "local":
        return None
    generated_settings = FormsSettings(
        csrf_cookie_secure=settings.cookie_secure,
        deployment_environment=site.deployment_environment,
    )
    return generated_settings.fallback_token_secret


__all__ = ("setup_site",)
