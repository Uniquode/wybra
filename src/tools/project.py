from pathlib import Path


def runtime_project_root() -> Path:
    source_project_root = Path(__file__).resolve().parents[2]
    if (source_project_root / "pyproject.toml").is_file():
        return source_project_root

    return Path.cwd()
