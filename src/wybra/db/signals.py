"""Application import namespace for supported native Tortoise model signals.

These are direct re-exports.  Their registration and lifecycle semantics are
owned by Tortoise; Wybra does not wrap or otherwise alter them.
"""

from tortoise.signals import post_delete, post_save, pre_delete, pre_save

__all__ = (
    "post_delete",
    "post_save",
    "pre_delete",
    "pre_save",
)
