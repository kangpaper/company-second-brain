import os
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    root = Path(__file__).resolve().parents[1]
    base_url = make_url(database_url)
    database_name = f"company_brain_migration_{uuid.uuid4().hex[:10]}"
    admin_url = base_url.set(database="postgres")
    fresh_url = base_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        environment = os.environ.copy()
        environment["DATABASE_URL"] = fresh_url.render_as_string(hide_password=False)
        subprocess.run(
            [str(root / ".venv" / "Scripts" / "alembic.exe"), "upgrade", "head"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [str(root / ".venv" / "Scripts" / "alembic.exe"), "check"],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        print("fresh_zero_to_head=passed")
        print("fresh_alembic_drift=none")
    finally:
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
        print("temporary_database_dropped=true")


if __name__ == "__main__":
    main()
