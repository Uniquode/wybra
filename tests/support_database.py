from pathlib import Path

from wybra.db.urls import sqlite_file_url

__all__ = ("sqlite_file_url", "sync_sqlite_file_url")


def sync_sqlite_file_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"
