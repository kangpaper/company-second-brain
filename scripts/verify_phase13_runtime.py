import os
import socket
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
        details = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{details[-5000:]}")
    if result.stdout.strip():
        print(result.stdout.strip())


def reserve_loopback_port() -> tuple[socket.socket, int]:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    except BaseException as error:
        try:
            reservation.close()
        except BaseException as cleanup_error:
            error.add_note(
                "Port reservation cleanup also failed "
                f"({type(cleanup_error).__name__}); acquisition failure preserved"
            )
        raise
    return reservation, port


def wait_for_tcp(process: subprocess.Popen[str], port: int, name: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"{name} exited before readiness: {output[-2000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{name} readiness timeout")


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    root = Path(__file__).resolve().parents[1]
    base_url = make_url(database_url)
    database_name = f"company_brain_phase13_{uuid.uuid4().hex[:10]}"
    admin_engine = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    api_server: subprocess.Popen[str] | None = None
    mcp_server: subprocess.Popen[str] | None = None
    reservations: list[socket.socket] = []
    database_created = False
    primary_error: BaseException | None = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database_created = True
        fresh_url = base_url.set(database=database_name).render_as_string(hide_password=False)
        environment = os.environ.copy()
        environment["DATABASE_URL"] = fresh_url
        environment["TEST_DATABASE_URL"] = fresh_url
        alembic = str(root / ".venv" / "Scripts" / "alembic.exe")
        python = str(root / ".venv" / "Scripts" / "python.exe")

        run_checked([alembic, "upgrade", "head"], root=root, environment=environment)
        run_checked(
            [alembic, "downgrade", "545c74fcdca1"],
            root=root,
            environment=environment,
        )
        run_checked([alembic, "upgrade", "head"], root=root, environment=environment)
        run_checked([alembic, "check"], root=root, environment=environment)
        run_checked(
            [
                python,
                "-m",
                "pytest",
                "apps/api/tests/unit/test_generic_mcp_client.py",
                "apps/api/tests/acceptance/test_generic_mcp_integration_api.py",
                "apps/api/tests/integration/test_generic_mcp_postgres.py",
                "-q",
            ],
            root=root,
            environment=environment,
        )

        mcp_reservation, mcp_port = reserve_loopback_port()
        reservations.append(mcp_reservation)
        api_reservation, api_port = reserve_loopback_port()
        reservations.append(api_reservation)
        environment["PHASE13_MCP_PORT"] = str(mcp_port)
        environment["PHASE13_API_URL"] = f"http://127.0.0.1:{api_port}"
        environment["MCP_CREDENTIAL_KEYS"] = "runtime-knowledge"
        environment["MCP_CREDENTIAL_RUNTIME_KNOWLEDGE"] = "runtime-server-owned-token"
        environment["MCP_ALLOWED_HOSTS"] = "runtime.mcp.example"

        mcp_reservation.close()
        reservations.remove(mcp_reservation)
        mcp_server = subprocess.Popen(
            [python, str(root / "scripts" / "phase13_mock_mcp_server.py")],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for_tcp(mcp_server, mcp_port, "mock MCP server")

        api_reservation.close()
        reservations.remove(api_reservation)
        api_server = subprocess.Popen(
            [
                str(root / ".venv" / "Scripts" / "uvicorn.exe"),
                "phase13_runtime_app:app",
                "--app-dir",
                str(root / "scripts"),
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if api_server.poll() is not None:
                output = api_server.stdout.read() if api_server.stdout else ""
                raise RuntimeError(f"Uvicorn exited before readiness: {output[-2000:]}")
            try:
                response = httpx.get(f"http://127.0.0.1:{api_port}/health", timeout=1)
                if response.status_code == 200 and response.json() == {
                    "service": "company-second-brain-api",
                    "status": "ok",
                }:
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Uvicorn readiness timeout")

        run_checked(
            [python, str(root / "scripts" / "phase13_mcp_runtime_smoke.py")],
            root=root,
            environment=environment,
        )
        if environment.get("PHASE13_INJECT_LATE_FAILURE") == "1":
            raise RuntimeError("injected Phase 13 late failure")
        print("phase13_fresh_zero_to_head=passed")
        print("phase13_migration_round_trip=passed")
        print("phase13_alembic_drift=none")
        print(f"phase13_mock_mcp_port={mcp_port}")
        print(f"phase13_tcp_runtime_port={api_port}")
        print("phase13_postgresql_contract=passed")
        print("phase13_tcp_runtime=passed")
    except BaseException as error:
        primary_error = error
    finally:
        cleanup_errors: list[BaseException] = []
        for reservation in reservations:
            try:
                reservation.close()
            except BaseException as error:
                cleanup_errors.append(error)
        for process in (api_server, mcp_server):
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except BaseException as error:
                cleanup_errors.append(error)
        if database_created:
            try:
                with admin_engine.connect() as connection:
                    connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :database_name"
                        ),
                        {"database_name": database_name},
                    )
                    connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
                print("phase13_runtime_database_dropped=true")
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            admin_engine.dispose()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is None:
                cleanup_error = cleanup_errors[0]
                for additional_error in cleanup_errors[1:]:
                    cleanup_error.add_note(
                        f"Additional Phase 13 cleanup failure ({type(additional_error).__name__})"
                    )
                raise cleanup_error
            for cleanup_error in cleanup_errors:
                primary_error.add_note(
                    "Phase 13 cleanup also failed "
                    f"({type(cleanup_error).__name__}); primary failure preserved"
                )
    if primary_error is not None:
        raise primary_error


if __name__ == "__main__":
    main()
