from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError

from wybra.auth.accounts.manager import InvalidID, UserManager, UserNotExists
from wybra.auth.admin.management import TortoiseAuthManagementStore
from wybra.auth.authorisation.effective import effective_scope_sets_for_user
from wybra.auth.email_normalisation import normalise_email_target
from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.storage import (
    TortoiseChallengeStore,
    TortoiseRecoveryCodeStore,
    TortoiseTOTPCredentialStore,
    TortoiseWebAuthnCredentialStore,
)
from wybra.auth.models import AccessToken, Group, IdentityUserEmail, Scope, User
from wybra.auth.options import IdentityOptions
from wybra.auth.persistence.contracts import (
    AuthManagementStore,
    AuthorisationStore,
    AuthPersistenceScope,
    ChallengeStore,
    DuplicateIdentityError,
    EffectiveScopeSet,
    GroupRecord,
    LocalUserRecord,
    RecoveryCodeStore,
    ScopeRecord,
    SessionTokenStore,
    TOTPCredentialStore,
    UserStore,
    WebAuthnCredentialStore,
)
from wybra.auth.provider_credentials import provider_credential_store
from wybra.auth.session_tokens import generate_session_token
from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_transaction
from wybra.db.persistence import Database
from wybra.services.crypto import SecretEnvelopeService
from wybra.site import SiteCapabilityProxy

SecretEnvelopeServiceResolver = Callable[[], SecretEnvelopeService | None]
_IDENTITY_UNIQUE_MARKERS: frozenset[str] = frozenset(
    {
        "identity_user.email",
        "identity_user_email.email",
        "identity_user_email_email",
    }
)


@dataclass(frozen=True, slots=True)
class TortoiseUserStore:
    connection: BaseDBAsyncClient
    user_table: type[User] = User

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.user_table.get_or_none(id=user_id, using_db=self.connection)

    async def get_by_email(self, email: str) -> User | None:
        normalised_email = normalise_email_target(email)
        if normalised_email is None:
            return None
        primary_email = await IdentityUserEmail.get_or_none(
            email=normalised_email,
            using_db=self.connection,
        )
        if primary_email is None:
            return None
        return await self.get(primary_email.user_id)

    async def create_local_user(
        self,
        values: Mapping[str, object],
        *,
        primary_email: str,
        after_create: Callable[[LocalUserRecord], Awaitable[None]] | None = None,
    ) -> User:
        create_values = dict(values)
        created_at = create_values.setdefault("created_at", _timestamp_now())
        create_values.setdefault("modified_at", created_at)
        created_user = self.user_table(**create_values)
        try:
            await created_user.save(using_db=self.connection)
            await IdentityUserEmail.create(
                user_id=created_user.id,
                email=primary_email,
                is_primary=True,
                is_verified=created_user.is_verified,
                using_db=self.connection,
            )
            if after_create is not None:
                try:
                    await after_create(created_user)
                except Exception:
                    await (
                        IdentityUserEmail.filter(
                            user_id=created_user.id,
                        )
                        .using_db(self.connection)
                        .delete()
                    )
                    await created_user.delete(using_db=self.connection)
                    raise
        except IntegrityError as exc:
            if _is_identity_unique_violation(exc):
                raise DuplicateIdentityError() from exc
            raise

        return created_user

    async def save_user(
        self,
        user: LocalUserRecord,
        *,
        primary_email: str | None = None,
        primary_email_verified: bool | None = None,
    ) -> LocalUserRecord:
        try:
            if isinstance(user, User):
                user.modified_at = _timestamp_now()
                await user.save(using_db=self.connection)
            if primary_email is not None:
                await self._set_primary_email(
                    user,
                    primary_email,
                    is_verified=(
                        user.is_verified
                        if primary_email_verified is None
                        else primary_email_verified
                    ),
                )
        except IntegrityError as exc:
            if _is_identity_unique_violation(exc):
                raise DuplicateIdentityError() from exc
            raise
        return user

    async def _set_primary_email(
        self,
        user: LocalUserRecord,
        email: str,
        *,
        is_verified: bool,
    ) -> None:
        await (
            self.user_table.filter(id=user.id)
            .using_db(self.connection)
            .select_for_update()
        )
        primary_emails = (
            await IdentityUserEmail.filter(user_id=user.id, is_primary=True)
            .using_db(self.connection)
            .select_for_update()
        )
        primary_email = primary_emails[0] if primary_emails else None
        if primary_email is None:
            await IdentityUserEmail.create(
                user_id=user.id,
                email=email,
                is_primary=True,
                is_verified=is_verified,
                using_db=self.connection,
            )
            return

        for stale_primary_email in primary_emails[1:]:
            stale_primary_email.is_primary = False
            await stale_primary_email.save(using_db=self.connection)

        primary_email.email = email
        primary_email.is_verified = is_verified
        await primary_email.save(using_db=self.connection)


@dataclass(frozen=True, slots=True)
class TortoiseAccessTokenStore:
    connection: BaseDBAsyncClient

    async def resolve(
        self,
        token: str,
        *,
        max_age_seconds: int | None,
    ) -> str | None:
        query = AccessToken.filter(token=token).using_db(self.connection)
        if max_age_seconds is not None:
            max_age = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
            query = query.filter(created_at__gte=max_age)
        access_token = await query.first()
        return None if access_token is None else str(access_token.user_id)

    async def create(self, user: LocalUserRecord) -> str:
        access_token = await AccessToken.create(
            token=generate_session_token(),
            user_id=user.id,
            using_db=self.connection,
        )
        return access_token.token

    async def delete(self, token: str) -> None:
        await AccessToken.filter(token=token).using_db(self.connection).delete()

    async def delete_by_token(self, token: str) -> None:
        await self.delete(token)

    async def delete_for_user(self, user: LocalUserRecord) -> None:
        await AccessToken.filter(user_id=user.id).using_db(self.connection).delete()


@dataclass(frozen=True, slots=True)
class PersistentSessionTokenStrategy:
    store: SessionTokenStore
    lifetime_seconds: int | None = None

    async def read_token(
        self,
        token: str | None,
        user_manager: UserManager,
    ) -> LocalUserRecord | None:
        if token is None:
            return None

        user_id = await self.store.resolve(
            token,
            max_age_seconds=self.lifetime_seconds,
        )
        if user_id is None:
            return None

        try:
            parsed_id = user_manager.parse_id(user_id)
            return await user_manager.get(parsed_id)
        except UserNotExists, InvalidID:
            return None

    async def write_token(self, user: LocalUserRecord) -> str:
        return await self.store.create(user)

    async def destroy_token(self, token: str) -> None:
        await self.store.delete(token)


@dataclass(frozen=True, slots=True)
class TortoiseAuthorisationStore:
    """Tortoise-backed authorisation catalogue and effective scopes."""

    connection: BaseDBAsyncClient

    async def list_groups(self) -> tuple[GroupRecord, ...]:
        groups = await Group.all().using_db(self.connection).order_by("abbrev")
        return tuple(
            GroupRecord(id=str(group.id), abbrev=group.abbrev, title=group.description)
            for group in groups
        )

    async def list_scopes(self) -> tuple[ScopeRecord, ...]:
        scopes = await Scope.all().using_db(self.connection).order_by("scope")
        return tuple(
            ScopeRecord(scope=scope.scope, title=scope.description) for scope in scopes
        )

    async def effective_scope_sets_for_user(
        self,
        user_id: uuid.UUID,
    ) -> EffectiveScopeSet:
        scopes, groups = await effective_scope_sets_for_user(
            self.connection,
            user_id,
        )
        return EffectiveScopeSet(scopes=scopes, groups=groups)


@dataclass(frozen=True, slots=True)
class TortoiseAuthPersistenceScope:
    """Tortoise-backed auth repository scope."""

    connection: BaseDBAsyncClient
    secret_service: SecretEnvelopeService | None = None

    @property
    def users(self) -> TortoiseUserStore:
        return TortoiseUserStore(self.connection)

    @property
    def session_tokens(self) -> TortoiseAccessTokenStore:
        return TortoiseAccessTokenStore(self.connection)

    @property
    def challenges(self) -> ChallengeStore:
        return TortoiseChallengeStore(self.connection)

    @property
    def totp_credentials(self) -> TOTPCredentialStore:
        return TortoiseTOTPCredentialStore(self.connection, self.secret_service)

    @property
    def recovery_codes(self) -> RecoveryCodeStore:
        return TortoiseRecoveryCodeStore(self.connection, self.secret_service)

    @property
    def webauthn_credentials(self) -> WebAuthnCredentialStore:
        return TortoiseWebAuthnCredentialStore(self.connection)

    @property
    def provider_credentials(self) -> object:
        return provider_credential_store(self.connection, self.secret_service)

    @property
    def management(self) -> AuthManagementStore:
        return TortoiseAuthManagementStore(self.connection, self.secret_service)

    @property
    def authorisation(self) -> AuthorisationStore:
        return TortoiseAuthorisationStore(self.connection)

    async def get_user(self, user_id: str | uuid.UUID) -> User | None:
        parsed_user_id = parse_uuid(user_id)
        if parsed_user_id is None:
            return None
        return await User.get_or_none(id=parsed_user_id, using_db=self.connection)

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.users.get_by_email(email)


@dataclass(frozen=True, slots=True)
class TortoiseAuthPersistenceCapability:
    """Auth persistence capability backed by the shared Tortoise database."""

    database: SiteCapabilityProxy[DatabaseCapability]
    secret_service: SecretEnvelopeService | None = None
    secret_service_resolver: SecretEnvelopeServiceResolver | None = None

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[AuthPersistenceScope]:
        async with self.transaction() as scope:
            yield scope

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AuthPersistenceScope]:
        database = await self.database.require()
        context_scope = getattr(database, "context", nullcontext)()
        with context_scope:
            async with tortoise_transaction(
                database,
                database.database().for_write(),
            ) as connection:
                yield TortoiseAuthPersistenceScope(connection, self._secret_service())

    def _secret_service(self) -> SecretEnvelopeService | None:
        if self.secret_service_resolver is not None:
            return self.secret_service_resolver()
        return self.secret_service


@asynccontextmanager
async def auth_persistence_scope(
    database: Database,
    *,
    secret_service: SecretEnvelopeService | None = None,
    secret_service_resolver: SecretEnvelopeServiceResolver | None = None,
) -> AsyncIterator[AuthPersistenceScope]:
    """Yield an auth persistence scope for standalone Tortoise tools."""

    resolved_secret_service = (
        secret_service_resolver()
        if secret_service_resolver is not None
        else secret_service
    )
    with database.context:
        async with database.transaction_for(
            database.routes.connection().for_write()
        ) as connection:
            yield TortoiseAuthPersistenceScope(
                connection,
                resolved_secret_service,
            )


def create_session_token_store(
    connection: BaseDBAsyncClient,
) -> TortoiseAccessTokenStore:
    return TortoiseAccessTokenStore(connection)


def create_session_token_strategy(
    connection: BaseDBAsyncClient,
    options: IdentityOptions,
) -> PersistentSessionTokenStrategy:
    return PersistentSessionTokenStrategy(
        create_session_token_store(connection),
        lifetime_seconds=options.session_lifetime_seconds,
    )


async def delete_session_token_by_value(
    connection: BaseDBAsyncClient,
    token: str,
) -> None:
    await create_session_token_store(connection).delete_by_token(token)


def create_user_store(connection: BaseDBAsyncClient) -> UserStore:
    return TortoiseUserStore(connection)


def _is_identity_unique_violation(exc: IntegrityError) -> bool:
    message = str(exc).lower()
    return "unique" in message and any(
        marker in message for marker in _IDENTITY_UNIQUE_MARKERS
    )


def _timestamp_now() -> float:
    from wybra.auth.timestamps import current_timestamp

    return current_timestamp()
