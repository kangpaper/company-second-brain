import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


def test_migrated_postgres_enforces_cross_organization_membership() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        transaction = connection.begin()
        ids = connection.execute(
            text(
                """
                INSERT INTO organizations (id, name, slug, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), 'Integration A', 'integration-a', now(), now()),
                  (gen_random_uuid(), 'Integration B', 'integration-b', now(), now())
                RETURNING id
                """
            )
        ).scalars().all()
        org_a, org_b = ids
        workspace_a = connection.execute(
            text(
                """
                INSERT INTO workspaces
                  (id, organization_id, name, slug, settings, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), :organization_id, 'Main', 'main', '{}', now(), now())
                RETURNING id
                """
            ),
            {"organization_id": org_a},
        ).scalar_one()
        user_b = connection.execute(
            text(
                """
                INSERT INTO users
                  (id, organization_id, email, display_name, created_at, updated_at)
                VALUES
                  (
                    gen_random_uuid(), :organization_id, 'integration@example.com',
                    'User', now(), now()
                  )
                RETURNING id
                """
            ),
            {"organization_id": org_b},
        ).scalar_one()

        try:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        """
                        INSERT INTO memberships
                          (id, organization_id, workspace_id, user_id, role, created_at, updated_at)
                        VALUES
                          (
                            gen_random_uuid(), :organization_id, :workspace_id,
                            :user_id, 'owner', now(), now()
                          )
                        """
                    ),
                    {
                        "organization_id": org_a,
                        "workspace_id": workspace_a,
                        "user_id": user_b,
                    },
                )
        finally:
            transaction.rollback()
