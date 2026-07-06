from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection
from pathlib import Path
from textwrap import dedent

SMOKE_PATH = "/__wybra_smoke__"
EXPECTED_BODY = "wybra smoke ok\n"
STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wybra-runserver-smoke-") as directory:
        project_root = Path(directory)
        _write_host_app(project_root)
        config_path = _write_app_config(project_root)
        port = _unused_loopback_port()
        process = _start_runserver(project_root, config_path, port)
        try:
            _wait_for_response(port, process)
        except SmokeFailure as exc:
            output = _stop_process(process)
            print(str(exc), file=sys.stderr)
            _print_process_output(output)
            return 1

        _stop_process(process)

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


def _start_runserver(
    project_root: Path,
    config_path: Path,
    port: int,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = _python_path_with(project_root, env.get("PYTHONPATH"))
    return subprocess.Popen(
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


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
        except OSError as exc:
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


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def _print_process_output(output: tuple[str, str]) -> None:
    stdout, stderr = output
    if stdout:
        print("wybra-runserver stdout:", file=sys.stderr)
        print(stdout, file=sys.stderr)
    if stderr:
        print("wybra-runserver stderr:", file=sys.stderr)
        print(stderr, file=sys.stderr)


class SmokeFailure(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
