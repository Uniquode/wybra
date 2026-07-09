from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wybra.services.secrets import SecretSource

CredentialRotationRole = Literal["current", "previous"]


@dataclass(frozen=True, slots=True)
class CredentialReference:
    """Configured credential key metadata exposed by module settings."""

    name: str
    key: str
    owner: str
    description: str
    source: SecretSource
    required: bool = False
    rotation_role: CredentialRotationRole | None = None


__all__ = ("CredentialReference", "CredentialRotationRole")
