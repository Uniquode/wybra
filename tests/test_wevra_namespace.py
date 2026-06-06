import ast
from pathlib import Path

FORBIDDEN_IMPORT_ROOTS = {
    "auth_ext",
    "data_core",
    "public",
    "tools",
    "web_core",
}
FORBIDDEN_TEXT_TOKENS = (
    "web_core",
    "data_core",
    "auth_ext",
    "src/public",
    "src/tools",
)
LIVE_TEXT_FILE_NAMES = ("README.md", "app.toml", "alembic.ini", "pyproject.toml")


def _python_files(project_root: Path) -> tuple[Path, ...]:
    roots = (project_root / "src", project_root / "tests")
    files: list[Path] = []
    for root in roots:
        files.extend(path for path in root.rglob("*.py") if path.is_file())
    return tuple(files)


def _live_text_files(project_root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for file_name in LIVE_TEXT_FILE_NAMES
        if (path := project_root / file_name).is_file()
    )


def _live_openspec_text_files(project_root: Path) -> tuple[Path, ...]:
    specs_root = project_root / "openspec" / "specs"
    changes_root = project_root / "openspec" / "changes"
    files: list[Path] = []
    if specs_root.is_dir():
        files.extend(path for path in specs_root.rglob("*.md") if path.is_file())
    if changes_root.is_dir():
        files.extend(
            path
            for path in changes_root.rglob("*.md")
            if path.is_file()
            and path.relative_to(changes_root).parts[:1] != ("archive",)
        )
    return tuple(files)


def test_live_python_imports_do_not_reference_old_reusable_package_roots() -> None:
    project_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in _python_files(project_root):
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_name = alias.name.split(".", maxsplit=1)[0]
                    if root_name in FORBIDDEN_IMPORT_ROOTS:
                        offenders.append(
                            f"{path.relative_to(project_root)} imports {root_name!r}"
                        )
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                root_name = node.module.split(".", maxsplit=1)[0]
                if root_name in FORBIDDEN_IMPORT_ROOTS:
                    offenders.append(
                        f"{path.relative_to(project_root)} imports {root_name!r}"
                    )

    assert offenders == []


def test_live_project_metadata_does_not_reference_old_reusable_package_names() -> None:
    project_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in _live_text_files(project_root):
        content = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TEXT_TOKENS:
            if token in content:
                offenders.append(f"{path.relative_to(project_root)} contains {token!r}")

    assert offenders == []


def test_live_openspec_docs_do_not_reference_old_reusable_package_names() -> None:
    project_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in _live_openspec_text_files(project_root):
        content = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TEXT_TOKENS:
            if token in content:
                offenders.append(f"{path.relative_to(project_root)} contains {token!r}")

    assert offenders == []
