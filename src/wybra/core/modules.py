from __future__ import annotations

from typing import Final

# Core modules are prepended during config and data-surface discovery so
# framework-owned settings and migrations are available before app modules.
# Write targets, such as generated migration revisions, must still require an
# explicitly configured app module and should not use this list as permission.
CORE_MODULES: Final = ("wybra.sessions",)


__all__ = ("CORE_MODULES",)
