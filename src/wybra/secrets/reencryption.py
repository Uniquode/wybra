from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tortoise.backends.base.client import BaseDBAsyncClient

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


# Internal registry of Wybra-owned reversible encrypted fields. Add new
# WYBRA:SECRET columns here with focused dry-run and rewrite coverage.
_REVERSIBLE_SECRET_FIELDS = (
    _SecretFieldSpec(
        IdentityProvider,
        IdentityProvider._meta.db_table,
        "crypt_access_token",
    ),
    _SecretFieldSpec(
        IdentityProvider,
        IdentityProvider._meta.db_table,
        "crypt_refresh_token",
    ),
    _SecretFieldSpec(
        IdentityTotpCredential,
        IdentityTotpCredential._meta.db_table,
        "crypt_secret",
    ),
)


async def reencrypt_persisted_secrets(
    connection: BaseDBAsyncClient,
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
                connection,
                spec,
                secret_service,
                current_version=current_version,
                dry_run=dry_run,
            )
        )

    unsupported_recovery_code_verifiers = await _count_recovery_code_verifiers(
        connection
    )
    result = ReencryptSecretsResult(
        fields=tuple(fields),
        unsupported_recovery_code_verifiers=unsupported_recovery_code_verifiers,
        dry_run=dry_run,
    )
    return result


async def _reencrypt_secret_field(
    connection: BaseDBAsyncClient,
    spec: _SecretFieldSpec,
    secret_service: SecretEnvelopeService,
    *,
    current_version: str,
    dry_run: bool,
) -> ReencryptedSecretFieldStats:
    scanned = 0
    rewritten = 0
    skipped_current = 0
    skipped_plaintext = 0
    versions: dict[str, int] = {}

    for row in await spec.model.all().using_db(connection):
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
            await row.save(using_db=connection)

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


async def _count_recovery_code_verifiers(connection: BaseDBAsyncClient) -> int:
    return await IdentityTotpRecoveryCode.all().using_db(connection).count()


__all__ = (
    "ReencryptedSecretFieldStats",
    "ReencryptedSecretVersionCount",
    "ReencryptSecretsResult",
    "reencrypt_persisted_secrets",
)
