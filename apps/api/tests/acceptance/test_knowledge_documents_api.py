from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import (
    Document,
    DocumentChunk,
    DocumentLink,
    DocumentVersion,
    Membership,
    Organization,
    User,
    Workspace,
)
from company_brain.main import app


@pytest.fixture
async def client(session: Session) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_session] = lambda: session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def knowledge_scope(session: Session, suffix: str = "knowledge") -> dict[str, str]:
    token = f"{suffix}-token"
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Knowledge",
        slug="knowledge",
    )
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
        display_name="Knowledge Editor",
        api_token_hash=sha256(token.encode()).hexdigest(),
    )
    session.add_all([workspace, user])
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role="editor",
        )
    )
    session.commit()
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


async def test_create_markdown_document_parses_frontmatter_and_creates_version_one(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session)
    markdown = """---
title: Customer ABC Review
type: customer-review
tags:
  - customer
  - risk
status: approved
---

# Review

Revenue declined. See [[Payment Policy]].
"""

    response = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "customers/abc-review.md", "markdown": markdown},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Customer ABC Review"
    assert body["path"] == "customers/abc-review.md"
    assert body["current_version"] == 1
    assert body["properties"]["type"] == "customer-review"
    assert body["properties"]["status"] == "approved"
    assert body["tags"] == ["customer", "risk"]
    assert body["markdown"] == markdown


async def test_update_document_creates_immutable_version_history(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "versions")
    version_one = "---\ntitle: Policy\ntags: [draft]\n---\n\nOriginal"
    created = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "policy.md", "markdown": version_one},
    )
    version_two = "---\ntitle: Policy\ntags: [approved]\n---\n\nUpdated"

    updated = await client.patch(
        f"/api/v1/documents/{created.json()['id']}",
        headers=headers,
        json={"markdown": version_two},
    )
    history = await client.get(
        f"/api/v1/documents/{created.json()['id']}/versions", headers=headers
    )

    assert updated.status_code == 200
    assert updated.json()["current_version"] == 2
    assert history.status_code == 200
    assert [item["version_number"] for item in history.json()] == [2, 1]
    assert history.json()[1]["markdown"] == version_one


async def test_invalid_frontmatter_returns_422_without_creating_document(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "invalid-yaml")

    response = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "bad.md", "markdown": "---\ntitle: [broken\n---\nBody"},
    )

    assert response.status_code == 422
    listing = await client.get("/api/v1/documents", headers=headers)
    assert listing.status_code == 200
    assert listing.json() == []


async def test_unresolved_link_becomes_backlink_when_target_is_created(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "backlinks")
    source = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={
            "path": "review.md",
            "markdown": "---\ntitle: Review\n---\nSee [[Payment Policy]].",
        },
    )
    unresolved = await client.get(f"/api/v1/documents/{source.json()['id']}/links", headers=headers)
    assert unresolved.status_code == 200
    assert unresolved.json()[0]["raw_target"] == "Payment Policy"
    assert unresolved.json()[0]["resolved"] is False

    target = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={
            "path": "payment-policy.md",
            "markdown": "---\ntitle: Payment Policy\n---\nApproved policy.",
        },
    )
    backlinks = await client.get(
        f"/api/v1/documents/{target.json()['id']}/backlinks", headers=headers
    )

    assert backlinks.status_code == 200
    assert backlinks.json() == [
        {
            "document_id": source.json()["id"],
            "title": "Review",
            "raw_target": "Payment Policy",
        }
    ]


async def test_search_finds_current_content_filters_tags_and_is_tenant_safe(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers_a = knowledge_scope(session, "search-a")
    headers_b = knowledge_scope(session, "search-b")
    await client.post(
        "/api/v1/documents",
        headers=headers_a,
        json={
            "path": "abc.md",
            "markdown": (
                "---\ntitle: Customer ABC Review\ntags: [risk, customer]\n---\n"
                "Payment delay increased this quarter."
            ),
        },
    )
    await client.post(
        "/api/v1/documents",
        headers=headers_a,
        json={
            "path": "policy.md",
            "markdown": "---\ntitle: Payment Policy\ntags: [policy]\n---\nPayment terms.",
        },
    )
    await client.post(
        "/api/v1/documents",
        headers=headers_b,
        json={
            "path": "secret.md",
            "markdown": "---\ntitle: Secret Delay\ntags: [risk]\n---\nPayment delay secret.",
        },
    )

    response = await client.get(
        "/api/v1/search", params={"q": "payment delay", "tag": "risk"}, headers=headers_a
    )

    assert response.status_code == 200
    assert [item["title"] for item in response.json()] == ["Customer ABC Review"]
    assert "Payment delay" in response.json()[0]["snippet"]


async def test_duplicate_title_makes_existing_link_unresolved(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "ambiguous-link")
    source = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={
            "path": "source.md",
            "markdown": "---\ntitle: Source\n---\nSee [[Policy]].",
        },
    )
    await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "policy-a.md", "markdown": "---\ntitle: Policy\n---\nA"},
    )
    assert (
        await client.get(f"/api/v1/documents/{source.json()['id']}/links", headers=headers)
    ).json()[0]["resolved"] is True

    await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "policy-b.md", "markdown": "---\ntitle: Policy\n---\nB"},
    )

    links = await client.get(f"/api/v1/documents/{source.json()['id']}/links", headers=headers)
    assert links.json()[0]["resolved"] is False


async def test_renaming_target_unresolves_links_to_its_old_title(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "rename-target")
    source = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "source.md", "markdown": "---\ntitle: Source\n---\n[[Policy]]"},
    )
    target = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "policy.md", "markdown": "---\ntitle: Policy\n---\nOriginal"},
    )
    await client.patch(
        f"/api/v1/documents/{target.json()['id']}",
        headers=headers,
        json={"markdown": "---\ntitle: Renamed Policy\n---\nUpdated"},
    )

    links = await client.get(f"/api/v1/documents/{source.json()['id']}/links", headers=headers)
    assert links.json()[0]["resolved"] is False


async def test_links_are_deduplicated_by_normalized_target(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "normalized-links")

    response = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={
            "path": "source.md",
            "markdown": "---\ntitle: Source\n---\n[[Policy]] and [[ policy ]]",
        },
    )

    assert response.status_code == 201
    links = await client.get(f"/api/v1/documents/{response.json()['id']}/links", headers=headers)
    assert len(links.json()) == 1


async def test_duplicate_document_path_returns_conflict_and_rolls_back(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "duplicate-path")
    payload = {"path": "same.md", "markdown": "---\ntitle: First\n---\nBody"}
    created = await client.post("/api/v1/documents", headers=headers, json=payload)
    assert created.status_code == 201

    duplicate = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "same.md", "markdown": "---\ntitle: Second\n---\nBody"},
    )
    listed = await client.get("/api/v1/documents", headers=headers)

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Document path already exists"
    assert listed.status_code == 200
    assert len(listed.json()) == 1


async def test_document_creation_persists_citation_chunks_with_exact_coordinates(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = knowledge_scope(session, "citation-chunks")
    markdown = (
        "---\ntitle: Review\n---\n# Overview\n\nRevenue declined.\n\n## Risk\n\nLate payment.\n"
    )

    response = await client.post(
        "/api/v1/documents",
        headers=headers,
        json={"path": "review.md", "markdown": markdown},
    )
    document = session.get(Document, UUID(response.json()["id"]))
    assert document is not None
    version = session.query(DocumentVersion).filter_by(document_id=document.id).one()
    chunks = (
        session.query(DocumentChunk)
        .filter_by(document_id=document.id, version_id=version.id)
        .order_by(DocumentChunk.chunk_index)
        .all()
    )

    assert [chunk.heading_path for chunk in chunks] == [["Overview"], ["Overview", "Risk"]]
    assert [markdown[chunk.start_offset : chunk.end_offset] for chunk in chunks] == [
        chunk.text for chunk in chunks
    ]
    assert chunks[0].text == "Revenue declined."


def test_document_link_rejects_source_version_from_another_document(session: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    headers = knowledge_scope(session, "link-version-owner")
    organization_id = UUID(headers["X-Organization-ID"])
    workspace_id = UUID(headers["X-Workspace-ID"])
    first = Document(
        organization_id=organization_id,
        workspace_id=workspace_id,
        title="First",
        path="first.md",
    )
    second = Document(
        organization_id=organization_id,
        workspace_id=workspace_id,
        title="Second",
        path="second.md",
    )
    session.add_all([first, second])
    session.flush()
    second_version = DocumentVersion(
        organization_id=organization_id,
        workspace_id=workspace_id,
        document_id=second.id,
        version_number=1,
        markdown="Second",
        content_hash=sha256(b"Second").hexdigest(),
    )
    session.add(second_version)
    session.flush()
    session.add(
        DocumentLink(
            organization_id=organization_id,
            workspace_id=workspace_id,
            source_document_id=first.id,
            source_version_id=second_version.id,
            raw_target="Missing",
            normalized_target="missing",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
