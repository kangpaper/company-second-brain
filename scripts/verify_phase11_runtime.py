import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def _run_checked(command: list[str], *, root: Path, environment: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{details[-5000:]}")
    if result.stdout.strip():
        print(result.stdout.strip())


def _reserve_loopback_port() -> tuple[socket.socket, int]:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.bind(("127.0.0.1", 0))
    return reservation, int(reservation.getsockname()[1])


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    root = Path(__file__).resolve().parents[1]
    base_url = make_url(database_url)
    database_name = f"company_brain_phase11_{uuid.uuid4().hex[:10]}"
    admin_engine = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    fresh_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    server: subprocess.Popen[str] | None = None
    port_reservation: socket.socket | None = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        environment = os.environ.copy()
        environment["DATABASE_URL"] = fresh_url
        environment["TEST_DATABASE_URL"] = fresh_url
        alembic = str(root / ".venv" / "Scripts" / "alembic.exe")
        python = str(root / ".venv" / "Scripts" / "python.exe")
        _run_checked([alembic, "upgrade", "head"], root=root, environment=environment)
        _run_checked([alembic, "check"], root=root, environment=environment)
        _run_checked(
            [
                python,
                "-m",
                "pytest",
                "apps/api/tests/unit/test_risk_engine.py",
                "apps/api/tests/unit/test_odoo_mapping.py",
                "apps/api/tests/acceptance/test_customer_360_api.py",
                "-q",
            ],
            root=root,
            environment=environment,
        )
        port_reservation, runtime_port = _reserve_loopback_port()
        runtime_url = f"http://127.0.0.1:{runtime_port}"
        environment["PHASE11_API_URL"] = runtime_url
        port_reservation.close()
        port_reservation = None
        server = subprocess.Popen(
            [
                str(root / ".venv" / "Scripts" / "uvicorn.exe"),
                "company_brain.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(runtime_port),
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if server.poll() is not None:
                output = server.stdout.read() if server.stdout else ""
                raise RuntimeError(f"Uvicorn exited before readiness: {output[-2000:]}")
            try:
                response = httpx.get(f"{runtime_url}/health", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Uvicorn readiness timeout")
        _run_checked(
            [python, str(root / "scripts" / "phase11_risk_runtime_smoke.py")],
            root=root,
            environment=environment,
        )
    finally:
        if port_reservation is not None:
            port_reservation.close()
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
        print("phase11_runtime_database_dropped=true")


if __name__ == "__main__":
    main()
