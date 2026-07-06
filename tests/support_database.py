from pathlib import Path


def sqlite_file_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def sync_sqlite_file_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"
