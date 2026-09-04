from collections.abc import AsyncIterator
from hashlib import sha256

import httpx
import pytest
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import Membership, Organization, User, Workspace
from company_brain.main import app


@pytest.fixture
async def client(session: Session) -> AsyncIterator[httpx.AsyncClient]:
    def override_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api_client:
        yield api_client
    app.dependency_overrides.clear()


def create_scope(session: Session, suffix: str) -> tuple[Organization, Workspace, dict[str, str]]:
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=f"Workspace {suffix}",
        slug=f"workspace-{suffix}",
    )
    session.add(workspace)
    token = f"token-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
        display_name=f"User {suffix}",
        api_token_hash=sha256(token.encode()).hexdigest(),
    )
    session.add(user)
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
        )
    )
    session.commit()
    return organization, workspace, {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


async def test_create_and_list_entities_inside_tenant_scope(
    client: httpx.AsyncClient, session: Session
) -> None:
    _, _, headers = create_scope(session, "api")

    created = await client.post(
        "/api/v1/entities",
        headers=headers,
        json={"entity_type": "customer", "name": "ABC Ltd.", "aliases": ["ABC"]},
    )
    listed = await client.get("/api/v1/entities", headers=headers)

    assert created.status_code == 201
    assert created.json()["normalized_name"] == "abc ltd"
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["ABC Ltd."]

    entity_id = created.json()["id"]
    patched = await client.patch(
        f"/api/v1/entities/{entity_id}", headers=headers, json={"name": "ABC Europe"}
    )
    filtered = await client.get(
        "/api/v1/entities", headers=headers, params={"type": "customer", "q": "Europe"}
    )
    deleted = await client.delete(f"/api/v1/entities/{entity_id}", headers=headers)

    assert patched.status_code == 200
    assert patched.json()["normalized_name"] == "abc europe"
    assert [item["id"] for item in filtered.json()] == [entity_id]
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/entities/{entity_id}", headers=headers)).status_code == 404


async def test_entity_from_other_tenant_is_not_visible(
    client: httpx.AsyncClient, session: Session
) -> None:
    _, _, headers_a = create_scope(session, "visible")
    _, _, headers_b = create_scope(session, "hidden")
    created = await client.post(
        "/api/v1/entities",
        headers=headers_b,
        json={"entity_type": "customer", "name": "Hidden Customer"},
    )

    response = await client.get(
        f"/api/v1/entities/{created.json()['id']}",
        headers=headers_a,
    )

    assert response.status_code == 404


async def test_entity_lifecycle_cannot_be_mutated_through_generic_patch(
    client: httpx.AsyncClient, session: Session
) -> None:
    _, _, headers = create_scope(session, "entity-lifecycle")
    created = await client.post(
        "/api/v1/entities",
        headers=headers,
        json={"entity_type": "customer", "name": "ABC"},
    )

    response = await client.patch(
        f"/api/v1/entities/{created.json()['id']}",
        headers=headers,
        json={"lifecycle_status": "merged"},
    )

    assert response.status_code == 422


async def test_patch_rejects_explicit_null_for_non_nullable_entity_fields(
    client: httpx.AsyncClient, session: Session
) -> None:
    _, _, headers = create_scope(session, "entity-null")
    created = await client.post(
        "/api/v1/entities",
        headers=headers,
        json={"entity_type": "customer", "name": "ABC"},
    )

    for field in ("name", "aliases", "metadata"):
        response = await client.patch(
            f"/api/v1/entities/{created.json()['id']}",
            headers=headers,
            json={field: None},
        )
        assert response.status_code == 422, field