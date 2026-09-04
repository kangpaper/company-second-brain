import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    root = Path(__file__).resolve().parents[1]
    base_url = make_url(database_url)
    database_name = f"company_brain_runtime_{uuid.uuid4().hex[:10]}"
    admin_engine = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    fresh_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    server: subprocess.Popen[str] | None = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        environment = os.environ.copy()
        environment["DATABASE_URL"] = fresh_url
        environment["TEST_DATABASE_URL"] = fresh_url
        subprocess.run(
            [str(root / ".venv" / "Scripts" / "alembic.exe"), "upgrade", "head"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        server = subprocess.Popen(
            [
                str(root / ".venv" / "Scripts" / "uvicorn.exe"),
                "company_brain.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8022",
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
                response = httpx.get("http://127.0.0.1:8022/health", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Uvicorn readiness timeout")
        result = subprocess.run(
            [
                str(root / ".venv" / "Scripts" / "python.exe"),
                str(root / "scripts" / "phase7_entity_resolution_runtime_smoke.py"),
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        print(result.stdout.strip())
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
        print("runtime_database_dropped=true")


if __name__ == "__main__":
    main()
