"""Opaque database route selection and process-local route metrics."""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal, cast

from wybra.core.exceptions import ConfigurationError

type DatabaseRouteRole = Literal["default", "reader", "writer"]
type DatabaseRotationPolicy = Literal[
    "default",
    "queue",
    "random",
    "weighted",
    "load",
    "adaptive",
]

DATABASE_ROUTE_ROLES: frozenset[DatabaseRouteRole] = frozenset(
    {"default", "reader", "writer"}
)
DATABASE_ROTATION_POLICIES: frozenset[DatabaseRotationPolicy] = frozenset(
    {"default", "queue", "random", "weighted", "load", "adaptive"}
)


@dataclass(frozen=True, slots=True)
class DbRoute:
    """An opaque, immutable selection of one configured database route."""

    database_name: str
    role: DatabaseRouteRole
    _alias: str = field(repr=False, compare=True)


@dataclass(frozen=True, slots=True)
class DbConnection:
    """An opaque handle for selecting routes on one logical database."""

    _registry: DatabaseRouteRegistry = field(repr=False, compare=False)
    name: str = "default"

    def default(self) -> DbRoute:
        """Select an ordinary default route."""
        return self._registry.select(self.name, "default")

    def for_read(self) -> DbRoute:
        """Select a route suitable for an explicitly replica-tolerant read."""
        return self._registry.select(self.name, "reader")

    def for_write(self) -> DbRoute:
        """Select a writer-consistent route for a mutation workflow."""
        return self._registry.select(self.name, "writer")


@dataclass(frozen=True, slots=True)
class DatabaseRouteInstance:
    """One configured physical database instance in a logical route pool."""

    name: str
    alias: str
    roles: frozenset[DatabaseRouteRole]
    weight: int = 1

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("Database route instance name is required.")
        if not self.alias:
            raise ConfigurationError("Database route alias is required.")
        if not self.roles:
            raise ConfigurationError("Database route instance role is required.")
        if self.weight <= 0:
            raise ConfigurationError("Database route instance weight must be positive.")


@dataclass(slots=True)
class _RouteMetrics:
    active_statements: int = 0
    active_transactions: int = 0
    latency_seconds: float | None = None

    def record_statement(self, duration_seconds: float) -> None:
        if self.latency_seconds is None:
            self.latency_seconds = duration_seconds
            return
        self.latency_seconds = (self.latency_seconds * 0.8) + (duration_seconds * 0.2)


@dataclass(slots=True)
class DatabaseRouteRegistry:
    """Select configured route instances without exposing driver clients."""

    instances: tuple[DatabaseRouteInstance, ...]
    default_rotation: DatabaseRotationPolicy = "queue"
    reader_rotation: DatabaseRotationPolicy = "queue"
    writer_rotation: DatabaseRotationPolicy = "queue"
    _queue_offsets: dict[tuple[str, DatabaseRouteRole], int] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _weighted_offsets: dict[tuple[str, DatabaseRouteRole], int] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _metrics: dict[str, _RouteMetrics] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        aliases = [instance.alias for instance in self.instances]
        if len(aliases) != len(set(aliases)):
            raise ConfigurationError("Database route aliases must be unique.")
        for policy in self._policies():
            if policy not in DATABASE_ROTATION_POLICIES:
                raise ConfigurationError(f"Unknown database route rotation: {policy}.")
        self._metrics = {instance.alias: _RouteMetrics() for instance in self.instances}

    def connection(self, name: str = "default") -> DbConnection:
        self._require_database(name)
        return DbConnection(self, name)

    def select(self, database_name: str, role: DatabaseRouteRole) -> DbRoute:
        candidates = self._candidates(database_name, role)
        if not candidates and role != "default":
            return self.select(database_name, "default")
        if not candidates:
            raise ConfigurationError(
                f"No eligible {role} database route is configured for {database_name}."
            )
        selected = self._select_candidate(
            database_name,
            role,
            candidates,
            self._policy_for(role),
        )
        return DbRoute(database_name=database_name, role=role, _alias=selected.alias)

    def alias_for(self, route: DbRoute) -> str:
        """Resolve a route alias for internal Tortoise adapter use only."""
        self._metrics_for(route)
        return route._alias

    def static_default_alias(self, database_name: str = "default") -> str:
        """Return the deterministic bootstrap alias for Tortoise app defaults."""
        candidates = self._candidates(database_name, "default")
        if not candidates:
            raise ConfigurationError(
                f"No eligible default database route is configured for {database_name}."
            )
        return candidates[0].alias

    def begin_statement(self, route: DbRoute) -> float:
        metrics = self._metrics_for(route)
        metrics.active_statements += 1
        return time.perf_counter()

    def end_statement(self, route: DbRoute, started: float) -> None:
        metrics = self._metrics_for(route)
        metrics.active_statements = max(0, metrics.active_statements - 1)
        metrics.record_statement(max(0.0, time.perf_counter() - started))

    def begin_transaction(self, route: DbRoute) -> None:
        self._metrics_for(route).active_transactions += 1

    def end_transaction(self, route: DbRoute) -> None:
        metrics = self._metrics_for(route)
        metrics.active_transactions = max(0, metrics.active_transactions - 1)

    def metrics(self, route: DbRoute) -> tuple[int, int, float | None]:
        metrics = self._metrics_for(route)
        return (
            metrics.active_statements,
            metrics.active_transactions,
            metrics.latency_seconds,
        )

    def _candidates(
        self,
        database_name: str,
        role: DatabaseRouteRole,
    ) -> tuple[DatabaseRouteInstance, ...]:
        self._require_database(database_name)
        return tuple(
            instance
            for instance in self.instances
            if instance.name == database_name and role in instance.roles
        )

    def _select_candidate(
        self,
        database_name: str,
        role: DatabaseRouteRole,
        candidates: tuple[DatabaseRouteInstance, ...],
        policy: DatabaseRotationPolicy,
    ) -> DatabaseRouteInstance:
        if policy == "default":
            return candidates[0]
        if policy == "random":
            return random.choice(candidates)
        if policy == "weighted":
            return self._weighted_candidate(database_name, role, candidates)
        if policy == "load":
            return min(candidates, key=self._load_key)
        if policy == "adaptive":
            return min(candidates, key=self._adaptive_key)
        return self._queue_candidate(database_name, role, candidates)

    def _queue_candidate(
        self,
        database_name: str,
        role: DatabaseRouteRole,
        candidates: tuple[DatabaseRouteInstance, ...],
    ) -> DatabaseRouteInstance:
        key = (database_name, role)
        offset = self._queue_offsets.get(key, 0)
        self._queue_offsets[key] = offset + 1
        return candidates[offset % len(candidates)]

    def _weighted_candidate(
        self,
        database_name: str,
        role: DatabaseRouteRole,
        candidates: tuple[DatabaseRouteInstance, ...],
    ) -> DatabaseRouteInstance:
        expanded = tuple(
            candidate for candidate in candidates for _ in range(candidate.weight)
        )
        key = (database_name, role)
        offset = self._weighted_offsets.get(key, 0)
        self._weighted_offsets[key] = offset + 1
        return expanded[offset % len(expanded)]

    def _load_key(self, instance: DatabaseRouteInstance) -> tuple[float, str]:
        metrics = self._metrics[instance.alias]
        return (
            (metrics.active_statements + metrics.active_transactions) / instance.weight,
            instance.alias,
        )

    def _adaptive_key(
        self, instance: DatabaseRouteInstance
    ) -> tuple[float, float, str]:
        load, alias = self._load_key(instance)
        latency = self._metrics[instance.alias].latency_seconds
        return (load, latency if latency is not None else 0.0, alias)

    def _metrics_for(self, route: DbRoute) -> _RouteMetrics:
        self._require_database(route.database_name)
        try:
            return self._metrics[route._alias]
        except KeyError as exc:
            raise ConfigurationError("Unknown database route.") from exc

    def _require_database(self, name: str) -> None:
        if not any(instance.name == name for instance in self.instances):
            raise ConfigurationError(f"Unknown logical database: {name}.")

    def _policy_for(self, role: DatabaseRouteRole) -> DatabaseRotationPolicy:
        if role == "default":
            return self.default_rotation
        if role == "reader":
            return self.reader_rotation
        return self.writer_rotation

    def _policies(self) -> tuple[DatabaseRotationPolicy, ...]:
        return self.default_rotation, self.reader_rotation, self.writer_rotation


def tortoise_route_router(registry: DatabaseRouteRegistry) -> type[object]:
    """Build a Tortoise router type backed by one Wybra route registry."""

    class RouteRouter:
        def db_for_read(self, _model: object) -> str:
            return registry.alias_for(registry.connection().for_read())

        def db_for_write(self, _model: object) -> str:
            return registry.alias_for(registry.connection().for_write())

    return RouteRouter


_ROUTE_INSTRUMENTED_ATTRIBUTE = "_wybra_route_instrumented"
_ROUTE_QUERY_METHODS: tuple[str, ...] = (
    "execute_insert",
    "execute_many",
    "execute_query",
    "execute_query_dict",
    "execute_query_dict_with_affected",
    "execute_script",
)
_TRANSACTION_FACTORY_METHOD = "_in_transaction"
_statement_depth: ContextVar[int] = ContextVar("wybra_route_statement_depth", default=0)

type _AsyncQueryMethod = Callable[..., Awaitable[Any]]
type _TransactionFactory = Callable[..., AbstractAsyncContextManager[Any]]


def instrument_tortoise_route_connection(
    connection: object,
    *,
    registry: DatabaseRouteRegistry,
    alias: str,
) -> None:
    """Record route metrics for a Tortoise client and its transaction wrappers."""
    if getattr(connection, _ROUTE_INSTRUMENTED_ATTRIBUTE, False):
        return
    route = DbRoute(database_name="default", role="default", _alias=alias)
    registry.alias_for(route)
    for method_name in _ROUTE_QUERY_METHODS:
        method = getattr(connection, method_name, None)
        if method is None:
            continue
        setattr(
            connection,
            method_name,
            _instrument_route_query_method(
                cast(_AsyncQueryMethod, method),
                registry,
                route,
            ),
        )
    transaction_factory = getattr(connection, _TRANSACTION_FACTORY_METHOD, None)
    if transaction_factory is not None:
        setattr(
            connection,
            _TRANSACTION_FACTORY_METHOD,
            _instrument_route_transaction_factory(
                cast(_TransactionFactory, transaction_factory),
                registry,
                route,
            ),
        )
    setattr(connection, _ROUTE_INSTRUMENTED_ATTRIBUTE, True)


def _instrument_route_query_method(
    method: _AsyncQueryMethod,
    registry: DatabaseRouteRegistry,
    route: DbRoute,
) -> _AsyncQueryMethod:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        depth = _statement_depth.get()
        token = _statement_depth.set(depth + 1)
        started = registry.begin_statement(route) if depth == 0 else None
        try:
            return await method(*args, **kwargs)
        finally:
            _statement_depth.reset(token)
            if started is not None:
                registry.end_statement(route, started)

    return wrapped


def _instrument_route_transaction_factory(
    method: _TransactionFactory,
    registry: DatabaseRouteRegistry,
    route: DbRoute,
) -> _TransactionFactory:
    def wrapped(*args: Any, **kwargs: Any) -> AbstractAsyncContextManager[Any]:
        return _RouteInstrumentedTransactionContext(
            method(*args, **kwargs),
            registry,
            route,
        )

    return wrapped


class _RouteInstrumentedTransactionContext(AbstractAsyncContextManager[Any]):
    def __init__(
        self,
        context: AbstractAsyncContextManager[Any],
        registry: DatabaseRouteRegistry,
        route: DbRoute,
    ) -> None:
        self._context = context
        self._registry = registry
        self._route = route
        self._entered = False

    async def __aenter__(self) -> Any:
        self._registry.begin_transaction(self._route)
        try:
            connection = await self._context.__aenter__()
            instrument_tortoise_route_connection(
                connection,
                registry=self._registry,
                alias=self._registry.alias_for(self._route),
            )
        except Exception:
            self._registry.end_transaction(self._route)
            raise
        self._entered = True
        return connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return await self._context.__aexit__(exc_type, exc_value, traceback)
        finally:
            if self._entered:
                self._registry.end_transaction(self._route)
                self._entered = False
