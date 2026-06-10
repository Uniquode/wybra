from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast, runtime_checkable
from uuid import UUID

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.ids import parse_uuid
from wevra.auth.mfa.recovery import (
    create_recovery_code_verifier,
    verify_recovery_code,
)
from wevra.auth.models import (
    IdentityAuthenticationChallenge,
    IdentityTotpCredential,
    IdentityTotpRecoveryCode,
)
from wevra.auth.timestamps import current_timestamp
from wevra.services.crypto import SecretEnvelopeService

ChallengeKind = Literal["totp", "webauthn", "recovery-code"]
TOTP_ACTIVE_STATUS: Literal["active"] = "active"
TOTP_DISABLED_STATUS: Literal["disabled"] = "disabled"
TOTP_PENDING_STATUS: Literal["pending"] = "pending"


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


@runtime_checkable
class ChallengeStore(Protocol):
    async def create_challenge(
        self,
        user_id: str,
        kind: ChallengeKind,
        expires_at: float,
        metadata: dict[str, Any] | None = None,
    ) -> ChallengeRecord: ...

    async def get_challenge(self, challenge_id: str) -> ChallengeRecord | None: ...

    async def consume_challenge(self, challenge_id: str) -> None: ...


@runtime_checkable
class TOTPCredentialStore(Protocol):
    async def create_pending_totp_credential(
        self,
        user_id: str,
        secret: str,
    ) -> str: ...

    async def activate_totp_credential(self, credential_id: str) -> None: ...

    async def disable_totp_credential(self, credential_id: str) -> None: ...

    async def get_active_totp_credential(self, user_id: str) -> str | None: ...

    async def get_pending_totp_credential(self, user_id: str) -> str | None: ...

    async def clear_totp_credentials(self, user_id: str) -> None: ...


@runtime_checkable
class WebAuthnCredentialStore(Protocol):
    async def store_webauthn_credential(
        self,
        user_id: str,
        credential_id: str,
        public_key: bytes,
        sign_count: int,
    ) -> None: ...

    async def update_webauthn_sign_count(
        self,
        credential_id: str,
        sign_count: int,
    ) -> None: ...


@runtime_checkable
class RecoveryCodeStore(Protocol):
    async def replace_recovery_codes(
        self,
        user_id: str,
        credential_id: str,
        recovery_codes: tuple[str, ...],
    ) -> None: ...

    async def consume_recovery_code(self, user_id: str, code: str) -> bool: ...


class SqlAlchemyChallengeStore:
    """Store challenge state in application tables."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_challenge(
        self,
        user_id: str,
        kind: ChallengeKind,
        expires_at: float,
        metadata: dict[str, Any] | None = None,
    ) -> ChallengeRecord:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            raise ValueError("Challenge user id must be a UUID string.")

        challenge_id = uuid.uuid4().hex
        metadata_payload = dict(metadata or {})
        self._session.add(
            IdentityAuthenticationChallenge(
                id=challenge_id,
                user_id=user_uuid,
                kind=kind,
                expires_at=expires_at,
                metadata_payload=metadata_payload,
            )
        )
        return ChallengeRecord(
            id=challenge_id,
            user_id=user_id,
            kind=kind,
            expires_at=expires_at,
            metadata=metadata_payload,
        )

    async def get_challenge(self, challenge_id: str) -> ChallengeRecord | None:
        """Return an active challenge and stage expired-challenge cleanup.

        Expired challenge deletion is flushed here so the current transaction sees
        the cleanup immediately. The caller remains responsible for committing or
        rolling back the session because challenge access often participates in a
        larger authentication transaction.
        """
        challenge = (
            await self._session.execute(
                select(IdentityAuthenticationChallenge).where(
                    IdentityAuthenticationChallenge.id == challenge_id,
                )
            )
        ).scalar_one_or_none()
        if challenge is None:
            return None

        now = current_timestamp()
        if now > challenge.expires_at:
            await self.consume_challenge(challenge_id)
            await self._session.flush()
            return None

        return ChallengeRecord(
            id=challenge.id,
            user_id=str(challenge.user_id),
            kind=cast(ChallengeKind, challenge.kind),
            expires_at=challenge.expires_at,
            metadata=challenge.metadata_payload or {},
        )

    async def consume_challenge(self, challenge_id: str) -> None:
        await self._session.execute(
            delete(IdentityAuthenticationChallenge).where(
                IdentityAuthenticationChallenge.id == challenge_id,
            )
        )


class SqlAlchemyTOTPCredentialStore:
    """Store TOTP credentials with status transitions and replay metadata."""

    def __init__(
        self,
        session: AsyncSession,
        secret_service: SecretEnvelopeService | None = None,
    ):
        self._session = session
        self._secret_service = secret_service or SecretEnvelopeService.from_env(
            os.environ
        )

    async def create_pending_totp_credential(
        self,
        user_id: str,
        secret: str,
    ) -> str:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            raise ValueError("User id must be a UUID string.")

        now = current_timestamp()
        await self._session.execute(
            delete(IdentityTotpCredential).where(
                IdentityTotpCredential.user_id == user_uuid,
                IdentityTotpCredential.status == TOTP_PENDING_STATUS,
            )
        )

        credential = IdentityTotpCredential(
            user_id=user_uuid,
            crypt_secret=self._secret_service.encrypt_required(secret),
            status=TOTP_PENDING_STATUS,
            created_at=now,
        )
        self._session.add(credential)
        await self._session.flush()
        return str(credential.id)

    async def get_totp_credential(
        self,
        credential_id: str,
    ) -> IdentityTotpCredential | None:
        credential_uuid = parse_uuid(credential_id)
        if credential_uuid is None:
            return None

        return (
            await self._session.execute(
                select(IdentityTotpCredential).where(
                    IdentityTotpCredential.id == credential_uuid,
                )
            )
        ).scalar_one_or_none()

    def decrypt_totp_secret(self, credential: IdentityTotpCredential) -> str:
        secret, _version = self._secret_service.decrypt_required(
            credential.crypt_secret
        )
        return secret

    async def _active_credential(
        self,
        user_uuid: UUID,
    ) -> IdentityTotpCredential | None:
        return (
            await self._session.execute(
                select(IdentityTotpCredential)
                .where(
                    IdentityTotpCredential.user_id == user_uuid,
                    IdentityTotpCredential.status == TOTP_ACTIVE_STATUS,
                )
                .order_by(desc(IdentityTotpCredential.created_at))
            )
        ).scalar_one_or_none()

    async def _pending_credential(
        self,
        user_uuid: UUID,
    ) -> IdentityTotpCredential | None:
        return (
            await self._session.execute(
                select(IdentityTotpCredential)
                .where(
                    IdentityTotpCredential.user_id == user_uuid,
                    IdentityTotpCredential.status == TOTP_PENDING_STATUS,
                )
                .order_by(desc(IdentityTotpCredential.created_at))
            )
        ).scalar_one_or_none()

    async def activate_totp_credential(self, credential_id: str) -> None:
        credential = await self.get_totp_credential(credential_id)
        if credential is None:
            raise ValueError("TOTP credential was not found.")

        now = current_timestamp()
        await self._session.execute(
            delete(IdentityTotpCredential).where(
                IdentityTotpCredential.user_id == credential.user_id,
                IdentityTotpCredential.status == TOTP_PENDING_STATUS,
                IdentityTotpCredential.id != credential.id,
            )
        )

        active_credential = await self._active_credential(credential.user_id)
        if active_credential is not None:
            active_credential.status = TOTP_DISABLED_STATUS
            active_credential.disabled_at = now

        credential.status = TOTP_ACTIVE_STATUS
        credential.activated_at = now

    async def disable_totp_credential(self, credential_id: str) -> None:
        credential = await self.get_totp_credential(credential_id)
        if credential is None:
            return

        if credential.status == TOTP_DISABLED_STATUS:
            return

        credential.status = TOTP_DISABLED_STATUS
        credential.disabled_at = current_timestamp()

    async def get_active_totp_credential(self, user_id: str) -> str | None:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return None

        credential = await self._active_credential(user_uuid)
        return str(credential.id) if credential is not None else None

    async def get_pending_totp_credential(self, user_id: str) -> str | None:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return None

        credential = await self._pending_credential(user_uuid)
        return str(credential.id) if credential is not None else None

    async def get_user_totp_credentials(
        self,
        user_id: str,
    ) -> list[IdentityTotpCredential]:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return []

        return list(
            (
                await self._session.execute(
                    select(IdentityTotpCredential).where(
                        IdentityTotpCredential.user_id == user_uuid,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def clear_totp_credentials(self, user_id: str) -> None:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return

        await self._session.execute(
            delete(IdentityTotpCredential).where(
                IdentityTotpCredential.user_id == user_uuid,
            )
        )


class SqlAlchemyRecoveryCodeStore:
    """Store and consume single-use recovery code verifiers."""

    def __init__(
        self,
        session: AsyncSession,
        secret_service: SecretEnvelopeService | None = None,
    ):
        self._session = session
        self._secret_service = secret_service or SecretEnvelopeService.from_env(
            os.environ
        )

    async def replace_recovery_codes(
        self,
        user_id: str,
        credential_id: str,
        recovery_codes: tuple[str, ...],
    ) -> None:
        credential_store = SqlAlchemyTOTPCredentialStore(
            self._session,
            self._secret_service,
        )
        credential = await credential_store.get_totp_credential(credential_id)
        if credential is None:
            raise ValueError("TOTP credential was not found.")

        user_uuid = parse_uuid(user_id)
        if user_uuid is None or credential.user_id != user_uuid:
            raise ValueError("TOTP credential does not belong to the user.")

        await self._session.execute(
            delete(IdentityTotpRecoveryCode).where(
                IdentityTotpRecoveryCode.credential_id == credential.id,
            )
        )

        now = current_timestamp()
        self._session.add_all(
            IdentityTotpRecoveryCode(
                credential_id=credential.id,
                code_verifier=create_recovery_code_verifier(
                    code,
                    self._secret_service,
                ),
                created_at=now,
            )
            for code in recovery_codes
        )

    async def consume_recovery_code(self, user_id: str, code: str) -> bool:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return False

        candidates = (
            (
                await self._session.execute(
                    select(IdentityTotpRecoveryCode)
                    .join(IdentityTotpCredential)
                    .where(
                        IdentityTotpCredential.user_id == user_uuid,
                        IdentityTotpCredential.status == TOTP_ACTIVE_STATUS,
                        IdentityTotpRecoveryCode.consumed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        candidate = next(
            (
                recovery_code
                for recovery_code in candidates
                if verify_recovery_code(
                    code,
                    recovery_code.code_verifier,
                    self._secret_service,
                )
            ),
            None,
        )

        if candidate is not None:
            candidate.consumed_at = current_timestamp()
            return True

        return False
