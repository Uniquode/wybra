"""Tests for the direct Tortoise-signal import namespace."""

import pytest
from tests_support.content_types.models import Article
from tortoise import signals
from tortoise.signals import Signals

from wybra.db import signals as wybra_signals
from wybra.testing import migrated_test_database


def test_wybra_database_signals_are_the_native_tortoise_signals() -> None:
    """The namespace must not wrap or replace native lifecycle signals."""

    assert wybra_signals.pre_save is signals.pre_save
    assert wybra_signals.post_save is signals.post_save
    assert wybra_signals.pre_delete is signals.pre_delete
    assert wybra_signals.post_delete is signals.post_delete


@pytest.mark.anyio
async def test_wybra_database_signals_preserve_native_save_and_delete_lifecycle() -> (
    None
):
    """Handlers receive Tortoise's original notifications without an adaptor."""

    observed: list[str] = []

    @wybra_signals.post_save(Article)
    async def after_save(*_args: object) -> None:
        observed.append("saved")

    @wybra_signals.post_delete(Article)
    async def after_delete(*_args: object) -> None:
        observed.append("deleted")

    try:
        async with migrated_test_database(modules=("tests_support.content_types",)):
            article = await Article.create(title="Signal test")
            await article.delete()
    finally:
        Article._listeners[Signals.post_save][Article].remove(after_save)
        Article._listeners[Signals.post_delete][Article].remove(after_delete)

    assert observed == ["saved", "deleted"]


@pytest.mark.anyio
async def test_wybra_native_pre_signal_retains_tortoise_veto_semantics() -> None:
    """Pre-signal failure remains Tortoise's lifecycle behaviour, unchanged."""

    @wybra_signals.pre_save(Article)
    async def veto(*_args: object) -> None:
        raise RuntimeError("native veto")

    try:
        async with migrated_test_database(modules=("tests_support.content_types",)):
            with pytest.raises(RuntimeError, match="native veto"):
                await Article.create(title="Must not persist")
    finally:
        Article._listeners[Signals.pre_save][Article].remove(veto)
