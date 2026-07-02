from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast

import jwt
from jwt import PyJWTError


class OIDCJwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


type OIDCJwksClientFactory = Callable[[str], OIDCJwksClient]


def oidc_id_token_payload(
    id_token: str,
    *,
    jwks_uri: str,
    audience: str,
    issuer: str,
    jwks_client_factory: OIDCJwksClientFactory,
    error_type: type[Exception],
    missing_message: str,
    invalid_message: str,
    invalid_payload_message: str,
    algorithms: Sequence[str] = ("RS256",),
    required_claims: Sequence[str] = ("aud", "exp", "iss", "sub"),
) -> Mapping[str, object]:
    if not isinstance(id_token, str) or not id_token.strip():
        raise error_type(missing_message)
    try:
        signing_key = jwks_client_factory(jwks_uri).get_signing_key_from_jwt(id_token)
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=tuple(algorithms),
            audience=audience,
            issuer=issuer,
            options={"require": list(required_claims)},
        )
    except PyJWTError as exc:
        raise error_type(invalid_message) from exc
    if not isinstance(payload, dict):
        raise error_type(invalid_payload_message)
    return cast(Mapping[str, object], payload)


__all__ = (
    "OIDCJwksClient",
    "OIDCJwksClientFactory",
    "oidc_id_token_payload",
)
