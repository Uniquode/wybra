from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from tests_support.database_containers import skip_if_docker_unavailable

from wybra.streams.exceptions import StreamSubscriptionClosedError
from wybra.streams.nats import NatsStreamsProvider
from wybra.streams.redis import RedisStreamsProvider

DEFAULT_NATS_IMAGE = "nats:2.11.12-alpine"
NATS_IMAGE_ENV = "WYBRA_TESTCONTAINERS_NATS_IMAGE"
DEFAULT_REDIS_IMAGE = "redis:7-alpine"
REDIS_IMAGE_ENV = "WYBRA_TESTCONTAINERS_REDIS_IMAGE"


@pytest.fixture
def redis_url() -> Iterator[str]:
    skip_if_docker_unavailable()
    with RedisContainer(os.environ.get(REDIS_IMAGE_ENV, DEFAULT_REDIS_IMAGE)) as container:
        yield (f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0")


@pytest.fixture
def nats_url() -> Iterator[str]:
    skip_if_docker_unavailable()
    container = (
        DockerContainer(os.environ.get(NATS_IMAGE_ENV, DEFAULT_NATS_IMAGE))
        .with_exposed_ports(4222)
        .waiting_for(LogMessageWaitStrategy("Server is ready"))
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"NATS streams testcontainer could not start: {exc}")
    try:
        yield (f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}")
    finally:
        container.stop()


async def _assert_live_delivery_and_isolation(
    publisher: RedisStreamsProvider | NatsStreamsProvider,
    subscriber: RedisStreamsProvider | NatsStreamsProvider,
) -> None:
    subscription = await subscriber.subscribe("events", "updates")
    collision = await subscriber.subscribe("a:b", "c")
    try:
        await publisher.publish("events", "updates", b"delivered")
        assert await subscription.receive(timeout=3) == b"delivered"

        await publisher.publish("a", "b:c", b"isolated")
        with pytest.raises(TimeoutError):
            await collision.receive(timeout=0.1)
    finally:
        await subscription.close()
        await collision.close()


@pytest.mark.anyio
async def test_redis_streams_deliver_across_connections_and_isolate_pairs(
    redis_url: str,
) -> None:
    namespace = f"streams_{uuid4().hex}"
    publisher = RedisStreamsProvider(redis_url, namespace)
    subscriber = RedisStreamsProvider(redis_url, namespace)
    await publisher.start()
    await subscriber.start()
    try:
        await _assert_live_delivery_and_isolation(publisher, subscriber)
    finally:
        await publisher.close()
        await subscriber.close()


@pytest.mark.anyio
async def test_nats_streams_deliver_across_connections_and_isolate_pairs(
    nats_url: str,
) -> None:
    namespace = f"streams_{uuid4().hex}"
    publisher = NatsStreamsProvider((nats_url,), namespace)
    subscriber = NatsStreamsProvider((nats_url,), namespace)
    await publisher.start()
    await subscriber.start()
    try:
        await _assert_live_delivery_and_isolation(publisher, subscriber)
    finally:
        await publisher.close()
        await subscriber.close()


@pytest.mark.anyio
async def test_nats_stream_subscription_close_unblocks_receive(nats_url: str) -> None:
    provider = NatsStreamsProvider((nats_url,), f"streams_{uuid4().hex}")
    await provider.start()
    subscription = await provider.subscribe("events", "updates")
    receive = asyncio.create_task(subscription.receive())
    try:
        # The nats-py SDK defaults next_msg's own timeout to 1 second; an
        # unbounded receive() must outlast that without raising, or it is
        # not actually blocking indefinitely.
        await asyncio.sleep(1.2)
        assert not receive.done()

        await subscription.close()
        with pytest.raises(StreamSubscriptionClosedError):
            await asyncio.wait_for(receive, timeout=3)
    finally:
        await provider.close()
