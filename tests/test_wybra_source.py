from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_wybra_source_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "wybra_source.py"
    spec = importlib.util.spec_from_file_location("wybra_source", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pyproject(path: Path, source_lines: str) -> None:
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "consumer"',
                "",
                "[tool.uv.sources]",
                source_lines,
                "",
                "[dependency-groups]",
                "dev = []",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_wybra_source_executable_reports_project_source(tmp_path: Path) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                wybra_source.GIT_SOURCE,
                f"# {wybra_source.PATH_SOURCE}",
            ]
        ),
    )
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "wybra_source.py"

    result = subprocess.run(
        [sys.executable, script_path, "check", "--pyproject", pyproject],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "git\n"
    assert result.stderr == ""


def test_wybra_source_switches_between_git_and_local_path(tmp_path: Path) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                wybra_source.GIT_SOURCE,
                f"# {wybra_source.PATH_SOURCE}",
            ]
        ),
    )

    assert wybra_source.main(["path", "--pyproject", str(pyproject)]) == 0
    text = pyproject.read_text(encoding="utf-8")
    assert f"# {wybra_source.GIT_SOURCE}" in text
    assert wybra_source.PATH_SOURCE in text

    assert wybra_source.main(["git", "--pyproject", str(pyproject)]) == 0
    text = pyproject.read_text(encoding="utf-8")
    assert wybra_source.GIT_SOURCE in text
    assert f"# {wybra_source.PATH_SOURCE}" in text


def test_wybra_source_preserves_other_uv_sources(tmp_path: Path) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    other_source = 'dbscripts = { git = "https://github.com/deeprave/dbscripts" }'
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                wybra_source.GIT_SOURCE,
                other_source,
                f"# {wybra_source.PATH_SOURCE}",
            ]
        ),
    )

    assert wybra_source.main(["path", "--pyproject", str(pyproject)]) == 0

    text = pyproject.read_text(encoding="utf-8")
    assert other_source in text
    assert f"# {wybra_source.GIT_SOURCE}" in text
    assert wybra_source.PATH_SOURCE in text


def test_wybra_source_mode_command_syncs_when_source_already_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wybra_source = _load_wybra_source_module()
    project_root = tmp_path
    (project_root / ".git").mkdir()
    pyproject = project_root / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                f"# {wybra_source.GIT_SOURCE}",
                wybra_source.PATH_SOURCE,
            ]
        ),
    )
    calls: list[tuple[list[str], bool, Path]] = []

    def fake_run(args: list[str], *, check: bool, cwd: Path) -> None:
        calls.append((args, check, cwd))

    monkeypatch.setattr(wybra_source.subprocess, "run", fake_run)

    assert wybra_source.main(["path", "--pyproject", str(pyproject)]) == 0

    assert calls == [
        (
            ["uv", "lock", "--upgrade-package", wybra_source.WYBRA_PACKAGE],
            True,
            project_root,
        ),
        (["uv", "sync"], True, project_root),
    ]


def test_wybra_source_check_reports_current_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                f"# {wybra_source.GIT_SOURCE}",
                wybra_source.PATH_SOURCE,
            ]
        ),
    )

    assert wybra_source.main(["check", "--pyproject", str(pyproject)]) == 0
    assert capsys.readouterr().out == "path\n"

    assert wybra_source.main(["git", "--pyproject", str(pyproject)]) == 0
    assert wybra_source.main(["check", "--pyproject", str(pyproject)]) == 0
    assert capsys.readouterr().out == "git\n"


def test_wybra_source_check_enforces_expected_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                f"# {wybra_source.GIT_SOURCE}",
                wybra_source.PATH_SOURCE,
            ]
        ),
    )

    assert wybra_source.main(["check", "git", "--pyproject", str(pyproject)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "expected git" in captured.err

    assert wybra_source.main(["check", "path", "--pyproject", str(pyproject)]) == 0
    assert capsys.readouterr().out == "path\n"


def test_wybra_source_reports_missing_pyproject(tmp_path: Path) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "missing.toml"

    with pytest.raises(SystemExit) as exc_info:
        wybra_source.main(["check", "--pyproject", str(pyproject)])

    assert str(exc_info.value.code) == (
        f"Expected pyproject configuration at: {pyproject}"
    )


def test_wybra_source_quiet_check_suppresses_current_mode_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                wybra_source.GIT_SOURCE,
                f"# {wybra_source.PATH_SOURCE}",
            ]
        ),
    )

    assert wybra_source.main(["check", "git", "-q", "--pyproject", str(pyproject)]) == 0
    assert capsys.readouterr().out == ""


def test_wybra_source_rejects_ambiguous_active_sources(tmp_path: Path) -> None:
    wybra_source = _load_wybra_source_module()
    pyproject = tmp_path / "pyproject.toml"
    _write_pyproject(
        pyproject,
        "\n".join(
            [
                wybra_source.GIT_SOURCE,
                wybra_source.PATH_SOURCE,
            ]
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        wybra_source.main(["check", "--pyproject", str(pyproject)])

    assert str(exc_info.value.code) == (
        "[tool.uv.sources] must contain exactly one active Wybra source line."
    )
