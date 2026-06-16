from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.ids import parse_uuid
from wybra.auth.models import IdentityProvider
from wybra.services.crypto import SecretEnvelope, SecretEnvelopeService


@dataclass(frozen=True, slots=True)
class ProviderCredentialSecrets:
    access_token: SecretEnvelope
    refresh_token: SecretEnvelope | None


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
            crypt_access_token=SecretEnvelope.from_plaintext(
                access_token,
                service=self._secret_service,
            ).value,
            expires_at=expires_at,
            crypt_refresh_token=(
                SecretEnvelope.from_plaintext(
                    refresh_token,
                    service=self._secret_service,
                ).value
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

    async def get_provider_credential(
        self,
        provider_id: str,
    ) -> IdentityProvider | None:
        parsed_provider_id = parse_uuid(provider_id)
        if parsed_provider_id is None:
            return None

        return await self._session.get(IdentityProvider, parsed_provider_id)

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
