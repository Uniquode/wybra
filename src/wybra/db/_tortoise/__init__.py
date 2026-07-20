"""Private compatibility boundary for Tortoise internals.

Only :mod:`wybra.db` may import this package. It owns version-sensitive
instrumentation so applications and other Wybra modules never depend on
Tortoise private APIs.
"""

from __future__ import annotations

from wybra.db._tortoise.compatibility import (
    TortoiseCompatibilityError,
    ensure_supported_tortoise_version,
)

ensure_supported_tortoise_version()

from wybra.db._tortoise.instrumentation import (  # noqa: E402
    TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE,
    instrument_tortoise_connection,
    instrument_tortoise_context,
)

__all__ = (
    "TORTOISE_EVENTS_INSTRUMENTED_ATTRIBUTE",
    "TortoiseCompatibilityError",
    "ensure_supported_tortoise_version",
    "instrument_tortoise_connection",
    "instrument_tortoise_context",
)
