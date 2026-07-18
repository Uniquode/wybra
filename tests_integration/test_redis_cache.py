from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import ExecWaitStrategy
from tests_support.database_containers import skip_if_docker_unavailable

from wybra.cache import RedisCache

DEFAULT_REDIS_IMAGE = "redis:8.2-alpine"
REDIS_IMAGE_ENV = "WYBRA_TESTCONTAINERS_REDIS_IMAGE"


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    skip_if_docker_unavailable()
    container = (
        DockerContainer(os.environ.get(REDIS_IMAGE_ENV, DEFAULT_REDIS_IMAGE))
        .with_exposed_ports(6379)
        .waiting_for(ExecWaitStrategy(["redis-cli", "ping"]).with_startup_timeout(30))
    )
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"Redis testcontainer could not start: {exc}")
    try:
        yield (
            f"redis://{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(6379)}/0"
        )
    finally:
        container.stop()


@pytest.mark.anyio
async def test_redis_cache_round_trips_against_real_redis(redis_url: str) -> None:
    cache = RedisCache(redis_url)

    async def unexpected_factory() -> bytes:
        pytest.fail("A fresh Redis cache value must not run its factory.")

    try:
        await cache.set("integration", "round-trip", b"first", ttl=60)

        assert await cache.get("integration", "round-trip") == b"first"
        assert (
            await cache.get_or_set(
                "integration",
                "round-trip",
                ttl=60,
                factory=unexpected_factory,
            )
            == b"first"
        )
        await cache.delete("integration", "round-trip")
        assert await cache.get("integration", "round-trip") is None
    finally:
        await cache.close()
