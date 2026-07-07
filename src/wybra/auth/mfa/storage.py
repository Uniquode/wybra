from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Literal
from uuid import UUID

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.recovery import (
    create_recovery_code_verifier,
    verify_recovery_code,
)
from wybra.auth.mfa.totp import is_valid_totp_code, verify_totp
from wybra.auth.models import (
    IdentityAuthenticationChallenge,
    IdentityTotpCredential,
    IdentityTotpRecoveryCode,
    IdentityWebAuthnCredential,
)
from wybra.auth.persistence.contracts import (
    AuthPersistenceError,
    ChallengeKind,
    ChallengeRecord,
    ChallengeStore,
    RecoveryCodeStore,
    TOTPCredentialStore,
    WebAuthnCredentialRecord,
    WebAuthnCredentialStore,
)
from wybra.auth.timestamps import current_timestamp
from wybra.services.crypto import (
    SecretDataError,
    SecretEnvelopeService,
    SecretMaterialMissingError,
)

TOTP_ACTIVE_STATUS: Literal["active"] = "active"
TOTP_DISABLED_STATUS: Literal["disabled"] = "disabled"
TOTP_PENDING_STATUS: Literal["pending"] = "pending"
WEBAUTHN_ACTIVE_STATUS: Literal["active"] = "active"
WEBAUTHN_REVOKED_STATUS: Literal["revoked"] = "revoked"
TOTP_CODE_REPLAY_MESSAGE = "Authenticator code has already been used."
logger = logging.getLogger(__name__)


class TortoiseChallengeStore:
    """Store challenge state in application tables."""

    def __init__(self, connection: BaseDBAsyncClient):
        self._connection = connection

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
        await IdentityAuthenticationChallenge.create(
            id=challenge_id,
            user_id=user_uuid,
            kind=kind,
            expires_at=expires_at,
            metadata_payload=metadata_payload,
            using_db=self._connection,
        )
        return ChallengeRecord(
            id=challenge_id,
            user_id=user_id,
            kind=kind,
            expires_at=expires_at,
            metadata=metadata_payload,
        )

    async def get_challenge(self, challenge_id: str) -> ChallengeRecord | None:
        challenge = await IdentityAuthenticationChallenge.get_or_none(
            id=challenge_id,
            using_db=self._connection,
        )
        if challenge is None:
            return None

        if current_timestamp() > challenge.expires_at:
            await self.consume_challenge(challenge_id)
            return None

        return ChallengeRecord(
            id=challenge.id,
            user_id=str(challenge.user_id),
            kind=challenge.kind,  # type: ignore[arg-type]
            expires_at=challenge.expires_at,
            metadata=challenge.metadata_payload or {},
        )

    async def consume_challenge(self, challenge_id: str) -> None:
        await (
            IdentityAuthenticationChallenge.filter(
                id=challenge_id,
            )
            .using_db(self._connection)
            .delete()
        )


class TortoiseTOTPCredentialStore:
    """Store TOTP credentials with status transitions and replay metadata."""

    def __init__(
        self,
        connection: BaseDBAsyncClient,
        secret_service: SecretEnvelopeService | None = None,
    ):
        self._connection = connection
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
        await (
            IdentityTotpCredential.filter(
                user_id=user_uuid,
                status=TOTP_PENDING_STATUS,
            )
            .using_db(self._connection)
            .delete()
        )

        try:
            credential = await IdentityTotpCredential.create(
                user_id=user_uuid,
                crypt_secret=self._secret_service.encrypt_required(secret),
                status=TOTP_PENDING_STATUS,
                created_at=now,
                using_db=self._connection,
            )
        except IntegrityError as exc:
            raise AuthPersistenceError("TOTP credential could not be stored.") from exc
        return str(credential.id)

    async def get_totp_credential(
        self,
        credential_id: str,
    ) -> IdentityTotpCredential | None:
        credential_uuid = parse_uuid(credential_id)
        if credential_uuid is None:
            return None

        return await IdentityTotpCredential.get_or_none(
            id=credential_uuid,
            using_db=self._connection,
        )

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
            await IdentityTotpCredential.filter(
                user_id=user_uuid,
                status=TOTP_ACTIVE_STATUS,
            )
            .using_db(self._connection)
            .order_by("-created_at")
            .first()
        )

    async def _pending_credential(
        self,
        user_uuid: UUID,
    ) -> IdentityTotpCredential | None:
        return (
            await IdentityTotpCredential.filter(
                user_id=user_uuid,
                status=TOTP_PENDING_STATUS,
            )
            .using_db(self._connection)
            .order_by("-created_at")
            .first()
        )

    async def activate_totp_credential(self, credential_id: str) -> None:
        credential = await self.get_totp_credential(credential_id)
        if credential is None:
            raise ValueError("TOTP credential was not found.")

        now = current_timestamp()
        await (
            IdentityTotpCredential.filter(
                user_id=credential.user_id,
                status=TOTP_PENDING_STATUS,
            )
            .exclude(id=credential.id)
            .using_db(self._connection)
            .delete()
        )

        active_credential = await self._active_credential(credential.user_id)
        if active_credential is not None:
            active_credential.status = TOTP_DISABLED_STATUS
            active_credential.disabled_at = now
            await active_credential.save(using_db=self._connection)

        credential.status = TOTP_ACTIVE_STATUS
        credential.activated_at = now
        await credential.save(using_db=self._connection)

    async def disable_totp_credential(self, credential_id: str) -> None:
        credential = await self.get_totp_credential(credential_id)
        if credential is None or credential.status == TOTP_DISABLED_STATUS:
            return

        credential.status = TOTP_DISABLED_STATUS
        credential.disabled_at = current_timestamp()
        await credential.save(using_db=self._connection)

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
            await IdentityTotpCredential.filter(
                user_id=user_uuid,
            ).using_db(self._connection)
        )

    async def clear_totp_credentials(self, user_id: str) -> None:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return

        await (
            IdentityTotpCredential.filter(
                user_id=user_uuid,
            )
            .using_db(self._connection)
            .delete()
        )

    async def verify_totp_credential(
        self,
        *,
        credential_id: str,
        user_id: str,
        code: str,
        period_seconds: int,
        allowed_drift: int,
        expected_status: str = TOTP_ACTIVE_STATUS,
        timestamp: float | None = None,
    ) -> tuple[bool, int | None, str | None]:
        parsed_credential_id = parse_uuid(credential_id)
        if parsed_credential_id is None or not is_valid_totp_code(code):
            return False, None, None

        credential = (
            await IdentityTotpCredential.filter(id=parsed_credential_id)
            .using_db(self._connection)
            .select_for_update()
            .first()
        )
        if (
            credential is None
            or str(credential.user_id) != user_id
            or credential.status != expected_status
        ):
            return False, None, None

        verification_time = current_timestamp() if timestamp is None else timestamp
        try:
            secret = self.decrypt_totp_secret(credential)
        except (SecretDataError, SecretMaterialMissingError) as exc:
            logger.error(
                "Unable to verify TOTP credential because secret material "
                "is unavailable or invalid: credential_id=%s user_id=%s",
                credential_id,
                user_id,
                exc_info=exc,
            )
            return False, None, None

        accepted, counter = verify_totp(
            secret,
            code,
            timestamp=verification_time,
            period=period_seconds,
            allowed_drift=allowed_drift,
        )
        if not accepted or counter is None:
            return False, None, None

        last_used_counter = getattr(credential, "last_used_counter", None)
        if last_used_counter is not None and counter <= last_used_counter:
            return False, None, TOTP_CODE_REPLAY_MESSAGE

        updated = (
            await IdentityTotpCredential.filter(
                id=credential.id,
                user_id=credential.user_id,
                status=expected_status,
            )
            .filter(
                Q(last_used_counter__isnull=True) | Q(last_used_counter__lt=counter),
            )
            .using_db(self._connection)
            .update(last_used_counter=counter)
        )
        if updated != 1:
            return False, None, TOTP_CODE_REPLAY_MESSAGE
        return True, counter, None


class TortoiseRecoveryCodeStore:
    """Store and consume single-use recovery code verifiers."""

    def __init__(
        self,
        connection: BaseDBAsyncClient,
        secret_service: SecretEnvelopeService | None = None,
    ):
        self._connection = connection
        self._secret_service = secret_service or SecretEnvelopeService.from_env(
            os.environ
        )

    async def replace_recovery_codes(
        self,
        user_id: str,
        credential_id: str,
        recovery_codes: tuple[str, ...],
    ) -> None:
        credential_store = TortoiseTOTPCredentialStore(
            self._connection,
            self._secret_service,
        )
        credential = await credential_store.get_totp_credential(credential_id)
        if credential is None:
            raise ValueError("TOTP credential was not found.")

        user_uuid = parse_uuid(user_id)
        if user_uuid is None or credential.user_id != user_uuid:
            raise ValueError("TOTP credential does not belong to the user.")

        await (
            IdentityTotpRecoveryCode.filter(
                credential_id=credential.id,
            )
            .using_db(self._connection)
            .delete()
        )

        now = current_timestamp()
        await IdentityTotpRecoveryCode.bulk_create(
            [
                IdentityTotpRecoveryCode(
                    credential_id=credential.id,
                    code_verifier=create_recovery_code_verifier(
                        code,
                        self._secret_service,
                    ),
                    created_at=now,
                )
                for code in recovery_codes
            ],
            using_db=self._connection,
        )

    async def consume_recovery_code(self, user_id: str, code: str) -> bool:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return False

        credential_ids = (
            await IdentityTotpCredential.filter(
                user_id=user_uuid,
                status=TOTP_ACTIVE_STATUS,
            )
            .using_db(self._connection)
            .values_list("id", flat=True)
        )
        if not credential_ids:
            return False

        candidates = (
            await IdentityTotpRecoveryCode.filter(
                credential_id__in=tuple(credential_ids),
                consumed_at__isnull=True,
            )
            .using_db(self._connection)
            .select_for_update()
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

        if candidate is None:
            return False

        updated = (
            await IdentityTotpRecoveryCode.filter(
                id=candidate.id,
                consumed_at__isnull=True,
            )
            .using_db(self._connection)
            .update(consumed_at=current_timestamp())
        )
        return updated == 1


class TortoiseWebAuthnCredentialStore:
    """Store WebAuthn credentials and authentication metadata."""

    def __init__(self, connection: BaseDBAsyncClient):
        self._connection = connection

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
    ) -> str:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            raise ValueError("User id must be a UUID string.")

        credential = await IdentityWebAuthnCredential.create(
            user_id=user_uuid,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            status=WEBAUTHN_ACTIVE_STATUS,
            label=_normalise_webauthn_label(label),
            created_at=current_timestamp(),
            user_verified=user_verified,
            credential_device_type=credential_device_type,
            credential_backed_up=credential_backed_up,
            transports=list(transports) if transports else None,
            aaguid=aaguid,
            attestation_format=attestation_format,
            using_db=self._connection,
        )
        return str(credential.id)

    async def get_webauthn_credential(
        self,
        credential_id: str,
    ) -> WebAuthnCredentialRecord | None:
        credential = (
            await IdentityWebAuthnCredential.filter(credential_id=credential_id)
            .using_db(self._connection)
            .select_for_update()
            .first()
        )
        return _webauthn_record(credential)

    async def get_user_webauthn_credential(
        self,
        user_id: str,
        row_id: str,
    ) -> WebAuthnCredentialRecord | None:
        user_uuid = parse_uuid(user_id)
        row_uuid = parse_uuid(row_id)
        if user_uuid is None or row_uuid is None:
            return None

        credential = await IdentityWebAuthnCredential.get_or_none(
            id=row_uuid,
            user_id=user_uuid,
            using_db=self._connection,
        )
        return _webauthn_record(credential)

    async def list_active_webauthn_credentials(
        self,
        user_id: str,
    ) -> tuple[WebAuthnCredentialRecord, ...]:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return ()

        credentials = (
            await IdentityWebAuthnCredential.filter(
                user_id=user_uuid,
                status=WEBAUTHN_ACTIVE_STATUS,
            )
            .using_db(self._connection)
            .order_by("-created_at")
        )
        return tuple(
            record
            for credential in credentials
            if (record := _webauthn_record(credential)) is not None
        )

    async def count_active_webauthn_credentials(
        self,
        user_id: str,
        *,
        exclude_row_id: str | UUID | None = None,
    ) -> int:
        user_uuid = parse_uuid(user_id)
        if user_uuid is None:
            return 0

        query = IdentityWebAuthnCredential.filter(
            user_id=user_uuid,
            status=WEBAUTHN_ACTIVE_STATUS,
        ).using_db(self._connection)
        excluded_uuid = (
            parse_uuid(exclude_row_id) if exclude_row_id is not None else None
        )
        if excluded_uuid is not None:
            query = query.exclude(id=excluded_uuid)
        return await query.count()

    async def update_webauthn_sign_count(
        self,
        credential_id: str,
        sign_count: int,
    ) -> None:
        credential = await self._credential(credential_id, for_update=True)
        if credential is None:
            return

        credential.sign_count = sign_count
        await credential.save(using_db=self._connection)

    async def update_webauthn_authentication(
        self,
        credential_id: str,
        *,
        sign_count: int,
        user_verified: bool,
        credential_device_type: str | None,
        credential_backed_up: bool,
    ) -> None:
        credential = await self._credential(credential_id, for_update=True)
        if credential is None:
            return

        credential.sign_count = sign_count
        credential.last_used_at = current_timestamp()
        credential.user_verified = user_verified
        credential.credential_device_type = credential_device_type
        credential.credential_backed_up = credential_backed_up
        await credential.save(using_db=self._connection)

    async def revoke_webauthn_credential(
        self,
        user_id: str,
        row_id: str,
    ) -> bool:
        user_uuid = parse_uuid(user_id)
        row_uuid = parse_uuid(row_id)
        if user_uuid is None or row_uuid is None:
            return False

        credential = await (
            IdentityWebAuthnCredential.filter(
                id=row_uuid,
                user_id=user_uuid,
                status=WEBAUTHN_ACTIVE_STATUS,
            )
            .using_db(self._connection)
            .select_for_update()
            .first()
        )
        if credential is None:
            return False

        credential.status = WEBAUTHN_REVOKED_STATUS
        credential.revoked_at = current_timestamp()
        await credential.save(using_db=self._connection)
        return True

    async def _credential(
        self,
        credential_id: str,
        *,
        for_update: bool = False,
    ) -> IdentityWebAuthnCredential | None:
        query = IdentityWebAuthnCredential.filter(credential_id=credential_id).using_db(
            self._connection,
        )
        if for_update:
            query = query.select_for_update()
        return await query.first()


def _normalise_webauthn_label(label: str | None) -> str | None:
    if label is None:
        return None
    normalised = " ".join(label.split())
    return normalised[:120] if normalised else None


def _webauthn_record(
    credential: IdentityWebAuthnCredential | None,
) -> WebAuthnCredentialRecord | None:
    if credential is None:
        return None

    return WebAuthnCredentialRecord(
        id=str(credential.id),
        user_id=str(credential.user_id),
        credential_id=credential.credential_id,
        public_key=bytes(credential.public_key),
        sign_count=credential.sign_count,
        status=credential.status,
        label=credential.label,
        created_at=credential.created_at,
        last_used_at=credential.last_used_at,
        revoked_at=credential.revoked_at,
        user_verified=credential.user_verified,
        credential_device_type=credential.credential_device_type,
        credential_backed_up=credential.credential_backed_up,
        transports=tuple(credential.transports or ()),
        aaguid=credential.aaguid,
        attestation_format=credential.attestation_format,
    )


__all__ = (
    "ChallengeKind",
    "ChallengeRecord",
    "ChallengeStore",
    "RecoveryCodeStore",
    "TOTP_ACTIVE_STATUS",
    "TOTP_CODE_REPLAY_MESSAGE",
    "TOTP_DISABLED_STATUS",
    "TOTP_PENDING_STATUS",
    "TOTPCredentialStore",
    "TortoiseChallengeStore",
    "TortoiseRecoveryCodeStore",
    "TortoiseTOTPCredentialStore",
    "TortoiseWebAuthnCredentialStore",
    "WEBAUTHN_ACTIVE_STATUS",
    "WEBAUTHN_REVOKED_STATUS",
    "WebAuthnCredentialRecord",
    "WebAuthnCredentialStore",
)
