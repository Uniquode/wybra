from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.ids import parse_uuid
from wybra.auth.models import (
    ExternalIdentityLink,
    IdentityProvider,
    IdentityUserEmail,
    User,
)
from wybra.core.exceptions import ConfigurationError
from wybra.services.crypto import (
    SecretEnvelope,
    SecretEnvelopeService,
    SecretMaterialMissingError,
)


class ProviderCredentialStorageError(ConfigurationError):
    """Provider credential token material cannot be stored safely."""


@dataclass(frozen=True, slots=True)
class ProviderCredentialSecrets:
    access_token: SecretEnvelope
    refresh_token: SecretEnvelope | None


class ProviderCredentialStore(Protocol):
    async def upsert_provider_credential(
        self,
        *,
        provider_name: str,
        provider_subject: str,
        access_token: str,
        account_email: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        provider_enabled: bool = True,
        provider_metadata: dict[str, object] | None = None,
    ) -> IdentityProvider: ...

    async def get_provider_by_identity(
        self,
        provider_name: str,
        provider_subject: str,
    ) -> IdentityProvider | None: ...

    async def get_user_providers(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_name: str,
    ) -> tuple[IdentityProvider, ...]: ...

    async def get_user_provider_by_id(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_id: str | uuid.UUID,
    ) -> IdentityProvider | None: ...

    async def user_has_enabled_provider_link(
        self,
        user_id: str | uuid.UUID,
        *,
        provider_names: Iterable[str] | None = None,
        exclude_provider_id: str | uuid.UUID | None = None,
        exclude_provider_name: str | None = None,
    ) -> bool: ...

    async def unlink_user_provider(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_id: str | uuid.UUID,
    ) -> bool: ...

    async def get_linked_user(
        self,
        provider: IdentityProvider,
    ) -> User | None: ...

    async def get_user(self, user_id: str | uuid.UUID) -> User | None: ...

    async def get_user_by_normalised_email(
        self,
        normalised_email: str,
    ) -> User | None: ...

    async def create_provider_user(
        self,
        *,
        email: str,
        is_verified: bool,
    ) -> User: ...

    async def verify_matching_user_email(
        self,
        user: User,
        email: str,
        *,
        is_verified: bool,
    ) -> bool: ...

    async def link_provider_to_user(
        self,
        *,
        provider_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> ExternalIdentityLink: ...


def provider_credential_store(
    session: AsyncSession,
    secret_service: SecretEnvelopeService | None = None,
) -> ProviderCredentialStore:
    return SqlAlchemyProviderCredentialStore(session, secret_service)


class SqlAlchemyProviderCredentialStore:
    def __init__(
        self,
        session: AsyncSession,
        secret_service: SecretEnvelopeService | None = None,
    ) -> None:
        self._session = session
        self._secret_service = secret_service or SecretEnvelopeService.from_env(
            os.environ
        )

    async def create_provider_credential(
        self,
        *,
        provider_name: str,
        provider_subject: str,
        access_token: str,
        account_email: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        provider_enabled: bool = True,
        provider_metadata: dict[str, object] | None = None,
    ) -> str:
        provider = IdentityProvider(
            provider_name=provider_name,
            provider_subject=provider_subject,
            crypt_access_token=self._encrypt_provider_token(access_token),
            expires_at=expires_at,
            crypt_refresh_token=(
                self._encrypt_provider_token(refresh_token)
                if refresh_token is not None
                else None
            ),
            account_email=account_email,
            provider_enabled=provider_enabled,
            provider_metadata=provider_metadata,
        )
        self._session.add(provider)
        await self._session.flush()
        return str(provider.id)

    async def upsert_provider_credential(
        self,
        *,
        provider_name: str,
        provider_subject: str,
        access_token: str,
        account_email: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        provider_enabled: bool = True,
        provider_metadata: dict[str, object] | None = None,
    ) -> IdentityProvider:
        provider = await self.get_provider_by_identity(
            provider_name,
            provider_subject,
        )
        if provider is None:
            provider = IdentityProvider(
                provider_name=provider_name,
                provider_subject=provider_subject,
                crypt_access_token="",
                account_email=account_email,
                provider_enabled=provider_enabled,
            )
            self._session.add(provider)

        provider.crypt_access_token = self._encrypt_provider_token(access_token)
        provider.expires_at = expires_at
        provider.crypt_refresh_token = (
            self._encrypt_provider_token(refresh_token)
            if refresh_token is not None
            else None
        )
        provider.account_email = account_email
        provider.provider_enabled = provider_enabled
        provider.provider_metadata = provider_metadata
        await self._session.flush()
        return provider

    async def get_provider_credential(
        self,
        provider_id: str,
    ) -> IdentityProvider | None:
        parsed_provider_id = parse_uuid(provider_id)
        if parsed_provider_id is None:
            return None

        return await self._session.get(IdentityProvider, parsed_provider_id)

    async def get_provider_by_identity(
        self,
        provider_name: str,
        provider_subject: str,
    ) -> IdentityProvider | None:
        result = await self._session.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_name == provider_name,
                IdentityProvider.provider_subject == provider_subject,
            )
        )
        return result.unique().scalar_one_or_none()

    async def get_link_for_provider(
        self,
        provider_id: str | uuid.UUID,
    ) -> ExternalIdentityLink | None:
        parsed_provider_id = parse_uuid(provider_id)
        if parsed_provider_id is None:
            return None
        result = await self._session.execute(
            select(ExternalIdentityLink).where(
                ExternalIdentityLink.provider_id == parsed_provider_id
            )
        )
        return result.unique().scalar_one_or_none()

    async def get_user_providers(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_name: str,
    ) -> tuple[IdentityProvider, ...]:
        parsed_user_id = parse_uuid(user_id)
        if parsed_user_id is None:
            return ()
        result = await self._session.execute(
            select(IdentityProvider)
            .join(ExternalIdentityLink)
            .where(
                ExternalIdentityLink.user_id == parsed_user_id,
                IdentityProvider.provider_name == provider_name,
            )
        )
        return tuple(result.unique().scalars().all())

    async def get_user_provider_by_id(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_id: str | uuid.UUID,
    ) -> IdentityProvider | None:
        parsed_user_id = parse_uuid(user_id)
        parsed_provider_id = parse_uuid(provider_id)
        if parsed_user_id is None or parsed_provider_id is None:
            return None
        result = await self._session.execute(
            select(IdentityProvider)
            .join(ExternalIdentityLink)
            .where(
                ExternalIdentityLink.user_id == parsed_user_id,
                IdentityProvider.id == parsed_provider_id,
            )
        )
        return result.unique().scalar_one_or_none()

    async def user_has_enabled_provider_link(
        self,
        user_id: str | uuid.UUID,
        *,
        provider_names: Iterable[str] | None = None,
        exclude_provider_id: str | uuid.UUID | None = None,
        exclude_provider_name: str | None = None,
    ) -> bool:
        parsed_user_id = parse_uuid(user_id)
        if parsed_user_id is None:
            return False
        parsed_excluded_provider_id = (
            parse_uuid(exclude_provider_id) if exclude_provider_id is not None else None
        )
        query = (
            select(IdentityProvider.id)
            .join(ExternalIdentityLink)
            .where(
                ExternalIdentityLink.user_id == parsed_user_id,
                IdentityProvider.provider_enabled.is_(True),
            )
            .limit(1)
        )
        if provider_names is not None:
            names = tuple(provider_names)
            if not names:
                return False
            query = query.where(IdentityProvider.provider_name.in_(names))
        if parsed_excluded_provider_id is not None:
            query = query.where(IdentityProvider.id != parsed_excluded_provider_id)
        if exclude_provider_name is not None:
            query = query.where(IdentityProvider.provider_name != exclude_provider_name)
        result = await self._session.execute(query)
        return result.scalar_one_or_none() is not None

    async def unlink_user_provider(
        self,
        *,
        user_id: str | uuid.UUID,
        provider_id: str | uuid.UUID,
    ) -> bool:
        parsed_user_id = parse_uuid(user_id)
        parsed_provider_id = parse_uuid(provider_id)
        if parsed_user_id is None or parsed_provider_id is None:
            return False
        link_delete_result = cast(
            CursorResult[object],
            await self._session.execute(
                delete(ExternalIdentityLink).where(
                    ExternalIdentityLink.user_id == parsed_user_id,
                    ExternalIdentityLink.provider_id == parsed_provider_id,
                )
            ),
        )
        if link_delete_result.rowcount is None or link_delete_result.rowcount < 1:
            return False
        await self._session.execute(
            delete(IdentityProvider).where(IdentityProvider.id == parsed_provider_id)
        )
        await self._session.flush()
        return True

    async def get_linked_user(
        self,
        provider: IdentityProvider,
    ) -> User | None:
        result = await self._session.execute(
            select(User)
            .join(ExternalIdentityLink)
            .where(ExternalIdentityLink.provider_id == provider.id)
        )
        return result.unique().scalar_one_or_none()

    async def get_user(self, user_id: str | uuid.UUID) -> User | None:
        parsed_user_id = parse_uuid(user_id)
        if parsed_user_id is None:
            return None
        return await self._session.get(User, parsed_user_id)

    async def get_user_by_normalised_email(
        self,
        normalised_email: str,
    ) -> User | None:
        result = await self._session.execute(
            select(User)
            .join(IdentityUserEmail)
            .where(IdentityUserEmail.email == normalised_email)
        )
        return result.unique().scalar_one_or_none()

    async def create_provider_user(
        self,
        *,
        email: str,
        is_verified: bool,
    ) -> User:
        user = User(
            email=email,
            hashed_password=None,
            password_login_enabled=False,
            is_active=True,
            is_superuser=False,
            is_verified=is_verified,
        )
        self._session.add(user)
        await self._session.flush()
        self._session.add(
            IdentityUserEmail(
                user=user,
                email=email,
                is_primary=True,
                is_verified=is_verified,
            )
        )
        await self._session.flush()
        return user

    async def verify_matching_user_email(
        self,
        user: User,
        email: str,
        *,
        is_verified: bool,
    ) -> bool:
        if not is_verified:
            return False
        result = await self._session.execute(
            select(IdentityUserEmail).where(
                IdentityUserEmail.user_id == user.id,
                IdentityUserEmail.email == email,
            )
        )
        email_record = result.unique().scalar_one_or_none()
        if email_record is None:
            return False
        email_record.is_verified = True
        if user.email == email:
            user.is_verified = True
        return True

    async def link_provider_to_user(
        self,
        *,
        provider_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
    ) -> ExternalIdentityLink:
        parsed_provider_id = parse_uuid(provider_id)
        parsed_user_id = parse_uuid(user_id)
        if parsed_provider_id is None or parsed_user_id is None:
            raise ValueError("Provider link requires valid provider and user IDs.")
        existing = await self.get_link_for_provider(parsed_provider_id)
        if existing is not None:
            if existing.user_id != parsed_user_id:
                raise ValueError("Provider identity is already linked.")
            return existing
        link = ExternalIdentityLink(
            provider_id=parsed_provider_id,
            user_id=parsed_user_id,
        )
        self._session.add(link)
        await self._session.flush()
        return link

    def secret_envelopes(
        self,
        provider: IdentityProvider,
    ) -> ProviderCredentialSecrets:
        return ProviderCredentialSecrets(
            access_token=SecretEnvelope(provider.crypt_access_token),
            refresh_token=(
                SecretEnvelope(provider.crypt_refresh_token)
                if provider.crypt_refresh_token is not None
                else None
            ),
        )

    def decrypt_access_token(self, provider: IdentityProvider) -> str:
        plaintext, _version = self.secret_envelopes(provider).access_token.decrypt(
            service=self._secret_service,
        )
        return plaintext

    def decrypt_refresh_token(self, provider: IdentityProvider) -> str | None:
        refresh_token = self.secret_envelopes(provider).refresh_token
        if refresh_token is None:
            return None

        plaintext, _version = refresh_token.decrypt(service=self._secret_service)
        return plaintext

    def _encrypt_provider_token(self, value: str) -> str:
        try:
            return SecretEnvelope.from_plaintext(
                value,
                service=self._secret_service,
            ).value
        except SecretMaterialMissingError as exc:
            raise ProviderCredentialStorageError(
                "Provider credential storage requires configured crypto secret "
                "material."
            ) from exc
