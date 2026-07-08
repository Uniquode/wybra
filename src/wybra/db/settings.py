from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

from wybra.config import ConfigService
from wybra.core.environment import environment_get
from wybra.core.exceptions import ConfigurationError
from wybra.db.config import DATABASE_CONFIG_SECTION
from wybra.db.urls import (
    DatabaseBackend,
    database_backend_for_scheme,
    database_backend_for_url,
    database_url_support_error,
    resolve_database_url,
    tortoise_database_url,
)
from wybra.services.secrets import (
    ENVIRONMENT_SOURCE,
    SecretsCapability,
    SecretsError,
    SecretSource,
    normalise_secret_source,
)

APP_CONFIG_SECTION = "app"
DATABASE_URL_FIELD = "database_url"
DATABASE_URL_SOURCE_ENVIRONMENT = "environment"
DatabaseConfigSource = Literal["url", "structured"]
CredentialPurpose = Literal["runtime", "service_account"]
RESERVED_OPTION_KEYS = frozenset(
    {
        "database",
        "file_path",
        "host",
        "password",
        "port",
        "user",
    }
)

logger = logging.getLogger(__name__)


class DatabaseConfigProtocol(Protocol):
    database_connection: ResolvedDatabaseConnection


@dataclass(frozen=True, slots=True)
class DatabaseCredentialConfig:
    user: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    user_key: str | None = field(default=None, repr=False)
    password_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user", _optional_non_blank_string(self.user, "user"))
        object.__setattr__(
            self,
            "password",
            _optional_non_blank_string(self.password, "password"),
        )
        object.__setattr__(
            self,
            "user_key",
            _optional_non_blank_string(self.user_key, "user_key"),
        )
        object.__setattr__(
            self,
            "password_key",
            _optional_non_blank_string(self.password_key, "password_key"),
        )
        _reject_plain_and_key(self.user, self.user_key, "user", "user_key")
        _reject_plain_and_key(
            self.password,
            self.password_key,
            "password",
            "password_key",
        )

    @property
    def has_keys(self) -> bool:
        return self.user_key is not None or self.password_key is not None

    @property
    def configured(self) -> bool:
        return any(
            value is not None
            for value in (
                self.user,
                self.password,
                self.user_key,
                self.password_key,
            )
        )


@dataclass(frozen=True, slots=True)
class ResolvedDatabaseCredentials:
    user: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class StructuredDatabaseConfig:
    backend: str
    database: str
    host: str | None = None
    port: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    credential_source: SecretSource | None = None
    runtime_credentials: DatabaseCredentialConfig = field(
        default_factory=DatabaseCredentialConfig,
        repr=False,
    )
    service_account_credentials: DatabaseCredentialConfig = field(
        default_factory=DatabaseCredentialConfig,
        repr=False,
    )

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, Any],
    ) -> StructuredDatabaseConfig:
        unknown_fields = sorted(set(values) - _structured_database_fields())
        if unknown_fields:
            raise ConfigurationError(
                "Unknown option(s) in [app.database] configuration: "
                + ", ".join(unknown_fields)
                + "."
            )

        runtime_credentials = DatabaseCredentialConfig(
            user=cast(str | None, values.get("user")),
            password=cast(str | None, values.get("password")),
            user_key=cast(str | None, values.get("user_key")),
            password_key=cast(str | None, values.get("password_key")),
        )
        service_account_credentials = DatabaseCredentialConfig(
            user=cast(str | None, values.get("sa_user")),
            password=cast(str | None, values.get("sa_password")),
            user_key=cast(str | None, values.get("sa_user_key")),
            password_key=cast(str | None, values.get("sa_password_key")),
        )

        return cls(
            backend=cast(str, values.get("backend")),
            database=cast(str, values.get("database")),
            host=cast(str | None, values.get("host")),
            port=cast(int | None, values.get("port")),
            options=cast(Mapping[str, Any], values.get("options") or {}),
            credential_source=cast(
                SecretSource | None,
                values.get("credential_source"),
            ),
            runtime_credentials=runtime_credentials,
            service_account_credentials=service_account_credentials,
        )

    def __post_init__(self) -> None:
        backend = _required_non_blank_string(self.backend, "backend")
        backend_info = database_backend_for_scheme(backend)
        if backend_info is None:
            raise ConfigurationError(database_url_support_error(f"{backend}://"))
        object.__setattr__(self, "backend", backend)
        object.__setattr__(
            self,
            "database",
            _required_non_blank_string(self.database, "database"),
        )
        object.__setattr__(self, "host", _optional_non_blank_string(self.host, "host"))
        object.__setattr__(self, "port", _optional_positive_int(self.port, "port"))
        object.__setattr__(
            self,
            "options",
            MappingProxyType(dict(_normalise_options(self.options))),
        )
        object.__setattr__(
            self,
            "credential_source",
            _optional_secret_source(self.credential_source),
        )
        _validate_database_credential_configuration(
            backend_info,
            self.credential_source,
            self.runtime_credentials,
            self.service_account_credentials,
        )
        _validate_database_backend_fields(backend_info, self.host, self.port)

    @property
    def backend_info(self) -> DatabaseBackend:
        backend = database_backend_for_scheme(self.backend)
        if backend is None:
            raise ConfigurationError(database_url_support_error(f"{self.backend}://"))
        return backend

    def requires_secret_capability_for(self, purpose: CredentialPurpose) -> bool:
        return (
            self.credential_source is not None
            and self.credential_source != ENVIRONMENT_SOURCE
            and self.credential_config(purpose).has_keys
        )

    def credential_config(self, purpose: CredentialPurpose) -> DatabaseCredentialConfig:
        if purpose == "runtime":
            return self.runtime_credentials
        return self.service_account_credentials


@dataclass(frozen=True, slots=True)
class EffectiveDatabaseConfig:
    source: DatabaseConfigSource
    project_root: Path
    database_url: str | None = None
    structured: StructuredDatabaseConfig | None = None

    @classmethod
    def from_url(
        cls,
        database_url: str,
        *,
        project_root: Path,
    ) -> EffectiveDatabaseConfig:
        resolved_url = _normalise_database_url(database_url, project_root)
        return cls(source="url", project_root=project_root, database_url=resolved_url)

    @classmethod
    def from_structured(
        cls,
        structured: StructuredDatabaseConfig,
        *,
        project_root: Path,
    ) -> EffectiveDatabaseConfig:
        return cls(
            source="structured",
            project_root=project_root.resolve(),
            structured=structured,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", self.project_root.resolve())
        if self.source == "url":
            if self.database_url is None:
                raise ConfigurationError("Database URL must be configured.")
            object.__setattr__(
                self,
                "database_url",
                _normalise_database_url(self.database_url, self.project_root),
            )
            if self.structured is not None:
                raise ConfigurationError(
                    "URL database config must not include structured settings."
                )
            return

        if self.source == "structured":
            if self.structured is None:
                raise ConfigurationError(
                    "Structured database config must include [app.database]."
                )
            if self.database_url is not None:
                raise ConfigurationError(
                    "Structured database config must not include database_url."
                )
            return

        raise ConfigurationError(f"Unsupported database config source: {self.source}.")

    def requires_secret_capability_for(self, purpose: CredentialPurpose) -> bool:
        return (
            self.structured.requires_secret_capability_for(purpose)
            if self.structured is not None
            else False
        )

    def resolve(
        self,
        *,
        environ: object | None = None,
        secrets: SecretsCapability | None = None,
        purpose: CredentialPurpose = "runtime",
    ) -> ResolvedDatabaseConnection:
        if self.source == "url":
            assert self.database_url is not None
            backend = database_backend_for_url(self.database_url)
            if backend is None:
                raise ConfigurationError(database_url_support_error(self.database_url))
            return ResolvedDatabaseConnection.from_url(
                self.database_url,
                backend=backend,
            )

        assert self.structured is not None
        return _resolve_structured_database_connection(
            self.structured,
            project_root=self.project_root,
            environ=environ,
            secrets=secrets,
            purpose=purpose,
        )


@dataclass(frozen=True, slots=True)
class ResolvedDatabaseConnection:
    source: DatabaseConfigSource
    backend: DatabaseBackend
    database_url: str | None = field(default=None, repr=False)
    credentials: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_url(
        cls,
        database_url: str,
        *,
        backend: DatabaseBackend,
    ) -> ResolvedDatabaseConnection:
        return cls(source="url", backend=backend, database_url=database_url)

    @classmethod
    def from_structured(
        cls,
        *,
        backend: DatabaseBackend,
        credentials: Mapping[str, Any],
    ) -> ResolvedDatabaseConnection:
        return cls(
            source="structured",
            backend=backend,
            credentials=MappingProxyType(dict(credentials)),
        )

    @property
    def tortoise_connection_config(self) -> str | dict[str, Any]:
        if self.database_url is not None:
            return tortoise_database_url(self.database_url)
        return {
            "engine": f"tortoise.backends.{self.backend.tortoise_scheme}",
            "credentials": dict(self.credentials),
        }

    @property
    def redacted_description(self) -> str:
        if self.database_url is not None:
            return f"database URL: {self.backend.scheme}://<redacted>"
        database = self.credentials.get("database") or self.credentials.get("file_path")
        return (
            "structured database config: "
            f"backend={self.backend.scheme}, database={database}"
        )


def effective_database_config_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    *,
    project_root: Path,
    configured_database_url: str | None = None,
    database_url_override: str | None = None,
) -> EffectiveDatabaseConfig | None:
    if database_url_override is not None:
        return EffectiveDatabaseConfig.from_url(
            database_url_override,
            project_root=project_root,
        )

    app_values = _section_values(config, APP_CONFIG_SECTION)
    structured_values = _section_values(config, DATABASE_CONFIG_SECTION)
    database_url = _configured_database_url(
        app_values.get(DATABASE_URL_FIELD),
        configured_database_url,
    )
    database_url_source = _field_source(
        config,
        f"{APP_CONFIG_SECTION}.{DATABASE_URL_FIELD}",
    )

    if (
        database_url is not None
        and database_url_source == DATABASE_URL_SOURCE_ENVIRONMENT
    ):
        if _structured_database_configured(structured_values):
            logger.info("Using DATABASE_URL, overriding config")
        return EffectiveDatabaseConfig.from_url(database_url, project_root=project_root)

    if _structured_database_configured(structured_values):
        if database_url is not None:
            logger.info(
                "[app.database] overrides [app].database_url; remove "
                "[app].database_url to avoid dead database configuration."
            )
        return EffectiveDatabaseConfig.from_structured(
            StructuredDatabaseConfig.from_values(structured_values),
            project_root=project_root,
        )

    if database_url is not None:
        return EffectiveDatabaseConfig.from_url(database_url, project_root=project_root)

    return None


def resolve_database_connection_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    *,
    project_root: Path,
    configured_database_url: str | None = None,
    database_url_override: str | None = None,
    secrets: SecretsCapability | None = None,
    purpose: CredentialPurpose = "runtime",
) -> ResolvedDatabaseConnection | None:
    effective = effective_database_config_from_config(
        config,
        project_root=project_root,
        configured_database_url=configured_database_url,
        database_url_override=database_url_override,
    )
    if effective is None:
        return None
    resolved_secrets = secrets
    if effective.requires_secret_capability_for(purpose) and resolved_secrets is None:
        resolved_secrets = _secrets_capability_from_config(config)
    return effective.resolve(
        environ=_config_environ(config),
        secrets=resolved_secrets,
        purpose=purpose,
    )


def resolve_database_provisioning_connection_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    *,
    project_root: Path,
    configured_database_url: str | None = None,
    admin_database_url: str | None = None,
    secrets: SecretsCapability | None = None,
) -> ResolvedDatabaseConnection:
    if admin_database_url is not None:
        connection = EffectiveDatabaseConfig.from_url(
            admin_database_url,
            project_root=project_root,
        ).resolve(environ=_config_environ(config), secrets=secrets)
        _reject_unsupported_provisioning_backend(connection)
        return connection

    connection = resolve_database_connection_from_config(
        config,
        project_root=project_root,
        configured_database_url=configured_database_url,
        secrets=secrets,
        purpose="service_account",
    )
    if connection is None:
        raise ConfigurationError(
            "Database provisioning requires [app.database] service-account "
            "credentials or an explicit admin database URL."
        )
    _reject_unsupported_provisioning_backend(connection)
    if connection.source == "url":
        raise ConfigurationError(
            "Database provisioning does not use application database_url "
            "configuration. Configure [app.database] service-account credentials "
            "or supply an explicit admin database URL."
        )
    if connection.credentials.get("user") is None:
        raise ConfigurationError(
            "Database provisioning requires a service-account database user "
            "or an explicit admin database URL."
        )
    if connection.credentials.get("password") is None:
        raise ConfigurationError(
            "Database provisioning requires a service-account database password "
            "or an explicit admin database URL."
        )
    return connection


def _validate_database_credential_configuration(
    backend: DatabaseBackend,
    credential_source: SecretSource | None,
    runtime_credentials: DatabaseCredentialConfig,
    service_account_credentials: DatabaseCredentialConfig,
) -> None:
    credentials_configured = (
        runtime_credentials.configured or service_account_credentials.configured
    )
    keys_configured = (
        runtime_credentials.has_keys or service_account_credentials.has_keys
    )
    if keys_configured and credential_source is None:
        raise ConfigurationError(
            "[app.database].credential_source is required when any database "
            "credential key is configured."
        )
    if backend.tortoise_scheme == "sqlite" and (
        credential_source is not None or credentials_configured
    ):
        raise ConfigurationError(
            "[app.database] credentials are not supported for the sqlite backend."
        )
    if credential_source is not None and not keys_configured:
        raise ConfigurationError("database configuration error, missing key fields")


def _validate_database_backend_fields(
    backend: DatabaseBackend,
    host: str | None,
    port: int | None,
) -> None:
    if backend.tortoise_scheme == "sqlite" and (host is not None or port is not None):
        raise ConfigurationError(
            "[app.database].host and [app.database].port are not supported for "
            "the sqlite backend."
        )


def _reject_unsupported_provisioning_backend(
    connection: ResolvedDatabaseConnection,
) -> None:
    if connection.backend.tortoise_scheme == "sqlite":
        raise ConfigurationError(
            "Database provisioning is not supported for the sqlite backend."
        )


def _resolve_structured_database_connection(
    structured: StructuredDatabaseConfig,
    *,
    project_root: Path,
    environ: object | None,
    secrets: SecretsCapability | None,
    purpose: CredentialPurpose,
) -> ResolvedDatabaseConnection:
    backend = structured.backend_info
    credentials = _structured_backend_credentials(
        structured,
        project_root=project_root,
    )
    resolved_credentials = _resolve_credentials(
        structured.credential_config(purpose),
        source=structured.credential_source,
        environ=environ,
        secrets=secrets,
        purpose=purpose,
    )
    if resolved_credentials.user is not None:
        credentials["user"] = resolved_credentials.user
    if resolved_credentials.password is not None:
        credentials["password"] = resolved_credentials.password
    return ResolvedDatabaseConnection.from_structured(
        backend=backend,
        credentials=credentials,
    )


def _structured_backend_credentials(
    structured: StructuredDatabaseConfig,
    *,
    project_root: Path,
) -> dict[str, Any]:
    credentials = dict(structured.options)
    conflicting_options = sorted(RESERVED_OPTION_KEYS & credentials.keys())
    if conflicting_options:
        raise ConfigurationError(
            "[app.database].options must not contain reserved database fields: "
            + ", ".join(conflicting_options)
            + "."
        )
    if structured.backend_info.tortoise_scheme == "sqlite":
        credentials["file_path"] = _sqlite_file_path(structured.database, project_root)
        return credentials

    credentials["database"] = structured.database
    if structured.host is not None:
        credentials["host"] = structured.host
    if structured.port is not None:
        credentials["port"] = structured.port
    return credentials


def _sqlite_file_path(database: str, project_root: Path) -> str:
    if database == ":memory:":
        return database
    path = Path(database)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve().as_posix()


def _resolve_credentials(
    credential_config: DatabaseCredentialConfig,
    *,
    source: SecretSource | None,
    environ: object | None,
    secrets: SecretsCapability | None,
    purpose: CredentialPurpose,
) -> ResolvedDatabaseCredentials:
    return ResolvedDatabaseCredentials(
        user=_resolve_credential_value(
            credential_config.user,
            credential_config.user_key,
            source=source,
            environ=environ,
            secrets=secrets,
            field_name=f"{purpose} database user",
        ),
        password=_resolve_credential_value(
            credential_config.password,
            credential_config.password_key,
            source=source,
            environ=environ,
            secrets=secrets,
            field_name=f"{purpose} database password",
        ),
    )


def _resolve_credential_value(
    plain_value: str | None,
    key: str | None,
    *,
    source: SecretSource | None,
    environ: object | None,
    secrets: SecretsCapability | None,
    field_name: str,
) -> str | None:
    if key is None:
        return plain_value
    if source is None:
        raise ConfigurationError(
            f"[app.database].credential_source is required for {field_name}."
        )
    if source == ENVIRONMENT_SOURCE:
        environment = environ if environ is not None else os.environ
        value = environment_get(environment, key)
        if value is None or not value.strip():
            raise ConfigurationError(
                f"Environment variable {key} for {field_name} must be set."
            )
        return value
    if secrets is None:
        raise ConfigurationError(
            f"SecretsCapability is required to resolve {field_name} from "
            f"source={source}, key={key}."
        )
    try:
        return secrets.resolve(source, key).reveal()
    except SecretsError as exc:
        raise ConfigurationError(
            f"Failed to resolve {field_name} from source={source}, key={key}."
        ) from exc


def _secrets_capability_from_config(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
) -> SecretsCapability:
    if not isinstance(config, ConfigService):
        raise ConfigurationError(
            "SecretsCapability is required to resolve database credentials from "
            "non-environment secret sources."
        )
    from wybra.secrets.capabilities import secrets_capability_from_config

    return secrets_capability_from_config(config)


def _config_environ(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
) -> object | None:
    if isinstance(config, ConfigService):
        return config.environ
    # Plain mappings do not carry an isolated environment; environment-source
    # credential keys fall back to os.environ in _resolve_credential_value.
    return None


def _section_values(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    section_name: str,
) -> dict[str, Any]:
    if isinstance(config, ConfigService):
        return dict(config.get_config(section_name) or {})
    configured_section = config.get(section_name)
    if configured_section is None:
        return {}
    if not isinstance(configured_section, Mapping):
        raise ConfigurationError(f"Config section {section_name!r} must be a table.")
    return dict(configured_section)


def _field_source(
    config: ConfigService | Mapping[str, Mapping[str, Any]],
    field_name: str,
) -> str | None:
    if not isinstance(config, ConfigService):
        return None
    return config.config.sources.get(field_name)


def _configured_database_url(
    configured_value: Any,
    fallback_value: str | None,
) -> str | None:
    value = configured_value if configured_value is not None else fallback_value
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigurationError("DATABASE_URL must not be blank.")


def _normalise_database_url(database_url: str, project_root: Path) -> str:
    if not isinstance(database_url, str) or not database_url.strip():
        raise ConfigurationError("DATABASE_URL must not be blank.")
    return resolve_database_url(database_url.strip(), project_root)


def _structured_database_configured(values: Mapping[str, Any]) -> bool:
    return any(value is not None for value in values.values())


def _structured_database_fields() -> frozenset[str]:
    return frozenset(
        {
            "backend",
            "host",
            "port",
            "database",
            "options",
            "credential_source",
            "user",
            "password",
            "user_key",
            "password_key",
            "sa_user",
            "sa_password",
            "sa_user_key",
            "sa_password_key",
        }
    )


def _normalise_options(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError("[app.database].options must be a table.")
    options: dict[str, Any] = {}
    for key, option_value in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigurationError(
                "[app.database].options keys must be non-blank strings."
            )
        options[key.strip()] = option_value
    return options


def _optional_secret_source(value: object) -> SecretSource | None:
    if value is None:
        return None
    return normalise_secret_source(value, name="[app.database].credential_source")


def _required_non_blank_string(value: object, field_name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigurationError(f"[app.database].{field_name} must be a non-blank string.")


def _optional_non_blank_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigurationError(f"[app.database].{field_name} must be a non-blank string.")


def _optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigurationError(
            f"[app.database].{field_name} must be a positive integer."
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"[app.database].{field_name} must be a positive integer."
            ) from exc
    else:
        raise ConfigurationError(
            f"[app.database].{field_name} must be a positive integer."
        )
    if parsed <= 0:
        raise ConfigurationError(
            f"[app.database].{field_name} must be a positive integer."
        )
    return parsed


def _reject_plain_and_key(
    plain_value: str | None,
    key_value: str | None,
    plain_field: str,
    key_field: str,
) -> None:
    if plain_value is None or key_value is None:
        return
    raise ConfigurationError(
        f"[app.database].{plain_field} and [app.database].{key_field} "
        "are mutually exclusive."
    )


__all__ = (
    "CredentialPurpose",
    "DatabaseConfigProtocol",
    "DatabaseConfigSource",
    "DatabaseCredentialConfig",
    "EffectiveDatabaseConfig",
    "ResolvedDatabaseConnection",
    "ResolvedDatabaseCredentials",
    "StructuredDatabaseConfig",
    "effective_database_config_from_config",
    "resolve_database_connection_from_config",
    "resolve_database_provisioning_connection_from_config",
)
