from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from wybra.auth.result import Result


class AuthPersistenceError(RuntimeError):
    """Base error raised by auth persistence workflows."""


class DuplicateIdentityError(AuthPersistenceError):
    """Raised when a user or owned email address would duplicate identity."""


type ChallengeKind = Literal["totp", "webauthn", "recovery-code"]


class LocalUserRecord(Protocol):
    """Local account shape consumed by local account services."""

    id: uuid.UUID
    email: str
    hashed_password: str | None
    is_active: bool
    is_superuser: bool
    is_verified: bool
    is_admin: bool
    password_login_enabled: bool
    modified_at: float
    email_verification_sent_at: float | None
    expires_at: float | None
    preferred_timezone: str | None


@dataclass(frozen=True, slots=True)
class IdentityEmailRecord:
    """Owned email address."""

    id: str
    user_id: str
    email: str
    is_primary: bool
    is_verified: bool


@dataclass(frozen=True, slots=True)
class ExternalIdentityRecord:
    """External provider identity."""

    id: str
    provider_name: str
    provider_subject: str
    account_email: str
    provider_enabled: bool
    expires_at: float | None
    provider_metadata: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ExternalIdentityLinkRecord:
    """Link between a user and provider identity."""

    provider_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class ScopeRecord:
    scope: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class GroupRecord:
    id: str
    abbrev: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveScopeSet:
    scopes: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChallengeRecord:
    id: str
    user_id: str
    kind: ChallengeKind
    expires_at: float
    metadata: dict[str, Any]

    @property
    def metadata_payload(self) -> dict[str, Any]:
        return self.metadata


@dataclass(frozen=True, slots=True)
class WebAuthnCredentialRecord:
    id: str
    user_id: str
    credential_id: str
    public_key: bytes
    sign_count: int
    status: str
    label: str | None
    created_at: float
    last_used_at: float | None
    revoked_at: float | None
    user_verified: bool
    credential_device_type: str | None
    credential_backed_up: bool
    transports: tuple[str, ...]
    aaguid: str | None
    attestation_format: str | None


class UserStore(Protocol):
    """Local user persistence operations."""

    async def get(self, user_id: uuid.UUID) -> LocalUserRecord | None: ...

    async def get_by_email(self, email: str) -> LocalUserRecord | None: ...

    async def create_local_user(
        self,
        values: Mapping[str, object],
        *,
        primary_email: str,
        after_create: Callable[[LocalUserRecord], Awaitable[None]] | None = None,
    ) -> LocalUserRecord: ...

    async def save_user(
        self,
        user: LocalUserRecord,
        *,
        primary_email: str | None = None,
        primary_email_verified: bool | None = None,
    ) -> LocalUserRecord: ...


class SessionTokenStore(Protocol):
    """Browser session-token persistence operations."""

    async def create(self, user: LocalUserRecord) -> str: ...

    async def resolve(
        self,
        token: str,
        *,
        max_age_seconds: int | None,
    ) -> str | None: ...

    async def delete(self, token: str) -> None: ...

    async def delete_for_user(self, user: LocalUserRecord) -> None: ...


class IdentityEmailStore(Protocol):
    """Owned email persistence operations."""

    async def get_user_by_email(self, email: str) -> LocalUserRecord | None: ...

    async def list_user_emails(
        self,
        user_id: str | uuid.UUID,
    ) -> tuple[IdentityEmailRecord, ...]: ...

    async def verify_matching_email(
        self,
        user: LocalUserRecord,
        email: str,
        *,
        is_verified: bool,
    ) -> bool: ...


class ExternalIdentityStore(Protocol):
    """External provider identity operations."""

    async def get_provider_by_identity(
        self,
        provider_name: str,
        provider_subject: str,
    ) -> ExternalIdentityRecord | None: ...

    async def list_user_providers(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_name: str,
    ) -> tuple[ExternalIdentityRecord, ...]: ...

    async def user_has_enabled_provider_link(
        self,
        user_id: str | uuid.UUID,
        *,
        provider_names: tuple[str, ...] = (),
        exclude_provider_id: str | uuid.UUID | None = None,
        exclude_provider_name: str | None = None,
    ) -> bool: ...

    async def unlink_user_provider(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_id: str | uuid.UUID,
    ) -> bool: ...


class InitialAdminBootstrapStore(Protocol):
    """Initial-administrator bootstrap persistence operations."""

    async def find_administrative_user(self) -> LocalUserRecord | None: ...

    async def claim_initial_admin_bootstrap(self) -> bool: ...


class AuthorisationStore(Protocol):
    """Group, scope, membership, and effective-scope persistence operations."""

    async def list_groups(self) -> tuple[GroupRecord, ...]: ...

    async def list_scopes(self) -> tuple[ScopeRecord, ...]: ...

    async def effective_scope_sets_for_user(
        self,
        user_id: uuid.UUID,
    ) -> EffectiveScopeSet: ...


class AuthManagementStore(Protocol):
    """Auth management workflow persistence operations."""

    async def resolve_user_record(self, target: str) -> Result[dict[str, Any]]: ...

    async def validate_group_targets(
        self,
        group_targets: tuple[str, ...],
    ) -> Result[dict[str, Any]]: ...

    async def update_user_groups(
        self,
        *,
        target: str,
        add_group_targets: tuple[str, ...] = (),
        remove_group_targets: tuple[str, ...] = (),
        set_group_targets: tuple[str, ...] = (),
    ) -> Result[dict[str, Any]]: ...

    async def create_local_user(
        self,
        options: object,
        *,
        email: str,
        password: str,
        is_admin: bool = False,
        is_superuser: bool = False,
        is_verified: bool = True,
        preferred_timezone: str | None = None,
        expires_at: float | None = None,
    ) -> Result[dict[str, Any]]: ...

    async def update_local_user(
        self,
        options: object,
        *,
        target: str,
        is_admin: bool | None = None,
        is_superuser: bool | None = None,
        is_verified: bool | None = None,
        password: str | None = None,
        revoke_sessions: bool = True,
        preferred_timezone: str | None = None,
        clear_preferred_timezone: bool = False,
        expires_at: float | None = None,
        clear_expires_at: bool = False,
    ) -> Result[dict[str, Any]]: ...

    async def delete_local_user(self, *, target: str) -> Result[dict[str, Any]]: ...

    async def deactivate_local_user(self, *, target: str) -> Result[dict[str, Any]]: ...

    async def list_local_users(
        self,
        *,
        email_pattern: str | None = None,
        domain_pattern: str | None = None,
        is_admin: bool | None = None,
        is_superuser: bool | None = None,
        effective_active: bool | None = None,
        is_verified: bool | None = None,
        since_created_at: float | None = None,
        before_created_at: float | None = None,
        since_modified_at: float | None = None,
        before_modified_at: float | None = None,
        since_last_login_at: float | None = None,
        before_last_login_at: float | None = None,
        never_logged_in: bool | None = None,
        order: str = "email",
        direction: str | None = None,
        include_passkeys: bool = False,
    ) -> Result[dict[str, Any]]: ...

    async def provision_totp(
        self,
        options: object,
        *,
        target: str,
    ) -> Result[dict[str, Any]]: ...

    async def disable_totp(self, *, target: str) -> Result[dict[str, Any]]: ...

    async def rotate_totp_recovery_codes(
        self,
        *,
        target: str,
    ) -> Result[dict[str, Any]]: ...

    async def revoke_passkeys(
        self,
        *,
        target: str,
        credential: str | None = None,
    ) -> Result[dict[str, Any]]: ...

    async def create_scope(
        self,
        *,
        scope: str,
        description: str | None = None,
    ) -> Result[dict[str, Any]]: ...

    async def update_scope(
        self,
        *,
        scope: str,
        description: str | None = None,
    ) -> Result[dict[str, Any]]: ...

    async def delete_scope(self, *, scope: str) -> Result[dict[str, Any]]: ...

    async def list_scopes(self) -> Result[dict[str, Any]]: ...

    async def create_group(
        self,
        *,
        abbrev: str,
        description: str,
    ) -> Result[dict[str, Any]]: ...

    async def update_group(
        self,
        *,
        target: str,
        description: str,
    ) -> Result[dict[str, Any]]: ...

    async def delete_group(self, *, target: str) -> Result[dict[str, Any]]: ...

    async def get_group(self, *, target: str) -> Result[dict[str, Any]]: ...

    async def list_groups(self) -> Result[dict[str, Any]]: ...

    async def add_scope_to_group(
        self,
        *,
        group_target: str,
        scope: str,
    ) -> Result[dict[str, Any]]: ...

    async def remove_scope_from_group(
        self,
        *,
        group_target: str,
        scope: str,
    ) -> Result[dict[str, Any]]: ...

    async def add_user_to_group(
        self,
        *,
        group_target: str,
        user_target: str,
    ) -> Result[dict[str, Any]]: ...

    async def remove_user_from_group(
        self,
        *,
        group_target: str,
        user_target: str,
    ) -> Result[dict[str, Any]]: ...

    async def add_child_group_to_group(
        self,
        *,
        parent_target: str,
        child_target: str,
    ) -> Result[dict[str, Any]]: ...

    async def remove_child_group_from_group(
        self,
        *,
        parent_target: str,
        child_target: str,
    ) -> Result[dict[str, Any]]: ...

    async def effective_scopes_for_user(
        self,
        *,
        user_target: str,
    ) -> Result[dict[str, Any]]: ...


class ChallengeStore(Protocol):
    """Authentication challenge persistence operations."""

    async def create_challenge(
        self,
        user_id: str,
        kind: ChallengeKind,
        expires_at: float,
        metadata: dict[str, Any] | None = None,
    ) -> ChallengeRecord: ...

    async def get_challenge(self, challenge_id: str) -> ChallengeRecord | None: ...

    async def consume_challenge(self, challenge_id: str) -> None: ...


class TOTPCredentialStore(Protocol):
    """TOTP credential persistence operations."""

    async def create_pending_totp_credential(
        self,
        user_id: str,
        secret: str,
    ) -> str: ...

    async def activate_totp_credential(self, credential_id: str) -> None: ...

    async def disable_totp_credential(self, credential_id: str) -> None: ...

    async def get_active_totp_credential(self, user_id: str) -> str | None: ...

    async def get_pending_totp_credential(self, user_id: str) -> str | None: ...

    async def get_totp_credential(self, credential_id: str) -> Any | None: ...

    def decrypt_totp_secret(self, credential: Any) -> str: ...

    async def clear_totp_credentials(self, user_id: str) -> None: ...

    async def verify_totp_credential(
        self,
        *,
        credential_id: str,
        user_id: str,
        code: str,
        period_seconds: int,
        allowed_drift: int,
        expected_status: str,
        timestamp: float | None = None,
    ) -> tuple[bool, int | None, str | None]: ...


class RecoveryCodeStore(Protocol):
    """Single-use recovery code persistence operations."""

    async def replace_recovery_codes(
        self,
        user_id: str,
        credential_id: str,
        recovery_codes: tuple[str, ...],
    ) -> None: ...

    async def consume_recovery_code(self, user_id: str, code: str) -> bool: ...


class WebAuthnCredentialStore(Protocol):
    """WebAuthn credential persistence operations."""

    async def store_webauthn_credential(
        self,
        user_id: str,
        credential_id: str,
        public_key: bytes,
        sign_count: int,
        *,
        label: str | None = None,
        user_verified: bool = False,
        credential_device_type: str | None = None,
        credential_backed_up: bool = False,
        transports: tuple[str, ...] = (),
        aaguid: str | None = None,
        attestation_format: str | None = None,
    ) -> str: ...

    async def get_webauthn_credential(
        self,
        credential_id: str,
    ) -> WebAuthnCredentialRecord | None: ...

    async def get_user_webauthn_credential(
        self,
        user_id: str,
        row_id: str,
    ) -> WebAuthnCredentialRecord | None: ...

    async def list_active_webauthn_credentials(
        self,
        user_id: str,
    ) -> tuple[WebAuthnCredentialRecord, ...]: ...

    async def count_active_webauthn_credentials(
        self,
        user_id: str,
        *,
        exclude_row_id: str | uuid.UUID | None = None,
    ) -> int: ...

    async def update_webauthn_sign_count(
        self,
        credential_id: str,
        sign_count: int,
    ) -> None: ...

    async def update_webauthn_authentication(
        self,
        credential_id: str,
        *,
        sign_count: int,
        user_verified: bool,
        credential_device_type: str | None,
        credential_backed_up: bool,
    ) -> None: ...

    async def revoke_webauthn_credential(
        self,
        user_id: str,
        row_id: str,
    ) -> bool: ...


class AuthPersistenceScope(Protocol):
    """Repository scope for one auth persistence interaction."""

    @property
    def users(self) -> UserStore: ...

    @property
    def session_tokens(self) -> SessionTokenStore: ...

    @property
    def challenges(self) -> ChallengeStore: ...

    @property
    def totp_credentials(self) -> TOTPCredentialStore: ...

    @property
    def recovery_codes(self) -> RecoveryCodeStore: ...

    @property
    def webauthn_credentials(self) -> WebAuthnCredentialStore: ...

    @property
    def provider_credentials(self) -> object: ...

    @property
    def management(self) -> AuthManagementStore: ...

    @property
    def authorisation(self) -> AuthorisationStore: ...

    async def get_user(self, user_id: str | uuid.UUID) -> LocalUserRecord | None: ...

    async def get_user_by_email(self, email: str) -> LocalUserRecord | None: ...


@runtime_checkable
class AuthPersistenceCapability(Protocol):
    """Public auth persistence capability exposed through ``Site``."""

    def scope(self) -> AbstractAsyncContextManager[AuthPersistenceScope]: ...

    def transaction(self) -> AbstractAsyncContextManager[AuthPersistenceScope]: ...
