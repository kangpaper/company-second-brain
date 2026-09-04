import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def run_checked(command: list[str], *, root: Path, environment: dict[str, str]) -> None:
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


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    root = Path(__file__).resolve().parents[1]
    base_url = make_url(database_url)
    database_name = f"company_brain_phase10_{uuid.uuid4().hex[:10]}"
    admin_engine = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    fresh_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = fresh_url
    environment["TEST_DATABASE_URL"] = fresh_url
    environment["PHASE10_API_URL"] = "http://127.0.0.1:8025"
    server: subprocess.Popen[str] | None = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        run_checked(
            [str(root / ".venv" / "Scripts" / "alembic.exe"), "upgrade", "head"],
            root=root,
            environment=environment,
        )
        run_checked(
            [str(root / ".venv" / "Scripts" / "alembic.exe"), "check"],
            root=root,
            environment=environment,
        )
        run_checked(
            [
                str(root / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "pytest",
                "apps/api/tests/integration/test_reasoning_run_postgres.py",
                "-q",
            ],
            root=root,
            environment=environment,
        )
        server = subprocess.Popen(
            [
                str(root / ".venv" / "Scripts" / "uvicorn.exe"),
                "phase10_runtime_app:app",
                "--app-dir",
                str(root / "scripts"),
                "--host",
                "127.0.0.1",
                "--port",
                "8025",
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
                response = httpx.get("http://127.0.0.1:8025/health", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Uvicorn readiness timeout")
        run_checked(
            [
                str(root / ".venv" / "Scripts" / "python.exe"),
                str(root / "scripts" / "phase10_ai_runtime_smoke.py"),
            ],
            root=root,
            environment=environment,
        )
        print("phase10_fresh_zero_to_head=passed")
        print("phase10_alembic_drift=none")
        print("phase10_postgresql_audit=passed")
        print("phase10_tcp_runtime=passed")
    finally:
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
        print("phase10_runtime_database_dropped=true")


if __name__ == "__main__":
    main()
