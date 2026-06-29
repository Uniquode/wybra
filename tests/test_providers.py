from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from wybra.config import ConfigService, MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.providers import (
    ProviderAccountPolicy,
    ProviderAssertion,
    ProviderPolicyOutcome,
    ProvidersCapability,
    ProviderSecretResolutionError,
    ProviderSettings,
    ProvidersSettings,
    resolve_provider_client_secret,
    validate_provider_secret_settings,
)
from wybra.secrets import MissingSecretError, SecretValue
from wybra.site import start


class RecordingSecretsCapability:
    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self.values = dict(values or {})
        self.exists_calls: list[tuple[str, str]] = []

    def resolve(self, source: str, key: str) -> SecretValue:
        try:
            return SecretValue(self.values[(source, key)], source=source, key=key)
        except KeyError as exc:
            raise MissingSecretError(source=source, key=key) from exc

    def exists(self, source: str, key: str) -> bool:
        self.exists_calls.append((source, key))
        return (source, key) in self.values


class FailingSecretsCapability:
    def resolve(self, source: str, key: str) -> SecretValue:
        raise AssertionError("disabled provider must not resolve secrets")

    def exists(self, source: str, key: str) -> bool:
        raise AssertionError("disabled provider must not validate secrets")


class TestProvidersSettings:
    def test_settings_load_from_providers_section(self) -> None:
        settings = _providers_settings(
            {
                "google": {
                    "enabled": True,
                    "client_id": " client-id ",
                    "secrets": " environment ",
                    "client_secret_key": " GOOGLE_SECRET ",
                    "account_creation_enabled": True,
                    "required_claims": ["email", "email_verified"],
                    "allowed_domains": [" Example.COM "],
                }
            }
        )

        provider = settings.provider("google")

        assert provider.client_id == "client-id"
        assert provider.required_client_secret_reference() == (
            "environment",
            "GOOGLE_SECRET",
        )
        assert provider.account_creation_enabled is True
        assert provider.required_claims == ("email", "email_verified")
        assert provider.allowed_domains == ("example.com",)

    def test_programmatic_provider_settings_strings_are_trimmed(self) -> None:
        provider = ProviderSettings(
            name=" google ",
            enabled=True,
            client_id=" client-id ",
            secrets=" environment ",
            client_secret_key=" GOOGLE_SECRET ",
        )

        assert provider.name == "google"
        assert provider.enabled is True
        assert provider.client_id == "client-id"
        assert provider.required_client_secret_reference() == (
            "environment",
            "GOOGLE_SECRET",
        )

    def test_enabled_provider_secret_reference_requires_source_and_key_pair(
        self,
    ) -> None:
        provider = ProviderSettings(name="google", enabled=True, secrets="environment")

        with pytest.raises(ConfigurationError, match="secrets.*client_secret_key"):
            provider.required_client_secret_reference()

    @pytest.mark.parametrize(
        "field_name",
        ["client_id", "secrets", "client_secret_key"],
    )
    def test_provider_settings_reject_blank_strings(self, field_name: str) -> None:
        provider_config = {
            "enabled": True,
            "client_id": "client-id",
            "secrets": "environment",
            "client_secret_key": "GOOGLE_SECRET",
        }
        provider_config[field_name] = "   "

        with pytest.raises(ConfigurationError, match=field_name):
            _providers_settings({"google": provider_config})


class TestProviderSecretValidation:
    def test_enabled_provider_validates_client_secret_reference(self) -> None:
        settings = _providers_settings(
            {
                "google": {
                    "enabled": True,
                    "client_id": "client-id",
                    "secrets": "environment",
                    "client_secret_key": "GOOGLE_SECRET",
                }
            }
        )
        secrets = RecordingSecretsCapability(
            {("environment", "GOOGLE_SECRET"): "secret"}
        )

        validate_provider_secret_settings(settings, secrets)

        assert secrets.exists_calls == [("environment", "GOOGLE_SECRET")]

    def test_enabled_provider_missing_secret_fails_clearly(self) -> None:
        settings = _providers_settings(
            {
                "google": {
                    "enabled": True,
                    "client_id": "client-id",
                    "secrets": "environment",
                    "client_secret_key": "GOOGLE_SECRET",
                }
            }
        )

        with pytest.raises(ProviderSecretResolutionError, match="google.*missing"):
            validate_provider_secret_settings(settings, RecordingSecretsCapability())

    def test_disabled_provider_does_not_validate_source_or_key(self) -> None:
        settings = _providers_settings(
            {
                "google": {
                    "enabled": False,
                    "secrets": "unsupported",
                    "client_secret_key": "IGNORED_SECRET",
                }
            }
        )

        validate_provider_secret_settings(settings, FailingSecretsCapability())

    def test_resolves_provider_client_secret(self) -> None:
        settings = _providers_settings(
            {
                "google": {
                    "enabled": True,
                    "client_id": "client-id",
                    "secrets": "environment",
                    "client_secret_key": "GOOGLE_SECRET",
                }
            }
        )
        secrets = RecordingSecretsCapability(
            {("environment", "GOOGLE_SECRET"): "secret"}
        )

        value = resolve_provider_client_secret(settings, "google", secrets)

        assert value.reveal() == "secret"
        assert "secret" not in repr(value)

    def test_enabled_provider_requires_secrets_capability(self) -> None:
        settings = _providers_settings(
            {
                "google": {
                    "enabled": True,
                    "client_id": "client-id",
                    "secrets": "environment",
                    "client_secret_key": "GOOGLE_SECRET",
                }
            }
        )

        with pytest.raises(ConfigurationError, match="SecretsCapability"):
            validate_provider_secret_settings(settings, None)


class TestProviderAccountPolicy:
    def test_linked_provider_subject_resolves_local_user(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(name="github", enabled=True),
            assertion=ProviderAssertion("github", "subject-1"),
            linked_user_id="user-1",
        )

        assert decision.outcome is ProviderPolicyOutcome.LINKED_USER
        assert decision.user_id == "user-1"
        assert decision.accepted is True

    def test_unlinked_provider_creates_only_when_policy_allows(self) -> None:
        policy = ProviderAccountPolicy()
        assertion = ProviderAssertion(
            "google",
            "subject-1",
            {"email": "USER@example.com", "email_verified": True},
        )

        allowed = policy.evaluate_login(
            provider=ProviderSettings(
                name="google",
                enabled=True,
                account_creation_enabled=True,
                allowed_domains=("example.com",),
            ),
            assertion=assertion,
        )
        denied = policy.evaluate_login(
            provider=ProviderSettings(name="google", enabled=True),
            assertion=assertion,
        )

        assert allowed.outcome is ProviderPolicyOutcome.CREATION_ALLOWED
        assert denied.outcome is ProviderPolicyOutcome.CREATION_DENIED

    def test_email_only_ownership_is_rejected_for_unverified_allowlist(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(
                name="google",
                enabled=True,
                account_creation_enabled=True,
                allowed_emails=("user@example.com",),
            ),
            assertion=ProviderAssertion(
                "google",
                "subject-1",
                {"email": "user@example.com", "email_verified": False},
            ),
        )

        assert decision.outcome is ProviderPolicyOutcome.CREATION_DENIED

    def test_linking_collision_is_rejected(self) -> None:
        decision = ProviderAccountPolicy().evaluate_linking(
            provider=ProviderSettings(name="github", enabled=True),
            assertion=ProviderAssertion("github", "subject-1"),
            current_user_id="user-1",
            linked_user_id="user-2",
        )

        assert decision.outcome is ProviderPolicyOutcome.COLLISION
        assert decision.accepted is False

    def test_inactive_linked_user_cannot_login(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(name="github", enabled=True),
            assertion=ProviderAssertion("github", "subject-1"),
            linked_user_id="user-1",
            linked_user_active=False,
        )

        assert decision.outcome is ProviderPolicyOutcome.INACTIVE_USER
        assert decision.accepted is False

    def test_required_claims_are_branchable_invalid_claims(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(
                name="apple",
                enabled=True,
                required_claims=("email",),
            ),
            assertion=ProviderAssertion("apple", "subject-1"),
        )

        assert decision.outcome is ProviderPolicyOutcome.INVALID_CLAIMS


@pytest.mark.anyio
async def test_provider_capability_is_available_when_module_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "secret")
    site = await start(
        FastAPI(),
        config_source=_site_config_source(
            tmp_path,
            modules=(
                "wybra.secrets",
                "wybra.forms",
                "wybra.db",
                "wybra.auth",
                "wybra.providers",
            ),
            providers={
                "google": {
                    "enabled": True,
                    "client_id": "client-id",
                    "secrets": "environment",
                    "client_secret_key": "GOOGLE_SECRET",
                }
            },
        ),
    )

    providers = site.require_capability(ProvidersCapability)

    assert providers.settings.provider("google").client_id == "client-id"


def _providers_settings(providers: dict[str, object]) -> ProvidersSettings:
    config = ConfigService(
        [MappingConfigSource({"auth.providers": providers})],
        config_defs=(ProvidersSettings.module_config,),
        discover_module_config=False,
    )
    return ProvidersSettings.load_settings(config)


def _site_config_source(
    tmp_path: Path,
    *,
    modules: tuple[str, ...],
    providers: dict[str, object] | None = None,
) -> MappingConfigSource:
    values: dict[str, dict[str, object]] = {
        "app": {
            "config_path": tmp_path / "app.toml",
            "project_root": tmp_path,
            "modules": modules,
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
        },
        "app.routes": {
            "prefixes": {
                "wybra.auth": {"account": "/account", "api": ""},
            }
        },
        "app.templates": {"auto_reload": True, "cache_size": 0},
        "app.assets": {"url_path": "/static/", "root": Path("static")},
    }
    if providers is not None:
        values["auth.providers"] = providers
    return MappingConfigSource(values)
