from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.models import (
    IdentityProvider,
    IdentityTotpCredential,
    IdentityTotpRecoveryCode,
)
from wybra.services.crypto import PLAIN_TEXT_VERSION, SecretEnvelopeService


@dataclass(frozen=True, slots=True)
class ReencryptedSecretVersionCount:
    version: str
    count: int


@dataclass(frozen=True, slots=True)
class ReencryptedSecretFieldStats:
    table: str
    field: str
    scanned: int
    rewritten: int
    skipped_current: int
    skipped_plaintext: int
    versions: tuple[ReencryptedSecretVersionCount, ...]


@dataclass(frozen=True, slots=True)
class ReencryptSecretsResult:
    fields: tuple[ReencryptedSecretFieldStats, ...]
    unsupported_recovery_code_verifiers: int
    dry_run: bool

    @property
    def scanned(self) -> int:
        return sum(field.scanned for field in self.fields)

    @property
    def rewritten(self) -> int:
        return sum(field.rewritten for field in self.fields)

    @property
    def skipped_current(self) -> int:
        return sum(field.skipped_current for field in self.fields)

    @property
    def skipped_plaintext(self) -> int:
        return sum(field.skipped_plaintext for field in self.fields)


@dataclass(frozen=True, slots=True)
class _SecretFieldSpec:
    model: type[Any]
    table: str
    field: str


_REVERSIBLE_SECRET_FIELDS = (
    _SecretFieldSpec(
        IdentityProvider,
        IdentityProvider.__tablename__,
        "crypt_access_token",
    ),
    _SecretFieldSpec(
        IdentityProvider,
        IdentityProvider.__tablename__,
        "crypt_refresh_token",
    ),
    _SecretFieldSpec(
        IdentityTotpCredential,
        IdentityTotpCredential.__tablename__,
        "crypt_secret",
    ),
)


async def reencrypt_persisted_secrets(
    session: AsyncSession,
    secret_service: SecretEnvelopeService,
    *,
    dry_run: bool = False,
) -> ReencryptSecretsResult:
    """Re-encrypt reversible persisted secret envelopes with the current key."""

    current_version = secret_service.current_version_required()
    fields: list[ReencryptedSecretFieldStats] = []
    for spec in _REVERSIBLE_SECRET_FIELDS:
        fields.append(
            await _reencrypt_secret_field(
                session,
                spec,
                secret_service,
                current_version=current_version,
                dry_run=dry_run,
            )
        )

    unsupported_recovery_code_verifiers = await _count_recovery_code_verifiers(session)
    result = ReencryptSecretsResult(
        fields=tuple(fields),
        unsupported_recovery_code_verifiers=unsupported_recovery_code_verifiers,
        dry_run=dry_run,
    )
    if not dry_run and result.rewritten:
        await session.commit()
    return result


async def _reencrypt_secret_field(
    session: AsyncSession,
    spec: _SecretFieldSpec,
    secret_service: SecretEnvelopeService,
    *,
    current_version: str,
    dry_run: bool,
) -> ReencryptedSecretFieldStats:
    result = await session.execute(select(spec.model))
    scanned = 0
    rewritten = 0
    skipped_current = 0
    skipped_plaintext = 0
    versions: dict[str, int] = {}

    for row in result.scalars():
        value = getattr(row, spec.field)
        if value is None:
            continue

        scanned += 1
        plaintext, version = secret_service.decrypt_required(value)
        versions[version] = versions.get(version, 0) + 1
        if version == current_version:
            skipped_current += 1
            continue
        if version == PLAIN_TEXT_VERSION:
            skipped_plaintext += 1
            continue

        rewritten += 1
        if not dry_run:
            setattr(row, spec.field, secret_service.encrypt_required(plaintext))

    return ReencryptedSecretFieldStats(
        table=spec.table,
        field=spec.field,
        scanned=scanned,
        rewritten=rewritten,
        skipped_current=skipped_current,
        skipped_plaintext=skipped_plaintext,
        versions=tuple(
            ReencryptedSecretVersionCount(version=version, count=count)
            for version, count in sorted(versions.items())
        ),
    )


async def _count_recovery_code_verifiers(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count()).select_from(IdentityTotpRecoveryCode)
    )
    return int(count or 0)


__all__ = (
    "ReencryptedSecretFieldStats",
    "ReencryptedSecretVersionCount",
    "ReencryptSecretsResult",
    "reencrypt_persisted_secrets",
)
