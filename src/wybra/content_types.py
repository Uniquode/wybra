"""Derived metadata for configured Tortoise models."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import inflect
from tortoise.models import Model

from wybra.db import DatabaseCapability
from wybra.site import Site, SiteCapabilityError

DEFAULT_CONTENT_ACTIONS = frozenset({"list", "view", "create", "update", "delete"})
_INFLECT = inflect.engine()


class ContentTypeError(RuntimeError):
    """Raised when derived content-type metadata is invalid."""


class UnknownContentTypeError(ContentTypeError):
    """Raised when a content type cannot be resolved from the registry."""


@runtime_checkable
class ContentTypesCapability(Protocol):
    """Public content-type metadata capability exposed through ``Site``."""

    def for_identifier(self, identifier: str) -> ContentType: ...

    def for_model(self, model: type[Model]) -> ContentType: ...


@dataclass(frozen=True, slots=True)
class ContentType:
    """Canonical source metadata for one configured Tortoise model."""

    identifier: str
    model: type[Model]
    verbose_name: str
    verbose_name_plural: str
    actions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ContentTypeRegistry:
    """Site-scoped content types indexed by identifier and model."""

    _by_identifier: Mapping[str, ContentType]
    _by_model: Mapping[type[Model], ContentType]

    @classmethod
    def from_models(cls, models: Iterable[type[Model]]) -> ContentTypeRegistry:
        """Derive a registry from finalised Tortoise model classes."""
        by_identifier: dict[str, ContentType] = {}
        by_model: dict[type[Model], ContentType] = {}
        for model in models:
            if model._meta.abstract:
                continue
            content_type = _content_type_for_model(model)
            previous = by_identifier.get(content_type.identifier)
            if previous is not None and previous.model is not model:
                raise ContentTypeError(
                    "Duplicate content type identifier: "
                    f"{content_type.identifier} for "
                    f"{previous.model.__name__} and {model.__name__}."
                )
            by_identifier[content_type.identifier] = content_type
            by_model[model] = content_type
        return cls(by_identifier, by_model)

    def for_identifier(self, identifier: str) -> ContentType:
        """Return metadata for a derived identifier."""
        try:
            return self._by_identifier[identifier]
        except KeyError as exc:
            raise UnknownContentTypeError(
                f"Unknown content type identifier: {identifier}."
            ) from exc

    def for_model(self, model: type[Model]) -> ContentType:
        """Return metadata for a configured model class."""
        try:
            return self._by_model[model]
        except KeyError as exc:
            raise UnknownContentTypeError(
                f"Model has no registered content type: {model.__name__}."
            ) from exc


@dataclass(slots=True)
class SiteContentTypesCapability:
    """Content-type capability finalised from one site's database inventory."""

    _registry: ContentTypeRegistry | None = None

    def finalise(self, database: DatabaseCapability) -> None:
        self._registry = ContentTypeRegistry.from_models(database.models())

    def for_identifier(self, identifier: str) -> ContentType:
        return self._require_registry().for_identifier(identifier)

    def for_model(self, model: type[Model]) -> ContentType:
        return self._require_registry().for_model(model)

    def _require_registry(self) -> ContentTypeRegistry:
        if self._registry is None:
            raise SiteCapabilityError(
                "Content types capability has not been finalised."
            )
        return self._registry


async def setup_site(site: Site) -> None:
    """Provide an unfinalised site-local content-type capability."""
    site.provide_capability(ContentTypesCapability, SiteContentTypesCapability())


async def post_setup_site(site: Site) -> None:
    """Populate content types after all configured modules have set up."""
    capability = site.require_capability(ContentTypesCapability)
    if not isinstance(capability, SiteContentTypesCapability):
        raise SiteCapabilityError(
            "Content types capability has an unsupported provider."
        )
    capability.finalise(site.require_capability(DatabaseCapability))


def _content_type_for_model(model: type[Model]) -> ContentType:
    meta = model._meta
    if not meta.app or not meta.db_table:
        raise ContentTypeError(
            f"Model has no finalised app/table identity: {model.__name__}."
        )
    verbose_name = _model_meta_value(model, "verbose_name") or _humanise(model.__name__)
    verbose_name_plural = _model_meta_value(model, "verbose_name_plural") or _pluralise(
        verbose_name
    )
    actions = _effective_actions(model)
    identifier_parts = (meta.app, meta.schema, meta.db_table)
    if any("." in part for part in identifier_parts[1:] if part):
        raise ContentTypeError(
            f"Model schema and table names must not contain dots: {model.__name__}."
        )
    identifier = ".".join(part for part in identifier_parts if part)
    return ContentType(identifier, model, verbose_name, verbose_name_plural, actions)


def _model_meta_value(model: type[Model], name: str) -> str | None:
    value = getattr(model.Meta, name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContentTypeError(
            f"Model Meta.{name} must be a non-empty string: {model.__name__}."
        )
    return value


def _effective_actions(model: type[Model]) -> frozenset[str]:
    selected = _model_meta_actions(model, "content_actions", DEFAULT_CONTENT_ACTIONS)
    excluded = _model_meta_actions(model, "content_exclude", frozenset())
    unknown = (selected | excluded) - DEFAULT_CONTENT_ACTIONS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ContentTypeError(
            f"Model Meta contains unknown content action(s): {names} "
            f"for {model.__name__}."
        )
    return selected - excluded


def _model_meta_actions(
    model: type[Model],
    name: str,
    default: frozenset[str],
) -> frozenset[str]:
    value = getattr(model.Meta, name, default)
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ContentTypeError(
            f"Model Meta.{name} must be an iterable of action names: {model.__name__}."
        )
    values = frozenset(value)
    if not all(isinstance(action, str) for action in values):
        raise ContentTypeError(
            f"Model Meta.{name} must contain only action names: {model.__name__}."
        )
    return values


def _humanise(name: str) -> str:
    words = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
        " ",
        name,
    ).replace("_", " ")
    return " ".join(word[:1].upper() + word[1:] for word in words.split())


def _pluralise(value: str) -> str:
    return _INFLECT.plural_noun(value) or value


__all__ = (
    "ContentType",
    "ContentTypesCapability",
    "ContentTypeError",
    "ContentTypeRegistry",
    "DEFAULT_CONTENT_ACTIONS",
    "SiteContentTypesCapability",
    "UnknownContentTypeError",
    "post_setup_site",
    "setup_site",
)
