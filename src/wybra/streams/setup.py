from __future__ import annotations

import asyncio

from wybra.services.secrets import SecretsCapability
from wybra.site import Site, SiteCapabilityProxy
from wybra.streams.capabilities import DefaultStreamsCapability
from wybra.streams.connection import resolve_nats_connection_options, resolve_redis_url
from wybra.streams.contracts import StreamsCapability
from wybra.streams.memory import InMemoryStreamsProvider
from wybra.streams.nats import NatsStreamsProvider
from wybra.streams.redis import RedisStreamsProvider
from wybra.streams.settings import StreamsSettings


async def setup_site(site: Site) -> None:
    settings = StreamsSettings.load_settings(site.config)
    secrets = site.capability_proxy(SecretsCapability)

    async def finalise_streams_setup() -> None:
        await _finalise_streams_setup(site, settings, secrets)

    site.defer_setup_finalisation(finalise_streams_setup)


async def _finalise_streams_setup(
    site: Site,
    settings: StreamsSettings,
    secrets: SiteCapabilityProxy[SecretsCapability],
) -> None:
    secrets_capability = None
    if settings.requires_secret_resolution:
        secrets_capability = await secrets.finalise_required()
    provider = await _provider_from_settings(settings, secrets_capability)
    capability = DefaultStreamsCapability(provider, settings.diagnostics())
    try:
        site.provide_capability(StreamsCapability, capability)
    except BaseException:
        await capability.close()
        raise


async def _provider_from_settings(
    settings: StreamsSettings,
    secrets: SecretsCapability | None,
) -> InMemoryStreamsProvider | NatsStreamsProvider | RedisStreamsProvider:
    if settings.backend == "memory":
        return InMemoryStreamsProvider()
    if settings.backend == "redis":
        provider = RedisStreamsProvider(
            resolve_redis_url(settings, secrets),
            settings.namespace,
        )
    else:
        assert settings.servers is not None
        provider = NatsStreamsProvider(
            settings.servers,
            settings.namespace,
            resolve_nats_connection_options(settings, secrets),
        )
    try:
        await provider.start()
    except BaseException as setup_error:
        try:
            await provider.close()
        except asyncio.CancelledError as close_error:
            close_error.add_note(
                f"Streams provider startup also failed with {type(setup_error).__name__}: {setup_error}"
            )
            raise close_error from setup_error
        except BaseException as close_error:
            if isinstance(setup_error, asyncio.CancelledError):
                setup_error.add_note(
                    f"Streams provider startup cleanup also failed with {type(close_error).__name__}: {close_error}"
                )
                raise setup_error from close_error
            raise BaseExceptionGroup(
                "Streams provider startup and cleanup failed.",
                [setup_error, close_error],
            ) from setup_error
        raise
    return provider


__all__ = ("setup_site",)
