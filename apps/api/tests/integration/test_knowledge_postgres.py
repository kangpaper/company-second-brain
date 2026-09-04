import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import sessionmaker

from company_brain.api.dependencies import Principal
from company_brain.api.documents import DocumentUpdate, update_document
from company_brain.domain.models import Document, DocumentVersion, Organization, Workspace
from company_brain.domain.repositories import TenantScope

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def create_document_version_fixture(connection):
    organization_id = connection.execute(
        text(
            "INSERT INTO organizations(id,name,slug,created_at,updated_at) "
            "VALUES(gen_random_uuid(),'Knowledge Integration',"
            "'knowledge-' || gen_random_uuid(),now(),now()) RETURNING id"
        )
    ).scalar_one()
    workspace_id = connection.execute(
        text(
            "INSERT INTO workspaces(id,organization_id,name,slug,settings,created_at,updated_at) "
            "VALUES(gen_random_uuid(),:organization_id,'Main','main','{}',now(),now()) "
            "RETURNING id"
        ),
        {"organization_id": organization_id},
    ).scalar_one()
    document_id = connection.execute(
        text(
            "INSERT INTO documents(id,organization_id,workspace_id,title,path,content,properties,"
            "created_at,updated_at) VALUES(gen_random_uuid(),:organization_id,:workspace_id,"
            "'Immutable','immutable.md','v1','{}',now(),now()) RETURNING id"
        ),
        {"organization_id": organization_id, "workspace_id": workspace_id},
    ).scalar_one()
    version_id = connection.execute(
        text(
            "INSERT INTO document_versions(id,organization_id,workspace_id,document_id,"
            "version_number,markdown,plain_text,frontmatter,tags,content_hash,"
            "created_at,updated_at) "
            "VALUES(gen_random_uuid(),:organization_id,:workspace_id,:document_id,1,'v1','v1',"
            "'{}','[]',repeat('a',64),now(),now()) RETURNING id"
        ),
        {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "document_id": document_id,
        },
    ).scalar_one()
    return organization_id, workspace_id, document_id, version_id


def test_document_versions_are_append_only_in_postgres() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        transaction = connection.begin()
        _, _, _, version_id = create_document_version_fixture(connection)
        try:
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(
                    text("UPDATE document_versions SET markdown='mutated' WHERE id=:id"),
                    {"id": version_id},
                )
        finally:
            transaction.rollback()


def test_application_role_cannot_bypass_append_only_delete() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        transaction = connection.begin()
        _, _, _, version_id = create_document_version_fixture(connection)
        try:
            connection.execute(text("SET LOCAL company_brain.allow_version_delete = 'on'"))
            with pytest.raises(DatabaseError, match="append-only"):
                connection.execute(
                    text("DELETE FROM document_versions WHERE id=:id"),
                    {"id": version_id},
                )
        finally:
            transaction.rollback()


def test_document_link_source_version_must_belong_to_source_document() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        transaction = connection.begin()
        organization_id, workspace_id, first_document_id, _ = create_document_version_fixture(
            connection
        )
        second_document_id = connection.execute(
            text(
                "INSERT INTO documents(id,organization_id,workspace_id,title,path,"
                "content,properties,"
                "created_at,updated_at) VALUES(gen_random_uuid(),:organization_id,:workspace_id,"
                "'Second','second.md','','{}',now(),now()) RETURNING id"
            ),
            {"organization_id": organization_id, "workspace_id": workspace_id},
        ).scalar_one()
        second_version_id = connection.execute(
            text(
                "INSERT INTO document_versions(id,organization_id,workspace_id,document_id,"
                "version_number,markdown,plain_text,frontmatter,tags,content_hash,"
                "created_at,updated_at) "
                "VALUES(gen_random_uuid(),:organization_id,:workspace_id,:document_id,1,'v1','v1',"
                "'{}','[]',repeat('b',64),now(),now()) RETURNING id"
            ),
            {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "document_id": second_document_id,
            },
        ).scalar_one()
        try:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO document_links(id,organization_id,workspace_id,"
                        "source_document_id,source_version_id,raw_target,normalized_target,active,"
                        "created_at,updated_at) VALUES(gen_random_uuid(),:organization_id,"
                        ":workspace_id,:source_document_id,:source_version_id,'Missing','missing',"
                        "true,now(),now())"
                    ),
                    {
                        "organization_id": organization_id,
                        "workspace_id": workspace_id,
                        "source_document_id": first_document_id,
                        "source_version_id": second_version_id,
                    },
                )
        finally:
            transaction.rollback()


def test_concurrent_document_updates_create_distinct_versions() -> None:
    engine = postgres_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    organization_id = uuid4()
    workspace_id = uuid4()
    document_id = uuid4()
    with factory() as session:
        session.add(Organization(id=organization_id, name="Concurrent", slug=f"c-{uuid4()}"))
        session.add(
            Workspace(
                id=workspace_id,
                organization_id=organization_id,
                name="Main",
                slug="main",
            )
        )
        session.flush()
        session.add(
            Document(
                id=document_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                title="Concurrent",
                path="concurrent.md",
            )
        )
        session.add(
            DocumentVersion(
                organization_id=organization_id,
                workspace_id=workspace_id,
                document_id=document_id,
                version_number=1,
                markdown="---\ntitle: Concurrent\n---\nv1",
                content_hash=sha256(b"v1").hexdigest(),
            )
        )
        session.commit()

    principal = Principal(
        user_id=uuid4(),
        role="editor",
        scope=TenantScope(organization_id=organization_id, workspace_id=workspace_id),
    )

    def write_version(label: str) -> int:
        with factory() as session:
            result = update_document(
                document_id,
                DocumentUpdate(markdown=f"---\ntitle: Concurrent\n---\n{label}"),
                principal,
                session,
            )
            return result.current_version

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = sorted(executor.map(write_version, ["v2", "v3"]))
    assert versions == [2, 3]
