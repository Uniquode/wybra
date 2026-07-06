from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.accounts.manager import InvalidID, UserManager, UserNotExists
from wybra.auth.admin.management import SqlAlchemyAuthManagementStore
from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.storage import (
    SqlAlchemyChallengeStore,
    SqlAlchemyRecoveryCodeStore,
    SqlAlchemyTOTPCredentialStore,
    SqlAlchemyWebAuthnCredentialStore,
)
from wybra.auth.models import AccessToken, IdentityUserEmail, User
from wybra.auth.options import IdentityOptions
from wybra.auth.persistence.contracts import (
    AuthManagementStore,
    AuthPersistenceScope,
    ChallengeStore,
    DuplicateIdentityError,
    LocalUserRecord,
    RecoveryCodeStore,
    SessionTokenStore,
    TOTPCredentialStore,
    WebAuthnCredentialStore,
)
from wybra.auth.provider_credentials import SqlAlchemyProviderCredentialStore
from wybra.db import DatabaseCapability
from wybra.db.persistence import Database, session_scope
from wybra.services.crypto import SecretEnvelopeService
from wybra.site import SiteCapabilityProxy

SecretEnvelopeServiceResolver = Callable[[], SecretEnvelopeService | None]
_IDENTITY_USER_EMAIL_UNIQUE_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "uq_identity_user_email_email",
        "identity_user_email_email_key",
    }
)


@dataclass(frozen=True, slots=True)
class SqlAlchemyUserStore:
    session: AsyncSession
    user_table: type[User] = User

    async def get(self, user_id) -> User | None:
        return await self.session.get(self.user_table, user_id)

    async def get_by_email(self, email: str) -> User | None:
        from wybra.auth.emails import resolve_user_by_email

        return await resolve_user_by_email(self.session, email)

    async def create_local_user(
        self,
        values: Mapping[str, object],
        *,
        primary_email: str,
        after_create: Callable[[LocalUserRecord], Awaitable[None]] | None = None,
    ) -> User:
        created_user = self.user_table(**dict(values))

        async def _create_and_persist_user() -> None:
            self.session.add(created_user)
            await self.session.flush()
            self.session.add(
                IdentityUserEmail(
                    user=created_user,
                    email=primary_email,
                    is_primary=True,
                    is_verified=created_user.is_verified,
                )
            )
            if after_create is not None:
                await after_create(created_user)

        try:
            async with (
                self.session.begin()
                if not self.session.in_transaction()
                else self.session.begin_nested()
            ):
                await _create_and_persist_user()
        except IntegrityError as exc:
            if _is_identity_email_unique_violation(exc):
                raise DuplicateIdentityError() from exc
            raise

        await self.session.refresh(created_user)
        return created_user

    async def save_user(self, user: LocalUserRecord) -> LocalUserRecord:
        self.session.add(user)
        try:
            await self.session.commit()
            await self.session.refresh(user)
        except IntegrityError as exc:
            if _is_identity_email_unique_violation(exc):
                raise DuplicateIdentityError() from exc
            raise
        return user


@dataclass(frozen=True, slots=True)
class SqlAlchemyAccessTokenStore:
    session: AsyncSession

    async def resolve(
        self,
        token: str,
        *,
        max_age_seconds: int | None,
    ) -> str | None:
        statement = select(AccessToken).where(AccessToken.token == token)
        if max_age_seconds:
            max_age = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
            statement = statement.where(AccessToken.created_at >= max_age)
        access_token = (await self.session.execute(statement)).scalar_one_or_none()
        return None if access_token is None else str(access_token.user_id)

    async def create(self, user: LocalUserRecord) -> str:
        access_token = AccessToken(token=secrets.token_urlsafe(), user_id=user.id)
        self.session.add(access_token)
        await self.session.commit()
        await self.session.refresh(access_token)
        return access_token.token

    async def delete(self, token: str) -> None:
        await self.session.execute(
            delete(AccessToken).where(AccessToken.token == token)
        )
        await self.session.commit()

    async def delete_by_token(self, token: str) -> None:
        await self.delete(token)


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
        except (UserNotExists, InvalidID):
            return None

    async def write_token(self, user: LocalUserRecord) -> str:
        return await self.store.create(user)

    async def destroy_token(self, token: str) -> None:
        await self.store.delete(token)


@dataclass(frozen=True, slots=True)
class SqlAlchemyAuthPersistenceScope:
    """SQLAlchemy-backed auth repository scope.

    Runtime auth code receives this as an ``AuthPersistenceScope``. SQLAlchemy
    sessions stay inside the adapter object rather than leaking through the
    public capability contract.
    """

    session: AsyncSession
    secret_service: SecretEnvelopeService | None = None

    @property
    def users(self) -> SqlAlchemyUserStore:
        return SqlAlchemyUserStore(self.session)

    @property
    def session_tokens(self) -> SqlAlchemyAccessTokenStore:
        return SqlAlchemyAccessTokenStore(self.session)

    @property
    def challenges(self) -> ChallengeStore:
        return SqlAlchemyChallengeStore(self.session)

    @property
    def totp_credentials(self) -> TOTPCredentialStore:
        return SqlAlchemyTOTPCredentialStore(self.session, self.secret_service)

    @property
    def recovery_codes(self) -> RecoveryCodeStore:
        return SqlAlchemyRecoveryCodeStore(self.session, self.secret_service)

    @property
    def webauthn_credentials(self) -> WebAuthnCredentialStore:
        return SqlAlchemyWebAuthnCredentialStore(self.session)

    @property
    def provider_credentials(self) -> SqlAlchemyProviderCredentialStore:
        return SqlAlchemyProviderCredentialStore(self.session, self.secret_service)

    @property
    def management(self) -> AuthManagementStore:
        return SqlAlchemyAuthManagementStore(self.session)

    async def get_user(self, user_id: str | uuid.UUID) -> User | None:
        parsed_user_id = parse_uuid(user_id)
        if parsed_user_id is None:
            return None
        return await self.session.get(User, parsed_user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.users.get_by_email(email)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()


@dataclass(frozen=True, slots=True)
class SqlAlchemyAuthPersistenceCapability:
    """Auth persistence capability backed by the shared SQLAlchemy database."""

    database: SiteCapabilityProxy[DatabaseCapability]
    secret_service: SecretEnvelopeService | None = None
    secret_service_resolver: SecretEnvelopeServiceResolver | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AuthPersistenceScope]:
        async with self.database.session() as session:
            yield SqlAlchemyAuthPersistenceScope(session, self._secret_service())

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AuthPersistenceScope]:
        async with self.database.transaction() as session:
            yield SqlAlchemyAuthPersistenceScope(session, self._secret_service())

    def _secret_service(self) -> SecretEnvelopeService | None:
        if self.secret_service_resolver is not None:
            return self.secret_service_resolver()
        return self.secret_service


@asynccontextmanager
async def auth_persistence_session(
    database: Database,
    *,
    secret_service: SecretEnvelopeService | None = None,
    secret_service_resolver: SecretEnvelopeServiceResolver | None = None,
) -> AsyncIterator[AuthPersistenceScope]:
    """Yield an auth persistence scope for standalone SQLAlchemy tools."""

    async with session_scope(database.session_factory) as session:
        resolved_secret_service = (
            secret_service_resolver()
            if secret_service_resolver is not None
            else secret_service
        )
        yield SqlAlchemyAuthPersistenceScope(session, resolved_secret_service)


def create_access_token_database(
    session: AsyncSession,
) -> SqlAlchemyAccessTokenStore:
    return SqlAlchemyAccessTokenStore(session)


def create_database_strategy(
    session: AsyncSession,
    options: IdentityOptions,
) -> PersistentSessionTokenStrategy:
    return PersistentSessionTokenStrategy(
        create_access_token_database(session),
        lifetime_seconds=options.session_lifetime_seconds,
    )


async def delete_session_token_by_value(session: AsyncSession, token: str) -> None:
    await create_access_token_database(session).delete_by_token(token)


def create_user_database(
    session: AsyncSession,
) -> SqlAlchemyUserStore:
    return SqlAlchemyUserStore(session)


def _is_identity_email_unique_violation(exc: IntegrityError) -> bool:
    constraint_name = getattr(
        getattr(exc.orig, "diag", None),
        "constraint_name",
        None,
    )
    if constraint_name and constraint_name in _IDENTITY_USER_EMAIL_UNIQUE_CONSTRAINTS:
        return True

    message = str(exc.orig).lower() if exc.orig else str(exc).lower()
    return "identity_user_email.email" in message and "unique" in message
