from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from wybra.auth.timestamps import current_timestamp
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
    provider_settings_with_available_secrets,
    resolve_provider_client_secret,
    validate_provider_secret_settings,
)
from wybra.providers.github import (
    GITHUB_DEFAULT_API_VERSION,
    GITHUB_DEFAULT_AUTHORISATION_ENDPOINT,
    GITHUB_DEFAULT_EMAILS_API_ENDPOINT,
    GITHUB_DEFAULT_SCOPES,
    GITHUB_DEFAULT_TOKEN_ENDPOINT,
    GITHUB_DEFAULT_USER_API_ENDPOINT,
    GitHubAPIError,
    github_granted_scopes,
    github_oauth_settings_from_provider,
    github_token_response_from_payload,
    github_token_response_has_required_scopes,
    github_user_claims_from_api_payloads,
)
from wybra.providers.google import (
    GOOGLE_DEFAULT_ISSUER,
    GOOGLE_DEFAULT_JWKS_URI,
    GoogleIDTokenValidationError,
    GoogleIDTokenValidationRequest,
    GoogleOAuthSettings,
    GoogleOIDCIDTokenValidator,
    google_id_token_claims_from_payload,
    google_oauth_settings_from_provider,
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


class FakeGoogleJwksClient:
    def __init__(self, key) -> None:
        self.key = key
        self.tokens: list[str] = []

    def get_signing_key_from_jwt(self, token: str):
        self.tokens.append(token)
        return SimpleNamespace(key=self.key)


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
                    "email_match_linking_enabled": True,
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
        assert provider.email_match_linking_enabled is True
        assert provider.required_claims == ("email", "email_verified")
        assert provider.allowed_domains == ("example.com",)

    def test_email_match_linking_defaults_to_disabled(self) -> None:
        provider = _providers_settings({"google": {"enabled": True}}).provider("google")

        assert provider.email_match_linking_enabled is False

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

    def test_google_oauth_settings_use_google_defaults(self) -> None:
        provider = _providers_settings(
            {
                "google": {
                    "enabled": True,
                    "client_id": " google-client-id ",
                    "secrets": "keychain",
                    "client_secret_key": "auth/providers/google/dev/client-secret",
                }
            }
        ).provider("google")

        settings = google_oauth_settings_from_provider(provider)

        assert settings.client_id == "google-client-id"
        assert settings.scopes == ("openid", "email", "profile")
        assert settings.issuer == "https://accounts.google.com"
        assert (
            settings.authorisation_endpoint
            == "https://accounts.google.com/o/oauth2/v2/auth"
        )
        assert settings.token_endpoint == "https://oauth2.googleapis.com/token"
        assert settings.jwks_uri == "https://www.googleapis.com/oauth2/v3/certs"
        assert (
            settings.discovery_document_url
            == "https://accounts.google.com/.well-known/openid-configuration"
        )

    def test_google_oauth_settings_require_google_provider(self) -> None:
        with pytest.raises(ConfigurationError, match="provider 'google'"):
            google_oauth_settings_from_provider(ProviderSettings(name="github"))

    def test_google_oauth_settings_require_client_id_and_secret_reference(
        self,
    ) -> None:
        with pytest.raises(ConfigurationError, match="client_id"):
            google_oauth_settings_from_provider(ProviderSettings(name="google"))

        with pytest.raises(ConfigurationError, match="client_secret_key"):
            google_oauth_settings_from_provider(
                ProviderSettings(name="google", client_id="client-id")
            )

    def test_github_oauth_settings_use_github_defaults(self) -> None:
        provider = _providers_settings(
            {
                "github": {
                    "enabled": True,
                    "client_id": " github-client-id ",
                    "secrets": "keychain",
                    "client_secret_key": "auth/providers/github/dev/client-secret",
                }
            }
        ).provider("github")

        settings = github_oauth_settings_from_provider(provider)

        assert settings.client_id == "github-client-id"
        assert settings.scopes == GITHUB_DEFAULT_SCOPES
        assert settings.authorisation_endpoint == GITHUB_DEFAULT_AUTHORISATION_ENDPOINT
        assert settings.token_endpoint == GITHUB_DEFAULT_TOKEN_ENDPOINT
        assert settings.user_api_endpoint == GITHUB_DEFAULT_USER_API_ENDPOINT
        assert settings.emails_api_endpoint == GITHUB_DEFAULT_EMAILS_API_ENDPOINT
        assert settings.api_version == GITHUB_DEFAULT_API_VERSION

    def test_github_oauth_settings_require_github_provider(self) -> None:
        with pytest.raises(ConfigurationError, match="provider 'github'"):
            github_oauth_settings_from_provider(ProviderSettings(name="google"))

    def test_github_oauth_settings_require_client_id_and_secret_reference(
        self,
    ) -> None:
        with pytest.raises(ConfigurationError, match="client_id"):
            github_oauth_settings_from_provider(ProviderSettings(name="github"))

        with pytest.raises(ConfigurationError, match="client_secret_key"):
            github_oauth_settings_from_provider(
                ProviderSettings(name="github", client_id="client-id")
            )


class TestGitHubClaimsAndTokens:
    def test_claim_mapping_uses_numeric_user_id_as_provider_subject(self) -> None:
        claims = github_user_claims_from_api_payloads(
            {
                "id": 12345,
                "login": "octocat",
                "avatar_url": "https://avatars.example/octocat",
            },
            (
                {
                    "email": "octocat@example.com",
                    "verified": True,
                    "primary": True,
                },
            ),
        )

        assert claims.subject == "12345"
        assert claims.email == "octocat@example.com"
        assert claims.email_verified is True
        assert claims.login == "octocat"
        assert claims.claims["id"] == "12345"
        assert claims.claims["login"] == "octocat"
        assert claims.claims["avatar_url"] == "https://avatars.example/octocat"

    def test_claim_mapping_prefers_verified_email_over_unverified_primary(
        self,
    ) -> None:
        claims = github_user_claims_from_api_payloads(
            {"id": "github-subject"},
            (
                {
                    "email": "primary@example.com",
                    "verified": False,
                    "primary": True,
                },
                {
                    "email": "verified@example.com",
                    "verified": True,
                    "primary": False,
                },
            ),
        )

        assert claims.email == "verified@example.com"
        assert claims.email_verified is True

    def test_claim_mapping_rejects_missing_email(self) -> None:
        with pytest.raises(GitHubAPIError, match="email"):
            github_user_claims_from_api_payloads({"id": 12345}, ())

    def test_token_response_parses_payload_fields(self) -> None:
        response = github_token_response_from_payload(
            {
                "access_token": "access-token",
                "token_type": "bearer",
                "scope": "read:user,user:email",
                "expires_in": 300,
                "refresh_token": "refresh-token",
            }
        )

        assert response.access_token == "access-token"
        assert response.token_type == "bearer"
        assert response.scope == "read:user,user:email"
        assert response.expires_in == 300
        assert response.refresh_token == "refresh-token"

    def test_scope_matching_accepts_comma_or_space_separated_values(self) -> None:
        response = github_token_response_from_payload(
            {
                "access_token": "access-token",
                "token_type": "bearer",
                "scope": "read:user, user:email repo",
            }
        )

        assert github_granted_scopes(response.scope) == (
            "read:user",
            "user:email",
            "repo",
        )
        assert github_token_response_has_required_scopes(
            response,
            ("read:user", "user:email"),
        )
        assert not github_token_response_has_required_scopes(response, ("gist",))


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

    def test_missing_provider_secret_disables_provider_for_runtime(self) -> None:
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

        effective, issues = provider_settings_with_available_secrets(
            settings,
            RecordingSecretsCapability(),
        )

        assert effective.provider("google").enabled is False
        assert len(issues) == 1
        assert issues[0].provider_name == "google"
        assert "missing" in issues[0].message

    def test_missing_secrets_capability_disables_provider_for_runtime(self) -> None:
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

        effective, issues = provider_settings_with_available_secrets(settings, None)

        assert effective.provider("google").enabled is False
        assert len(issues) == 1
        assert issues[0].provider_name == "google"
        assert "SecretsCapability is not available" in issues[0].message
        assert "wybra.secrets" in issues[0].message

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

    def test_verified_email_match_allows_auto_linking(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(
                name="google",
                enabled=True,
                email_match_linking_enabled=True,
            ),
            assertion=ProviderAssertion(
                "google",
                "subject-1",
                {"email": "user@example.com", "email_verified": True},
            ),
            email_match_user_id="user-1",
        )

        assert decision.outcome is ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED
        assert decision.user_id == "user-1"
        assert decision.accepted is True

    def test_email_match_requires_verified_provider_email(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(
                name="google",
                enabled=True,
                email_match_linking_enabled=True,
            ),
            assertion=ProviderAssertion(
                "google",
                "subject-1",
                {"email": "user@example.com", "email_verified": False},
            ),
            email_match_user_id="user-1",
        )

        assert decision.outcome is ProviderPolicyOutcome.INVALID_CLAIMS
        assert decision.accepted is False

    def test_email_match_is_denied_when_policy_is_disabled(self) -> None:
        decision = ProviderAccountPolicy().evaluate_login(
            provider=ProviderSettings(name="google", enabled=True),
            assertion=ProviderAssertion(
                "google",
                "subject-1",
                {"email": "user@example.com", "email_verified": True},
            ),
            email_match_user_id="user-1",
        )

        assert decision.outcome is ProviderPolicyOutcome.CREATION_DENIED
        assert decision.accepted is False

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


class TestGoogleIDTokenValidation:
    @pytest.mark.anyio
    async def test_oidc_validator_accepts_signed_google_id_token(self) -> None:
        token, public_key = self._signed_google_id_token()
        jwks_client = FakeGoogleJwksClient(public_key)
        jwks_uris: list[str] = []

        def jwks_client_factory(jwks_uri: str) -> FakeGoogleJwksClient:
            jwks_uris.append(jwks_uri)
            return jwks_client

        validator = GoogleOIDCIDTokenValidator(jwks_client_factory=jwks_client_factory)

        claims = await validator.validate(
            GoogleIDTokenValidationRequest(
                id_token=token,
                settings=GoogleOAuthSettings(
                    provider_name="google",
                    client_id="google-client-id",
                    client_secret_reference=("environment", "GOOGLE_SECRET"),
                ),
                nonce="nonce-value",
            )
        )

        assert claims.subject == "google-subject"
        assert claims.email == "user@example.com"
        assert claims.email_verified is True
        assert claims.nonce == "nonce-value"
        assert jwks_uris == [GOOGLE_DEFAULT_JWKS_URI]
        assert jwks_client.tokens == [token]

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "claim_overrides",
        (
            {"aud": "other-client-id"},
            {"iss": "https://accounts.example.invalid"},
            {"exp": int(current_timestamp() - 300)},
        ),
    )
    async def test_oidc_validator_rejects_invalid_trust_claims(
        self,
        claim_overrides: dict[str, object],
    ) -> None:
        token, public_key = self._signed_google_id_token(
            claim_overrides=claim_overrides
        )
        validator = GoogleOIDCIDTokenValidator(
            jwks_client_factory=lambda _jwks_uri: FakeGoogleJwksClient(public_key)
        )

        with pytest.raises(GoogleIDTokenValidationError, match="invalid"):
            await validator.validate(
                GoogleIDTokenValidationRequest(
                    id_token=token,
                    settings=GoogleOAuthSettings(
                        provider_name="google",
                        client_id="google-client-id",
                        client_secret_reference=("environment", "GOOGLE_SECRET"),
                    ),
                    nonce="nonce-value",
                )
            )

    @staticmethod
    def _signed_google_id_token(
        *,
        claim_overrides: dict[str, object] | None = None,
    ) -> tuple[str, object]:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims: dict[str, object] = {
            "iss": GOOGLE_DEFAULT_ISSUER,
            "aud": "google-client-id",
            "exp": int(current_timestamp() + 300),
            "sub": "google-subject",
            "email": "user@example.com",
            "email_verified": True,
            "nonce": "nonce-value",
        }
        if claim_overrides is not None:
            claims.update(claim_overrides)
        return (
            jwt.encode(
                claims,
                private_key,
                algorithm="RS256",
                headers={"kid": "test-key"},
            ),
            private_key.public_key(),
        )

    def test_claim_mapping_rejects_nonce_mismatch(self) -> None:
        with pytest.raises(GoogleIDTokenValidationError, match="nonce"):
            google_id_token_claims_from_payload(
                {
                    "sub": "google-subject",
                    "email": "user@example.com",
                    "email_verified": True,
                    "nonce": "actual",
                },
                expected_nonce="expected",
            )

    def test_claim_mapping_requires_email_verified_boolean(self) -> None:
        with pytest.raises(GoogleIDTokenValidationError, match="email_verified"):
            google_id_token_claims_from_payload(
                {
                    "sub": "google-subject",
                    "email": "user@example.com",
                    "email_verified": "true",
                    "nonce": "nonce",
                },
                expected_nonce="nonce",
            )


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


@pytest.mark.anyio
async def test_missing_provider_secret_disables_provider_without_startup_failure(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    with caplog.at_level("ERROR", logger="wybra.providers.capabilities"):
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

    assert providers.settings.provider("google").enabled is False
    assert "Provider 'google' disabled" in caplog.text
    assert "client secret is missing" in caplog.text


@pytest.mark.anyio
async def test_missing_secrets_module_disables_provider_without_startup_failure(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    with caplog.at_level("ERROR", logger="wybra.providers.capabilities"):
        site = await start(
            FastAPI(),
            config_source=_site_config_source(
                tmp_path,
                modules=(
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

    assert providers.settings.provider("google").enabled is False
    assert "Provider 'google' disabled" in caplog.text
    assert "SecretsCapability is not available" in caplog.text
    assert "wybra.secrets" in caplog.text


@pytest.mark.anyio
async def test_provider_secret_degradation_does_not_depend_on_module_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOOGLE_SECRET", "secret")
    site = await start(
        FastAPI(),
        config_source=_site_config_source(
            tmp_path,
            modules=(
                "wybra.providers",
                "wybra.secrets",
                "wybra.forms",
                "wybra.db",
                "wybra.auth",
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

    assert providers.settings.provider("google").enabled is True


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
