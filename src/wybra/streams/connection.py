from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

from wybra.core.exceptions import ConfigurationError
from wybra.services.secrets import SecretsCapability, SecretsError
from wybra.streams.settings import StreamsSettings


def resolve_redis_url(
    settings: StreamsSettings,
    secrets: SecretsCapability | None,
) -> str:
    url_reference = settings.url_reference
    credentials_reference = settings.credentials_reference
    if url_reference is not None:
        return _resolve_reference(secrets, url_reference, label="Redis URL")
    if settings.url is None:
        raise ConfigurationError("Streams Redis URL is not configured.")
    if credentials_reference is None:
        return settings.url
    credentials = _resolve_reference(
        secrets,
        credentials_reference,
        label="Redis credentials",
    )
    return _with_credentials(settings.url, credentials)


def resolve_nats_connection_options(
    settings: StreamsSettings,
    secrets: SecretsCapability | None,
) -> dict[str, str]:
    authentication = settings.authentication
    reference = settings.authentication_reference
    if authentication is None or reference is None:
        return {}
    value = _resolve_reference(secrets, reference, label="NATS authentication")
    if authentication == "token":
        return {"token": value}
    username, separator, password = value.partition(":")
    if not separator or not username or not password:
        raise ConfigurationError("Streams NATS user-password authentication must use username:password form.")
    return {"user": username, "password": password}


def _resolve_reference(
    secrets: SecretsCapability | None,
    reference: tuple[str, str],
    *,
    label: str,
) -> str:
    source, key = reference
    if secrets is None:
        raise ConfigurationError(f"Streams {label} reference requires the wybra.secrets module.")
    try:
        value = secrets.resolve(source, key).reveal()
    except SecretsError:
        raise ConfigurationError(f"Streams {label} secret resolution failed for source {source!r}.") from None
    if not value.strip():
        raise ConfigurationError(f"Streams {label} secret is blank.")
    return value


def _with_credentials(endpoint: str, credentials: str) -> str:
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise ConfigurationError("Streams Redis endpoint is invalid.") from None
    if parsed.scheme.lower() not in {"redis", "rediss"} or not parsed.netloc:
        raise ConfigurationError("Streams Redis endpoint cannot accept credentials.")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("Streams Redis endpoint already contains credentials.")
    username, separator, password = credentials.partition(":")
    if not separator or (not username and not password):
        raise ConfigurationError("Streams Redis credentials must use username:password form.")
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{userinfo}@{parsed.netloc}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


__all__ = ("resolve_nats_connection_options", "resolve_redis_url")
