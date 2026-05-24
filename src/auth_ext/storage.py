from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

ChallengeKind = Literal["totp", "webauthn", "recovery-code"]


@dataclass(frozen=True, slots=True)
class ChallengeRecord:
    id: str
    user_id: str
    kind: ChallengeKind
    expires_at: datetime
    metadata: dict[str, Any]


@runtime_checkable
class ChallengeStore(Protocol):
    async def create_challenge(
        self,
        user_id: str,
        kind: ChallengeKind,
        expires_at: datetime,
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
        code_hashes: tuple[str, ...],
    ) -> None: ...

    async def consume_recovery_code(self, user_id: str, code: str) -> bool: ...
