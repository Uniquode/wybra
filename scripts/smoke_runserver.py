from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from textwrap import dedent

SMOKE_PATH = "/__wybra_smoke__"
EXPECTED_BODY = "wybra smoke ok\n"
STARTUP_TIMEOUT_SECONDS = 60.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
START_ATTEMPTS = 3


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wybra-runserver-smoke-") as directory:
        project_root = Path(directory)
        _write_host_app(project_root)
        config_path = _write_app_config(project_root)
        runserver, failure = _start_responding_runserver(project_root, config_path)
        if runserver is None:
            assert failure is not None
            print(failure.message, file=sys.stderr)
            _print_process_output(failure.output)
            return 1

        _stop_process(runserver)

    return 0


def _write_host_app(project_root: Path) -> None:
    (project_root / "smoke_host.py").write_text(
        dedent(
            f"""
            from fastapi import FastAPI
            from fastapi.responses import PlainTextResponse

            import wybra

            app = FastAPI(lifespan=wybra.start_site())


            @app.get({SMOKE_PATH!r})
            async def smoke() -> PlainTextResponse:
                return PlainTextResponse({EXPECTED_BODY!r})
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (project_root / "smoke_module.py").write_text(
        dedent(
            """
            async def setup_site(site) -> None:
                del site
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_app_config(project_root: Path) -> Path:
    config_path = project_root / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["smoke_module"]
            deployment_environment = "local"

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "static"

            [app.runserver]
            asgi_app = "smoke_host:app"
            reload_env = "WYBRA_SMOKE_RELOAD"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_responding_runserver(
    project_root: Path,
    config_path: Path,
) -> tuple[RunserverProcess | None, SmokeAttemptFailure | None]:
    last_failure: SmokeAttemptFailure | None = None
    for attempt in range(START_ATTEMPTS):
        port = _unused_loopback_port()
        runserver = _start_runserver(project_root, config_path, port, attempt)
        try:
            _wait_for_response(port, runserver.process)
            return runserver, None
        except SmokeFailure as exc:
            output = _stop_process(runserver)
            last_failure = SmokeAttemptFailure(str(exc), output)
            if attempt + 1 < START_ATTEMPTS and _is_port_bind_failure(output):
                continue
            return None, last_failure
        except Exception:
            with suppress(Exception):
                _stop_process(runserver)
            raise

    return None, last_failure


def _start_runserver(
    project_root: Path,
    config_path: Path,
    port: int,
    attempt: int,
) -> RunserverProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = _python_path_with(project_root, env.get("PYTHONPATH"))
    output_path = project_root / f"runserver-{attempt}.log"
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                "uv",
                "run",
                "wybra-runserver",
                "--project",
                project_root.as_posix(),
                "--config",
                config_path.name,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-reload",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=_subprocess_creation_flags(),
            start_new_session=os.name != "nt",
        )
    return RunserverProcess(process=process, output_path=output_path)


def _python_path_with(project_root: Path, existing: str | None) -> str:
    if existing:
        return f"{project_root}{os.pathsep}{existing}"
    return str(project_root)


def _wait_for_response(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}{SMOKE_PATH}"
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(
                f"wybra-runserver exited before responding: "
                f"returncode={process.returncode}"
            )
        try:
            status, body = _http_get(port, SMOKE_PATH)
            if status != 200 or body != EXPECTED_BODY:
                raise SmokeFailure(
                    f"Unexpected smoke response: status={status} body={body!r}"
                )
            return
        except (HTTPException, OSError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise SmokeFailure(f"Timed out waiting for {url}: {last_error!r}")


def _http_get(port: int, path: str) -> tuple[int, str]:
    connection = HTTPConnection("127.0.0.1", port, timeout=1.0)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        connection.close()


def _stop_process(runserver: RunserverProcess) -> str:
    process = runserver.process
    try:
        if process.poll() is None:
            _request_process_exit(process)
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    return _read_process_output(runserver.output_path)


def _subprocess_creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return 0


def _request_process_exit(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        _kill_windows_process_tree(process)
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        process.terminate()


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        _kill_windows_process_tree(process)
        if process.poll() is None:
            process.kill()
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill()


def _kill_windows_process_tree(process: subprocess.Popen[str]) -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _read_process_output(output_path: Path) -> str:
    if output_path.exists():
        return output_path.read_text(encoding="utf-8", errors="replace")
    return ""


def _is_port_bind_failure(output: str) -> bool:
    lower_output = output.lower()
    return any(
        phrase in lower_output
        for phrase in (
            "address already in use",
            "only one usage of each socket address",
            "error while attempting to bind",
            "errno 98",
            "winerror 10048",
        )
    )


def _print_process_output(output: str) -> None:
    if output:
        print("wybra-runserver output:", file=sys.stderr)
        print(output, file=sys.stderr)


@dataclass(frozen=True, slots=True)
class RunserverProcess:
    process: subprocess.Popen[str]
    output_path: Path


@dataclass(frozen=True, slots=True)
class SmokeAttemptFailure:
    message: str
    output: str


class SmokeFailure(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
