#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

GIT_SOURCE = (
    'wybra = { git = "https://github.com/Uniquode/wybra.git", branch = "main" }'
)
PATH_SOURCE = 'wybra = { path = "../wybra", editable = true }'
WYBRA_PACKAGE = "wybra"
_SOURCE_LINES = {GIT_SOURCE, PATH_SOURCE}
_SOURCE_LINE_VARIANTS = {*_SOURCE_LINES, f"# {GIT_SOURCE}", f"# {PATH_SOURCE}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Switch a project Wybra uv source.",
    )
    parser.add_argument(
        "mode",
        choices=("git", "path", "check"),
        help="'git' is the commit-safe source; 'path' is for local Wybra work.",
    )
    parser.add_argument(
        "expected",
        nargs="?",
        choices=("git", "path"),
        help="With 'check', require a specific active source mode.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress current-mode output for successful checks.",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="pyproject.toml path. Defaults to the current directory.",
    )
    args = parser.parse_args(argv)

    pyproject_path = args.pyproject
    if not pyproject_path.is_file():
        raise SystemExit(f"Expected pyproject configuration at: {pyproject_path}")
    lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    source_range = _uv_sources_range(lines)
    source_line_indices = _wybra_source_line_indices(lines, source_range)
    state = _source_state([lines[index] for index in source_line_indices])

    if args.mode == "check":
        if args.expected is None:
            if not args.quiet:
                print(state)
            return 0
        if state == args.expected:
            if not args.quiet:
                print(state)
            return 0
        print(
            f"pyproject.toml uses the Wybra {state} source; expected "
            f"{args.expected}. Run `python ../wybra/scripts/wybra_source.py "
            f"{args.expected}`.",
            file=sys.stderr,
        )
        return 1

    if args.expected is not None:
        parser.error("expected mode is only valid with 'check'.")

    replacement = _source_lines(args.mode)
    current_source_lines = [lines[index].strip() for index in source_line_indices]
    if current_source_lines != replacement:
        insert_at = source_line_indices[0]
        for index in reversed(source_line_indices):
            del lines[index]
        for offset, line in enumerate(replacement):
            lines.insert(insert_at + offset, line)
        pyproject_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _sync_wybra_dependency(pyproject_path.parent)
    return 0


def _sync_wybra_dependency(project_root: Path) -> None:
    if not (project_root / ".git").is_dir():
        return

    subprocess.run(
        ["uv", "lock", "--upgrade-package", WYBRA_PACKAGE],
        check=True,
        cwd=project_root,
    )
    subprocess.run(["uv", "sync"], check=True, cwd=project_root)


def _uv_sources_range(lines: list[str]) -> range:
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "[tool.uv.sources]":
            start = index + 1
            break
    if start is None:
        raise SystemExit("pyproject.toml does not contain [tool.uv.sources].")

    stop = len(lines)
    for index in range(start, len(lines)):
        line = lines[index].strip()
        if line.startswith("[") and line.endswith("]"):
            stop = index
            break
    return range(start, stop)


def _source_state(lines: list[str]) -> str:
    active_sources = [line.strip() for line in lines if line.strip() in _SOURCE_LINES]
    if active_sources == [GIT_SOURCE]:
        return "git"
    if active_sources == [PATH_SOURCE]:
        return "path"
    raise SystemExit(
        "[tool.uv.sources] must contain exactly one active Wybra source line."
    )


def _source_lines(mode: str) -> list[str]:
    if mode == "git":
        return [GIT_SOURCE, f"# {PATH_SOURCE}"]
    if mode == "path":
        return [f"# {GIT_SOURCE}", PATH_SOURCE]
    raise AssertionError(f"unsupported mode: {mode}")


def _wybra_source_line_indices(lines: list[str], source_range: range) -> list[int]:
    return [
        index for index in source_range if lines[index].strip() in _SOURCE_LINE_VARIANTS
    ]


if __name__ == "__main__":
    raise SystemExit(main())
