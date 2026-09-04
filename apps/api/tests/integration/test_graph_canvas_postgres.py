import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def test_canvas_rejects_cross_organization_workspace() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        transaction = connection.begin()
        organization_ids = connection.execute(
            text(
                "INSERT INTO organizations(id,name,slug,created_at,updated_at) VALUES "
                "(gen_random_uuid(),'Canvas A','canvas-a-' || gen_random_uuid(),now(),now()),"
                "(gen_random_uuid(),'Canvas B','canvas-b-' || gen_random_uuid(),now(),now()) "
                "RETURNING id"
            )
        ).scalars().all()
        organization_a, organization_b = organization_ids
        workspace_a = connection.execute(
            text(
                "INSERT INTO workspaces(id,organization_id,name,slug,settings,"
                "created_at,updated_at) "
                "VALUES(gen_random_uuid(),:organization_id,'Main','main','{}',now(),now()) "
                "RETURNING id"
            ),
            {"organization_id": organization_a},
        ).scalar_one()
        try:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO canvases(id,organization_id,workspace_id,title,path,content,"
                        "created_at,updated_at) VALUES(gen_random_uuid(),:organization_id,"
                        ":workspace_id,'Cross tenant','cross.canvas','{}',now(),now())"
                    ),
                    {"organization_id": organization_b, "workspace_id": workspace_a},
                )
        finally:
            transaction.rollback()


def test_canvas_path_is_unique_per_workspace() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        transaction = connection.begin()
        organization_id = connection.execute(
            text(
                "INSERT INTO organizations(id,name,slug,created_at,updated_at) "
                "VALUES(gen_random_uuid(),'Canvas Unique','canvas-u-' || gen_random_uuid(),"
                "now(),now()) RETURNING id"
            )
        ).scalar_one()
        workspace_id = connection.execute(
            text(
                "INSERT INTO workspaces(id,organization_id,name,slug,settings,"
                "created_at,updated_at) "
                "VALUES(gen_random_uuid(),:organization_id,'Main','main','{}',now(),now()) "
                "RETURNING id"
            ),
            {"organization_id": organization_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO canvases(id,organization_id,workspace_id,title,path,content,"
                "created_at,updated_at) VALUES(gen_random_uuid(),:organization_id,"
                ":workspace_id,'First','same.canvas','{}',now(),now())"
            ),
            {"organization_id": organization_id, "workspace_id": workspace_id},
        )
        try:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO canvases(id,organization_id,workspace_id,title,path,content,"
                        "created_at,updated_at) VALUES(gen_random_uuid(),:organization_id,"
                        ":workspace_id,'Second','same.canvas','{}',now(),now())"
                    ),
                    {"organization_id": organization_id, "workspace_id": workspace_id},
                )
        finally:
            transaction.rollback()


def test_graph_timeline_composite_indexes_exist() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        definitions = " ".join(
            connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='public' "
                    "AND tablename IN ('relationships', 'events')"
                )
            ).scalars()
        )

    assert "organization_id, workspace_id, from_entity_id" in definitions
    assert "organization_id, workspace_id, to_entity_id" in definitions
    assert "organization_id, workspace_id, occurred_at DESC, id DESC" in definitions
