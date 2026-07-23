"""Declarative scope requirements for models, views, and endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from inspect import isawaitable
from typing import Protocol, TypeIs, TypeVar, cast, runtime_checkable

from fastapi import Request
from tortoise.models import Model

from wybra.content_types import ContentTypesCapability
from wybra.core.exceptions import Http403
from wybra.site import Site, get_site

_SCOPE_DECLARATION_ATTRIBUTE = "__wybra_scope_declaration__"
_SCOPE_TARGET_ATTRIBUTE = "__wybra_scope_target__"
_SCOPE_SUBJECT_STATE_ATTRIBUTE = "_wybra_scope_subject"
_NO_RECORD = object()
_NO_METADATA_SCOPES = object()

_Target = TypeVar("_Target")


class ScopeDeclarationError(ValueError):
    """Raised when scope metadata is invalid or ambiguous."""


class ScopeAction(StrEnum):
    """Canonical actions understood by model scope declarations."""

    LIST = "list"
    VIEW = "view"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MANAGE = "manage"


STANDARD_SCOPE_ACTIONS = tuple(ScopeAction)
_STANDARD_SCOPE_ACTION_NAMES = frozenset(action.value for action in ScopeAction)


class ScopeDeclarationOrigin(StrEnum):
    """Origin of one discoverable scope identifier."""

    LITERAL = "literal"
    DERIVED = "derived"
    AGGREGATE = "aggregate"


@dataclass(frozen=True, slots=True)
class ScopeDeclaration:
    """Immutable scope metadata attached to a protected target."""

    requires: tuple[str, ...] = ()
    actions: tuple[ScopeAction, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveredScope:
    """One declared or optional aggregate scope with audit metadata."""

    identifier: str
    origin: ScopeDeclarationOrigin
    target: str
    content_type: str | None = None
    action: ScopeAction | None = None
    aggregate: bool = False


@dataclass(frozen=True, slots=True)
class ScopeSubject:
    """Provider-neutral actor and its effective scope grants."""

    actor: object | None = None
    granted_scopes: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()


@runtime_checkable
class ScopeGrantsCapability(Protocol):
    """Optional provider of request-specific effective scope grants."""

    async def resolve_scope_subject(self, request: Request) -> ScopeSubject: ...


@runtime_checkable
class ScopeCatalogueCapability(Protocol):
    """Optional provider of persisted scope catalogue identifiers."""

    async def list_scope_identifiers(self) -> tuple[str, ...]: ...


type ObjectScopeCheck = Callable[
    [object | None, ScopeAction | None, object],
    bool | Awaitable[bool],
]


@dataclass(frozen=True, slots=True)
class ScopeAccessDecision:
    """Inspectable result of one declarative scope policy evaluation."""

    target: object
    action: ScopeAction | None
    model_requirements: tuple[str, ...]
    view_requirements: tuple[str, ...]
    required_scopes: tuple[str, ...]
    granted_scopes: tuple[str, ...]
    missing_scopes: tuple[str, ...]
    object_allowed: bool | None = None
    allowed: bool = False


def scopes(
    *values: str,
    requires: Iterable[str] | None = None,
    actions: Iterable[str] | None = None,
) -> Callable[[_Target], _Target]:
    """Attach target-sensitive scope requirements without wrapping a target."""

    def decorator(target: _Target) -> _Target:
        declaration = _normalise_declaration(
            target,
            values=values,
            requires=requires,
            actions=actions,
        )
        if not declaration.requires and not declaration.actions:
            return target
        setattr(target, _SCOPE_DECLARATION_ATTRIBUTE, declaration)
        return target

    return decorator


def get_scope_declaration(target: object) -> ScopeDeclaration:
    """Return the immutable declaration attached to a target, if any."""

    target = scope_target(target)
    attached = getattr(target, _SCOPE_DECLARATION_ATTRIBUTE, None)
    declaration = (
        attached if isinstance(attached, ScopeDeclaration) else ScopeDeclaration()
    )
    if not _is_model(target):
        return declaration

    metadata_declaration = _model_metadata_declaration(target)
    if (
        declaration.requires or declaration.actions
    ) and _model_declares_metadata_scopes(target):
        raise ScopeDeclarationError(
            f"Model {target.__name__} declares scopes through both the decorator "
            "and Meta.scopes."
        )
    if declaration.requires or declaration.actions:
        return declaration
    return metadata_declaration


def bind_scope_target[Target](endpoint: Target, target: object) -> Target:
    """Bind a generated endpoint to the declaration target it represents."""

    setattr(endpoint, _SCOPE_TARGET_ATTRIBUTE, target)
    return endpoint


def scope_target(target: object) -> object:
    """Return the declaration target represented by a generated endpoint."""

    return getattr(target, _SCOPE_TARGET_ATTRIBUTE, target)


def discover_declared_scopes(
    app: object,
    content_types: ContentTypesCapability,
) -> tuple[DiscoveredScope, ...]:
    """Discover model, view, and endpoint scope identifiers after finalisation."""

    discovered: set[DiscoveredScope] = set()
    inspected_targets: set[int] = set()
    for content_type in content_types.all():
        discovered.add(
            DiscoveredScope(
                identifier=content_type.identifier,
                origin=ScopeDeclarationOrigin.AGGREGATE,
                target=_target_identifier(content_type.model),
                content_type=content_type.identifier,
                aggregate=True,
            )
        )
        discovered.update(_discover_target_scopes(content_type.model, content_types))
        inspected_targets.add(id(content_type.model))

    for endpoint in _route_endpoints(getattr(app, "routes", ())):
        target = scope_target(endpoint)
        if id(target) in inspected_targets:
            continue
        inspected_targets.add(id(target))
        discovered.update(_discover_target_scopes(target, content_types))

    return tuple(
        sorted(
            discovered,
            key=lambda item: (
                item.identifier,
                item.origin.value,
                item.target,
                item.content_type or "",
                item.action.value if item.action is not None else "",
            ),
        )
    )


def declared_scope_identifiers(
    discovered: Iterable[DiscoveredScope],
) -> tuple[str, ...]:
    """Return distinct required identifiers, excluding optional aggregates."""

    return tuple(sorted({item.identifier for item in discovered if not item.aggregate}))


def missing_scope_catalogue_entries(
    discovered: Iterable[DiscoveredScope],
    persisted_identifiers: Iterable[str],
) -> tuple[str, ...]:
    """Return declared identifiers absent from a persisted scope catalogue."""

    persisted = frozenset(persisted_identifiers)
    return tuple(
        identifier
        for identifier in declared_scope_identifiers(discovered)
        if identifier not in persisted
    )


async def validate_site_scope_catalogue(site: Site) -> tuple[str, ...]:
    """Return declared identifiers absent from the site's persisted catalogue."""

    content_types = site.require_capability(ContentTypesCapability)
    catalogue = site.require_capability(ScopeCatalogueCapability)
    discovered = discover_declared_scopes(site.app, content_types)
    persisted = await catalogue.list_scope_identifiers()
    return missing_scope_catalogue_entries(discovered, persisted)


def resolve_scope_requirements(
    target: object,
    action: ScopeAction | str | None,
    content_types: ContentTypesCapability | None,
) -> tuple[str, ...]:
    """Resolve literal and content-type-derived requirements for one target."""

    declaration = get_scope_declaration(target)
    resolved_action = _normalise_operation_action(action)
    if resolved_action is None or resolved_action not in declaration.actions:
        return declaration.requires

    if content_types is None:
        raise ScopeDeclarationError(
            "ContentTypesCapability is required to resolve model scope actions."
        )
    model = target if _is_model(target) else _require_model_backed_view(target)
    content_type = content_types.for_model(model)
    return (*declaration.requires, f"{content_type.identifier}.{resolved_action}")


def resolve_operation_requirements(
    model: type[Model] | None,
    view: object | None,
    action: ScopeAction | str | None,
    content_types: ContentTypesCapability | None,
) -> tuple[str, ...]:
    """Compose model and view requirements as one conjunctive set."""

    requirements: list[str] = []
    if model is not None:
        requirements.extend(resolve_scope_requirements(model, action, content_types))
    if view is not None:
        requirements.extend(resolve_scope_requirements(view, action, content_types))
    return tuple(dict.fromkeys(requirements))


def missing_scope_requirements(
    required_scopes: Iterable[str],
    granted_scopes: Iterable[str],
    content_types: ContentTypesCapability | None,
) -> tuple[str, ...]:
    """Return requirements not met by exact or aggregate content-type grants."""

    requirements = tuple(dict.fromkeys(required_scopes))
    grants = frozenset(granted_scopes)
    aggregate_grants = (
        grants & {content_type.identifier for content_type in content_types.all()}
        if content_types is not None
        else frozenset()
    )
    return tuple(
        requirement
        for requirement in requirements
        if requirement not in grants
        and not any(
            requirement.startswith(f"{aggregate}.") for aggregate in aggregate_grants
        )
    )


async def access_decision(
    request: Request,
    *,
    target: object,
    action: ScopeAction | str | None = None,
    model: type[Model] | None = None,
    record: object = _NO_RECORD,
    object_check: ObjectScopeCheck | None = None,
) -> ScopeAccessDecision:
    """Resolve additive requirements against request-local effective grants."""

    site = get_site(request.app)
    content_types = site.optional_capability(ContentTypesCapability)
    model_requirements = (
        resolve_scope_requirements(model, action, content_types)
        if model is not None
        else ()
    )
    view_requirements = resolve_scope_requirements(target, action, content_types)
    required_scopes = tuple(dict.fromkeys((*model_requirements, *view_requirements)))
    resolved_action = _normalise_operation_action(action)
    subject: ScopeSubject | None = None
    missing_scopes: tuple[str, ...] = ()
    if required_scopes:
        subject = await _scope_subject(request)
        missing_scopes = missing_scope_requirements(
            required_scopes,
            subject.granted_scopes,
            content_types,
        )

    object_allowed: bool | None = None
    if object_check is not None and not missing_scopes:
        if record is _NO_RECORD:
            raise ValueError("Object scope checks require a resolved record.")
        subject = subject or await _scope_subject(request)
        result = object_check(subject.actor, resolved_action, record)
        object_allowed = bool(await result) if isawaitable(result) else bool(result)

    granted_scopes = subject.granted_scopes if subject is not None else ()
    return ScopeAccessDecision(
        target=target,
        action=resolved_action,
        model_requirements=model_requirements,
        view_requirements=view_requirements,
        required_scopes=required_scopes,
        granted_scopes=granted_scopes,
        missing_scopes=missing_scopes,
        object_allowed=object_allowed,
        allowed=not missing_scopes and object_allowed is not False,
    )


async def allows_scope_access(
    request: Request,
    *,
    target: object,
    action: ScopeAction | str | None = None,
    model: type[Model] | None = None,
) -> bool:
    """Return whether a request satisfies a target's scope requirements."""

    return (
        await access_decision(
            request,
            target=target,
            action=action,
            model=model,
        )
    ).allowed


async def enforce_scope_access(
    request: Request,
    *,
    target: object,
    action: ScopeAction | str | None = None,
    model: type[Model] | None = None,
    record: object = _NO_RECORD,
    object_check: ObjectScopeCheck | None = None,
) -> ScopeAccessDecision:
    """Return an allowed decision or raise the standard forbidden exception."""

    decision = await access_decision(
        request,
        target=target,
        action=action,
        model=model,
        record=record,
        object_check=object_check,
    )
    if not decision.allowed:
        raise Http403()
    return decision


async def scope_visibility(
    request: Request,
    *,
    target: object,
    model: type[Model] | None = None,
    actions: Iterable[ScopeAction] = STANDARD_SCOPE_ACTIONS,
) -> Mapping[ScopeAction, bool]:
    """Resolve action visibility using one request-local subject lookup."""

    return {
        action: await allows_scope_access(
            request,
            target=target,
            action=action,
            model=model,
        )
        for action in actions
    }


def scope_dependency(
    target: object,
    *,
    action: ScopeAction | str | None = None,
    model: type[Model] | None = None,
) -> Callable[[Request], Awaitable[ScopeAccessDecision]]:
    """Build a FastAPI dependency that enforces a decorated target."""

    async def dependency(request: Request) -> ScopeAccessDecision:
        return await enforce_scope_access(
            request,
            target=target,
            action=action,
            model=model,
        )

    return dependency


async def _scope_subject(request: Request) -> ScopeSubject:
    cached = getattr(request.state, _SCOPE_SUBJECT_STATE_ATTRIBUTE, None)
    if isinstance(cached, ScopeSubject):
        return cached

    capability = get_site(request.app).optional_capability(ScopeGrantsCapability)
    subject = (
        await capability.resolve_scope_subject(request)
        if capability is not None
        else ScopeSubject()
    )
    setattr(request.state, _SCOPE_SUBJECT_STATE_ATTRIBUTE, subject)
    return subject


def _normalise_declaration(
    target: object,
    *,
    values: tuple[str, ...],
    requires: Iterable[str] | None,
    actions: Iterable[str] | None,
) -> ScopeDeclaration:
    if _is_model(target):
        if values and actions is not None:
            raise ScopeDeclarationError(
                "Model positional scope actions cannot be combined with actions=."
            )
        return ScopeDeclaration(
            requires=_normalise_requirements(requires),
            actions=_normalise_actions(values if values else actions),
        )

    if values and requires is not None:
        raise ScopeDeclarationError(
            "View or endpoint positional requirements cannot be combined with "
            "requires=."
        )
    if actions is not None:
        _require_model_backed_view(target)
    return ScopeDeclaration(
        requires=_normalise_requirements(values if values else requires),
        actions=_normalise_actions(actions),
    )


def _normalise_operation_action(
    action: ScopeAction | str | None,
) -> ScopeAction | None:
    if action is None or isinstance(action, ScopeAction):
        return action
    try:
        return ScopeAction(action)
    except ValueError as exc:
        raise ScopeDeclarationError(f"Unknown scope action: {action}.") from exc


def _normalise_requirements(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raise ScopeDeclarationError("requires= must be an iterable of scope names.")
    try:
        requirements = tuple(values)
    except TypeError as exc:
        raise ScopeDeclarationError(
            "requires= must be an iterable of scope names."
        ) from exc
    if not all(
        isinstance(requirement, str) and requirement.strip()
        for requirement in requirements
    ):
        raise ScopeDeclarationError("Scope requirements must be non-blank strings.")
    if len(set(requirements)) != len(requirements):
        raise ScopeDeclarationError("Scope requirements must not contain duplicates.")
    return requirements


def _normalise_actions(values: Iterable[str] | None) -> tuple[ScopeAction, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raise ScopeDeclarationError("actions= must be an iterable of action names.")
    try:
        names = tuple(values)
    except TypeError as exc:
        raise ScopeDeclarationError(
            "actions= must be an iterable of action names."
        ) from exc
    if not all(isinstance(name, str) and name for name in names):
        raise ScopeDeclarationError("Scope actions must be non-blank strings.")
    if len(set(names)) != len(names):
        raise ScopeDeclarationError("Scope actions must not contain duplicates.")
    excluded = tuple(name.removeprefix("-") for name in names if name.startswith("-"))
    included = tuple(name for name in names if not name.startswith("-"))
    if excluded and included:
        raise ScopeDeclarationError(
            "Inclusive and exclusion-prefixed scope actions cannot be combined."
        )
    invalid = next(
        (
            name
            for name in (*included, *excluded)
            if name not in _STANDARD_SCOPE_ACTION_NAMES
        ),
        None,
    )
    if invalid is not None:
        raise ScopeDeclarationError(f"Unknown scope action: {invalid}.")
    selected_names = (
        tuple(
            action.value
            for action in STANDARD_SCOPE_ACTIONS
            if action.value not in excluded
        )
        if excluded
        else included
    )
    return tuple(ScopeAction(name) for name in selected_names)


def _model_metadata_declaration(model: type[Model]) -> ScopeDeclaration:
    value = getattr(model.Meta, "scopes", _NO_METADATA_SCOPES)
    if value is _NO_METADATA_SCOPES or value is None:
        return ScopeDeclaration()
    if value is True:
        return ScopeDeclaration(actions=STANDARD_SCOPE_ACTIONS)
    if value is False:
        raise ScopeDeclarationError(
            f"Model Meta.scopes must be None, True, or an iterable of action "
            f"names: {model.__name__}."
        )
    try:
        actions = _normalise_actions(cast(Iterable[str], value))
    except TypeError as exc:
        raise ScopeDeclarationError(
            f"Model Meta.scopes must be None, True, or an iterable of action "
            f"names: {model.__name__}."
        ) from exc
    return ScopeDeclaration(actions=actions)


def _model_declares_metadata_scopes(model: type[Model]) -> bool:
    return getattr(model.Meta, "scopes", _NO_METADATA_SCOPES) is not _NO_METADATA_SCOPES


def _is_model(target: object) -> TypeIs[type[Model]]:
    return isinstance(target, type) and issubclass(target, Model)


def _require_model_backed_view(target: object) -> type[Model]:
    from wybra.views.generic import ModelGenericView

    if not isinstance(target, type) or not issubclass(target, ModelGenericView):
        raise ScopeDeclarationError(
            "actions= requires a model or model-backed ModelGenericView."
        )
    model = target.model
    if model is None or not _is_model(model):
        raise ScopeDeclarationError(
            "actions= requires ModelGenericView.model to be a Tortoise model."
        )
    return model


def _discover_target_scopes(
    target: object,
    content_types: ContentTypesCapability,
) -> tuple[DiscoveredScope, ...]:
    declaration = get_scope_declaration(target)
    if not declaration.requires and not declaration.actions:
        return ()

    model: type[Model] | None = None
    if _is_model(target):
        model = target
    elif declaration.actions:
        model = _require_model_backed_view(target)
    else:
        candidate = getattr(target, "model", None)
        if _is_model(candidate):
            model = candidate

    content_type = content_types.for_model(model) if model is not None else None
    target_name = _target_identifier(target)
    discovered = [
        DiscoveredScope(
            identifier=identifier,
            origin=ScopeDeclarationOrigin.LITERAL,
            target=target_name,
            content_type=(
                content_type.identifier if content_type is not None else None
            ),
        )
        for identifier in declaration.requires
    ]
    if content_type is not None:
        discovered.extend(
            DiscoveredScope(
                identifier=f"{content_type.identifier}.{action}",
                origin=ScopeDeclarationOrigin.DERIVED,
                target=target_name,
                content_type=content_type.identifier,
                action=action,
            )
            for action in declaration.actions
        )
    return tuple(discovered)


def _route_endpoints(routes: Sequence[object]) -> tuple[object, ...]:
    endpoints: list[object] = []
    for route in routes:
        route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(route_contexts):
            endpoints.extend(
                endpoint
                for context in route_contexts()
                if (
                    endpoint := getattr(
                        getattr(context, "original_route", None),
                        "endpoint",
                        None,
                    )
                )
                is not None
            )
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            endpoints.append(endpoint)
        children = getattr(route, "routes", None)
        if isinstance(children, Sequence):
            endpoints.extend(_route_endpoints(children))
    return tuple(endpoints)


def _target_identifier(target: object) -> str:
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return f"{module}.{qualname}"
    return type(target).__name__


__all__ = (
    "DiscoveredScope",
    "ObjectScopeCheck",
    "STANDARD_SCOPE_ACTIONS",
    "ScopeAction",
    "ScopeAccessDecision",
    "ScopeCatalogueCapability",
    "ScopeDeclaration",
    "ScopeDeclarationError",
    "ScopeDeclarationOrigin",
    "ScopeGrantsCapability",
    "ScopeSubject",
    "access_decision",
    "allows_scope_access",
    "bind_scope_target",
    "declared_scope_identifiers",
    "discover_declared_scopes",
    "enforce_scope_access",
    "get_scope_declaration",
    "missing_scope_requirements",
    "missing_scope_catalogue_entries",
    "resolve_operation_requirements",
    "resolve_scope_requirements",
    "scope_dependency",
    "scope_target",
    "scope_visibility",
    "scopes",
    "validate_site_scope_catalogue",
)
