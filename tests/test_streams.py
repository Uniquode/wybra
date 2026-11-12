from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import wybra.streams.setup as streams_setup
from wybra.config import MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.secrets.keys import configured_credential_references
from wybra.services.secrets import SecretValue
from wybra.site import start
from wybra.streams import MAX_STREAM_PAYLOAD_BYTES, StreamsCapability, StreamsSettings
from wybra.streams.connection import resolve_nats_connection_options
from wybra.streams.contracts import stream_resource_digest
from wybra.streams.exceptions import StreamSubscriptionClosedError
from wybra.streams.memory import InMemoryStreamsProvider
from wybra.streams.nats import NatsStreamsProvider
from wybra.streams.redis import RedisStreamsProvider


async def _memory_streams_site():
    return await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {"modules": ("wybra.streams",)},
                "streams": {"backend": "memory"},
            }
        ),
        environ={},
    )


@pytest.mark.anyio
async def test_streams_capability_is_unavailable_without_the_module() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
        environ={},
    )
    try:
        assert await site.capability_proxy(StreamsCapability).optional() is None
    finally:
        await site.close()


def test_stream_resource_digest_isolates_delimiter_bearing_pairs() -> None:
    assert stream_resource_digest("a:b", "c") != stream_resource_digest("a", "b:c")


@pytest.mark.parametrize("value", (" topic", "topic ", "\ttopic"))
def test_stream_resources_reject_surrounding_whitespace(value: str) -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        stream_resource_digest(value, "updates")


def test_streams_settings_rejects_ambiguous_environment_url_source() -> None:
    with pytest.raises(ConfigurationError, match="requires streams.url_key"):
        StreamsSettings(backend="redis", url_source="environment")


@pytest.mark.parametrize(
    "servers",
    (
        ("redis://cache:6379",),
        ("nats://:4222",),
        ("nats://cache:65536",),
        ("nats://token@cache:4222",),
    ),
)
def test_streams_settings_reject_invalid_nats_servers(
    servers: tuple[str, ...],
) -> None:
    with pytest.raises(ConfigurationError, match="valid NATS server URLs"):
        StreamsSettings(backend="nats", servers=servers)


def test_streams_settings_reject_mixed_nats_transports() -> None:
    with pytest.raises(ConfigurationError, match="cannot mix WebSocket and TCP"):
        StreamsSettings(
            backend="nats",
            servers=("nats://nats.internal:4222", "wss://nats.internal:443"),
        )


@pytest.mark.parametrize(
    ("authentication", "authentication_source", "message"),
    (
        ("token", None, "requires streams.authentication_source"),
        (None, "keychain", "requires streams.authentication"),
        ("certificate", "keychain", "must be 'token' or 'user-password'"),
    ),
)
def test_streams_settings_validate_nats_authentication(
    authentication: str | None,
    authentication_source: str | None,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        StreamsSettings(
            backend="nats",
            servers=("nats://nats.internal:4222",),
            authentication=authentication,
            authentication_source=authentication_source,
        )


def test_streams_settings_expose_safe_secret_reference_diagnostics() -> None:
    settings = StreamsSettings(
        backend="redis",
        url="rediss://cache.internal:6380/0",
        credentials_source="keychain",
        credentials_key="streams/production/credentials",
    )

    diagnostic = settings.diagnostics()

    assert diagnostic.url_source is None
    assert diagnostic.url_key is None
    assert diagnostic.credentials_source == "keychain"
    assert diagnostic.credentials_key == "streams/production/credentials"
    assert "cache.internal" not in repr(diagnostic)


def test_nats_settings_expose_safe_authentication_reference_diagnostics() -> None:
    settings = StreamsSettings(
        backend="nats",
        servers=("nats://nats.internal:4222",),
        authentication="user-password",
        authentication_source="keychain",
    )

    diagnostic = settings.diagnostics()

    assert settings.authentication_reference == (
        "keychain",
        "streams/nats/credentials",
    )
    assert diagnostic.authentication == "user-password"
    assert diagnostic.authentication_source == "keychain"
    assert diagnostic.authentication_key == "streams/nats/credentials"
    assert "nats.internal" not in repr(diagnostic)


@pytest.mark.parametrize(
    ("authentication", "expected_key"),
    (
        ("token", "WYBRA_STREAMS_NATS_TOKEN"),
        ("user-password", "WYBRA_STREAMS_NATS_CREDENTIALS"),
    ),
)
def test_nats_environment_authentication_uses_mode_specific_default_key(
    authentication: str,
    expected_key: str,
) -> None:
    settings = StreamsSettings(
        backend="nats",
        servers=("nats://nats.internal:4222",),
        authentication=authentication,
        authentication_source="environment",
    )

    assert settings.authentication_reference == ("environment", expected_key)


def test_streams_keychain_references_appear_in_secret_catalogue() -> None:
    references = configured_credential_references(
        raw_config={
            "streams": {
                "backend": "redis",
                "url": "rediss://cache.internal:6380/0",
                "credentials_source": "keychain",
                "credentials_key": "streams/production/credentials",
            }
        }
    )

    assert {(reference.name, reference.key) for reference in references} >= {
        ("streams-credentials", "streams/production/credentials"),
    }


def test_nats_keychain_reference_appears_in_secret_catalogue() -> None:
    references = configured_credential_references(
        raw_config={
            "streams": {
                "backend": "nats",
                "servers": ("nats://nats.internal:4222",),
                "authentication": "token",
                "authentication_source": "keychain",
            }
        }
    )

    assert {(reference.name, reference.key) for reference in references} >= {
        ("streams-nats-token", "streams/nats/token"),
    }


@pytest.mark.parametrize(
    ("authentication", "secret", "expected"),
    (
        ("token", "nats-token", {"token": "nats-token"}),
        (
            "user-password",
            "worker:correct-horse-battery-staple",
            {"user": "worker", "password": "correct-horse-battery-staple"},
        ),
    ),
)
def test_nats_authentication_resolves_to_client_options(
    authentication: str,
    secret: str,
    expected: dict[str, str],
) -> None:
    class Secrets:
        def resolve(self, source: str, key: str) -> SecretValue:
            assert source == "keychain"
            assert key.startswith("streams/nats/")
            return SecretValue(secret)

        def exists(self, _source: str, _key: str) -> bool:
            return True

    settings = StreamsSettings(
        backend="nats",
        servers=("nats://nats.internal:4222",),
        authentication=authentication,
        authentication_source="keychain",
    )

    assert resolve_nats_connection_options(settings, Secrets()) == expected


@pytest.mark.anyio
async def test_configured_memory_streams_deliver_to_active_subscribers() -> None:
    site = await _memory_streams_site()
    try:
        streams = site.require_capability(StreamsCapability)
        subscription = await streams.subscribe("notifications", "published")

        await streams.publish("notifications", "published", b"hello")

        assert await subscription.receive(timeout=0.1) == b"hello"
        await subscription.close()
    finally:
        await site.close()


@pytest.mark.anyio
async def test_streams_isolate_owners_with_the_same_topic() -> None:
    site = await _memory_streams_site()
    try:
        streams = site.require_capability(StreamsCapability)
        first = await streams.subscribe("first", "updates")
        second = await streams.subscribe("second", "updates")

        await streams.publish("first", "updates", b"first-only")

        assert await first.receive(timeout=0.1) == b"first-only"
        with pytest.raises(TimeoutError):
            await second.receive(timeout=0.01)
        await first.close()
        await second.close()
    finally:
        await site.close()


@pytest.mark.anyio
async def test_streams_reject_oversized_payload_before_delivery() -> None:
    site = await _memory_streams_site()
    try:
        streams = site.require_capability(StreamsCapability)
        subscription = await streams.subscribe("notifications", "published")

        with pytest.raises(ValueError, match="65536"):
            await streams.publish(
                "notifications",
                "published",
                b"x" * (MAX_STREAM_PAYLOAD_BYTES + 1),
            )

        with pytest.raises(TimeoutError):
            await subscription.receive(timeout=0.01)
        await subscription.close()
    finally:
        await site.close()


@pytest.mark.anyio
async def test_memory_streams_closes_an_overflowed_subscription() -> None:
    provider = InMemoryStreamsProvider(subscriber_buffer=1)
    subscription = await provider.subscribe("notifications", "published")

    await provider.publish("notifications", "published", b"first")
    await provider.publish("notifications", "published", b"overflow")

    with pytest.raises(StreamSubscriptionClosedError):
        await subscription.receive(timeout=0.1)


@pytest.mark.anyio
async def test_redis_streams_publish_receive_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PubSub:
        def __init__(self) -> None:
            self.closed = False
            self.channel: str | None = None
            self.messages: list[dict[str, object]] = []

        async def subscribe(self, channel: str) -> None:
            self.channel = channel
            self.messages.append({"type": "subscribe", "channel": channel, "data": 1})

        async def get_message(self, **_kwargs: object) -> object:
            return self.messages.pop(0) if self.messages else None

        async def aclose(self) -> None:
            self.closed = True

    class Client:
        def __init__(self) -> None:
            self.subscription = PubSub()
            self.published: list[tuple[str, bytes]] = []
            self.closed = False

        async def ping(self) -> bool:
            return True

        def pubsub(self) -> PubSub:
            return self.subscription

        async def publish(self, channel: str, payload: bytes) -> None:
            self.published.append((channel, payload))

        async def aclose(self) -> None:
            self.closed = True

    client = Client()

    class Redis:
        @staticmethod
        def from_url(*_args: object, **_kwargs: object) -> Client:
            return client

    monkeypatch.setattr(
        "wybra.streams.redis.importlib.import_module",
        lambda _: SimpleNamespace(Redis=Redis),
    )
    provider = RedisStreamsProvider("redis://cache/0", "site")

    await provider.start()
    subscription = await provider.subscribe("notifications", "published")
    await provider.publish("notifications", "published", b"hello")
    channel, payload = client.published[-1]
    client.subscription.messages.append({"type": "message", "channel": channel, "data": payload})

    assert await subscription.receive(timeout=0.1) == b"hello"
    await provider.close()
    assert client.subscription.closed
    assert client.closed


@pytest.mark.anyio
async def test_nats_streams_publish_receive_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Subscription:
        def __init__(self) -> None:
            self.closed = False
            self.messages: list[SimpleNamespace] = []

        async def next_msg(self, **_kwargs: object) -> SimpleNamespace:
            return self.messages.pop(0)

        async def unsubscribe(self) -> None:
            self.closed = True

    class Client:
        def __init__(self) -> None:
            self.subscription = Subscription()
            self.published: list[tuple[str, bytes]] = []
            self.closed = False
            self.flush_count = 0

        async def flush(self) -> None:
            self.flush_count += 1

        async def publish(self, subject: str, payload: bytes) -> None:
            self.published.append((subject, payload))

        async def subscribe(self, _subject: str) -> Subscription:
            return self.subscription

        async def close(self) -> None:
            self.closed = True

    client = Client()
    connect_calls: list[dict[str, object]] = []

    async def connect(**kwargs: object) -> Client:
        connect_calls.append(kwargs)
        return client

    monkeypatch.setattr(
        "wybra.streams.nats.importlib.import_module",
        lambda _: SimpleNamespace(connect=connect),
    )
    provider = NatsStreamsProvider(
        ("nats://nats-1:4222", "nats://nats-2:4222"),
        "site",
        {"token": "nats-token"},
    )

    await provider.start()
    subscription = await provider.subscribe("notifications", "published")
    await provider.publish("notifications", "published", b"hello")
    subject, payload = client.published[-1]
    client.subscription.messages.append(SimpleNamespace(subject=subject, data=payload))

    assert await subscription.receive(timeout=0.1) == b"hello"
    await provider.close()
    assert client.subscription.closed
    assert client.closed
    assert client.flush_count == 3
    assert connect_calls == [
        {
            "servers": ["nats://nats-1:4222", "nats://nats-2:4222"],
            "token": "nats-token",
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("settings", "provider_name"),
    (
        (
            StreamsSettings(backend="redis", url="redis://cache/0"),
            "RedisStreamsProvider",
        ),
        (
            StreamsSettings(backend="nats", servers=("nats://cache:4222",)),
            "NatsStreamsProvider",
        ),
    ),
)
async def test_provider_start_failure_closes_the_unregistered_provider(
    monkeypatch: pytest.MonkeyPatch,
    settings: StreamsSettings,
    provider_name: str,
) -> None:
    class FailingProvider:
        close_calls = 0

        def __init__(self, *_args: object) -> None:
            providers.append(self)

        async def start(self) -> None:
            raise RuntimeError("unavailable")

        async def close(self) -> None:
            self.close_calls += 1

    providers: list[FailingProvider] = []

    monkeypatch.setattr(streams_setup, provider_name, FailingProvider)

    with pytest.raises(RuntimeError, match="unavailable"):
        await streams_setup._provider_from_settings(settings, None)

    assert providers[0].close_calls == 1


@pytest.mark.anyio
async def test_redis_provider_retries_incomplete_cleanup() -> None:
    class FailsOnceSubscription:
        def __init__(self) -> None:
            self.calls = 0

        async def close(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary subscription failure")

    class FailsOnceClient:
        def __init__(self) -> None:
            self.calls = 0

        async def aclose(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary client failure")

    subscription = FailsOnceSubscription()
    client = FailsOnceClient()
    provider = RedisStreamsProvider("redis://cache/0", "site")
    provider._subscriptions.add(subscription)
    provider._client = client

    with pytest.raises(ExceptionGroup, match="cleanup failed"):
        await provider.close()

    await provider.close()

    assert subscription.calls == 2
    assert client.calls == 2
