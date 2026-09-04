import base64
from collections.abc import AsyncIterator
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import (
    Document,
    DocumentVersion,
    Evidence,
    EvidenceLink,
    ExtractionCandidate,
    IngestionRun,
    Membership,
    Organization,
    Source,
    SourceAsset,
    User,
    Workspace,
)
from company_brain.ingestion import parsers
from company_brain.main import app


def ingestion_scope(session: Session, suffix: str, role: str = "editor") -> dict[str, str]:
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug="main",
        settings={},
    )
    token = f"token-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
        display_name=suffix,
        api_token_hash=sha256(token.encode()).hexdigest(),
    )
    session.add_all([workspace, user])
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role=role,
        )
    )
    session.commit()
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


@pytest.fixture
async def client(session: Session) -> AsyncIterator[httpx.AsyncClient]:
    def override_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value
    app.dependency_overrides.clear()


async def test_ingestion_persists_source_run_and_candidates(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "success")
    content = b"name,revenue\nAcme,1200\nBeta,900\n"

    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://customers.csv",
            "filename": "customers.csv",
            "media_type": "text/csv",
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["candidate_count"] == 2
    assert body["content_hash"] == sha256(content).hexdigest()
    source = session.get(Source, UUID(body["source_id"]))
    run = session.get(IngestionRun, UUID(body["id"]))
    assert source is not None and source.uri == "upload://customers.csv"
    assert run is not None and run.error_code is None
    candidates = list(
        session.scalars(
            select(ExtractionCandidate).where(ExtractionCandidate.ingestion_run_id == run.id)
        )
    )
    assert [candidate.data["name"] for candidate in candidates] == ["Acme", "Beta"]
    assert candidates[0].locator == {"row": 2}

    detail = await client.get(f"/api/v1/ingestions/{body['id']}", headers=headers)
    assert detail.status_code == 200
    assert len(detail.json()["candidates"]) == 2


async def test_ingestion_preserves_raw_source_and_builds_reviewable_markdown(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "reviewable")
    content = b"Invoice number INV-42\nAmount due: 1,200 USD\nPayment due date: 2026-09-15"

    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://INV-42.txt",
            "filename": "INV-42.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["classification"]["document_type"] == "invoice"
    assert body["classification"]["confidence"] >= 0.8
    assert body["review_status"] == "pending"
    assert body["normalized_markdown"].startswith("---\n")
    assert "# INV-42" in body["normalized_markdown"]
    assert body["document_id"] is None

    run = session.get(IngestionRun, UUID(body["id"]))
    assert run is not None and run.source_asset_id is not None
    asset = session.get(SourceAsset, run.source_asset_id)
    assert asset is not None
    assert asset.content == content
    assert asset.content_hash == sha256(content).hexdigest()

    detail = await client.get(f"/api/v1/ingestions/{body['id']}", headers=headers)
    assert detail.json()["normalized_markdown"] == body["normalized_markdown"]


async def test_operator_promotes_ingestion_to_canonical_knowledge_with_evidence(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "promote")
    content = b"Meeting notes\nAttendees: Alice, Bob\nAction items: send renewal proposal"
    created = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://renewal-meeting.txt",
            "filename": "renewal-meeting.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    promoted = await client.post(
        f"/api/v1/ingestions/{created.json()['id']}/promote",
        headers=headers,
        json={"path": "uploads/renewal-meeting.md"},
    )

    assert promoted.status_code == 201
    body = promoted.json()
    assert body["review_status"] == "promoted"
    assert body["document_id"]
    assert body["document_version_id"]

    document = session.get(Document, UUID(body["document_id"]))
    version = session.get(DocumentVersion, UUID(body["document_version_id"]))
    run = session.get(IngestionRun, UUID(created.json()["id"]))
    assert document is not None and document.path == "uploads/renewal-meeting.md"
    assert version is not None and version.markdown == created.json()["normalized_markdown"]
    assert run is not None and run.review_status == "promoted"
    assert list(
        session.scalars(
            select(ExtractionCandidate.status).where(
                ExtractionCandidate.ingestion_run_id == run.id
            )
        )
    ) == ["accepted"]

    evidence = session.scalar(select(Evidence).where(Evidence.source_id == run.source_id))
    assert evidence is not None
    assert evidence.pointer["ingestion_run_id"] == str(run.id)
    assert evidence.pointer["document_version_id"] == str(version.id)
    link = session.scalar(select(EvidenceLink).where(EvidenceLink.evidence_id == evidence.id))
    assert link is not None and link.document_id == document.id

    duplicate = await client.post(
        f"/api/v1/ingestions/{created.json()['id']}/promote",
        headers=headers,
        json={"path": "uploads/renewal-meeting.md"},
    )
    assert duplicate.status_code == 409


async def test_operator_can_list_and_reject_pending_ingestion(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "reject")
    created = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://obsolete-report.txt",
            "filename": "obsolete-report.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"obsolete customer report").decode(),
        },
    )

    queue = await client.get(
        "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
    )
    assert queue.status_code == 200
    assert created.json()["id"] in {item["id"] for item in queue.json()}

    rejected = await client.post(
        f"/api/v1/ingestions/{created.json()['id']}/reject",
        headers=headers,
        json={"reason": "Duplicate of an existing customer report"},
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["review_status"] == "rejected"
    assert body["review_reason"] == "Duplicate of an existing customer report"
    assert body["reviewed_by"]
    assert body["reviewed_at"]

    pending = await client.get(
        "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
    )
    assert created.json()["id"] not in {item["id"] for item in pending.json()}


async def test_rejection_reason_must_contain_non_whitespace_text(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "blank-reject")
    created = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://blank-reject.txt",
            "filename": "blank-reject.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"review me").decode(),
        },
    )

    response = await client.post(
        f"/api/v1/ingestions/{created.json()['id']}/reject",
        headers=headers,
        json={"reason": "   "},
    )

    assert response.status_code == 422
    run = session.get(IngestionRun, UUID(created.json()["id"]))
    assert run is not None and run.review_status == "pending"


async def test_browser_multipart_upload_uses_canonical_ingestion_pipeline(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "multipart")
    content = b"Security policy\nAccess control requirements"

    response = await client.post(
        "/api/v1/ingestions/upload",
        headers=headers,
        files={"file": ("security-policy.txt", content, "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["classification"]["document_type"] == "policy"
    assert body["review_status"] == "pending"
    asset = session.get(SourceAsset, UUID(body["source_asset_id"]))
    assert asset is not None and asset.content == content


async def test_plain_text_ingestion_succeeds_through_http(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "plain-text")
    content = b"Acme customer call notes"

    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://call-notes.txt",
            "filename": "call-notes.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    assert response.status_code == 201
    assert response.json()["candidate_count"] == 1
    run = session.get(IngestionRun, UUID(response.json()["id"]))
    assert run is not None and run.extracted_text == "Acme customer call notes"


async def test_failed_parse_is_audited_without_candidates(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "failure")

    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://broken.pdf",
            "filename": "broken.pdf",
            "media_type": "application/pdf",
            "content_base64": base64.b64encode(b"not a pdf").decode(),
        },
    )

    assert response.status_code == 422
    run_id = UUID(response.json()["detail"]["run_id"])
    run = session.get(IngestionRun, run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "parse_error"
    assert "Invalid PDF" in (run.error_message or "")
    assert list(
        session.scalars(
            select(ExtractionCandidate).where(ExtractionCandidate.ingestion_run_id == run_id)
        )
    ) == []
    queue = await client.get(
        "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
    )
    assert run_id not in {UUID(item["id"]) for item in queue.json()}

    run.status = "succeeded"
    run.extracted_text = "Legacy successful extraction"
    run.error_code = None
    run.error_message = None
    session.commit()
    legacy_queue = await client.get(
        "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
    )
    assert run_id not in {UUID(item["id"]) for item in legacy_queue.json()}


async def test_incomplete_legacy_run_cannot_be_promoted_or_rejected_directly(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "legacy-review")
    created = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://legacy-invoice.txt",
            "filename": "legacy-invoice.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(b"Invoice amount due").decode(),
        },
    )
    assert created.status_code == 201
    run_id = UUID(created.json()["id"])
    run = session.get(IngestionRun, run_id)
    assert run is not None
    run.source_asset_id = None
    run.document_type = None
    run.classification_confidence = None
    run.classification_method = None
    run.classification_reason = None
    session.commit()

    queue = await client.get(
        "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
    )
    assert run_id not in {UUID(item["id"]) for item in queue.json()}
    promoted = await client.post(
        f"/api/v1/ingestions/{run_id}/promote",
        headers=headers,
        json={"path": "legacy/incomplete.md"},
    )
    rejected = await client.post(
        f"/api/v1/ingestions/{run_id}/reject",
        headers=headers,
        json={"reason": "Incomplete legacy intake"},
    )
    assert promoted.status_code == 409
    assert rejected.status_code == 409
    session.refresh(run)
    assert run.review_status == "pending"


async def test_oversized_normalized_markdown_preserves_original_asset(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "normalization-bound")
    content = b"x" * 2_000_000

    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://large.txt",
            "filename": "large.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "normalization_error"
    run = session.get(IngestionRun, UUID(response.json()["detail"]["run_id"]))
    assert run is not None and run.status == "failed"
    asset = session.get(SourceAsset, run.source_asset_id)
    assert asset is not None and asset.content == content


async def test_ingestion_requires_writer_and_is_tenant_safe(
    client: httpx.AsyncClient, session: Session
) -> None:
    writer = ingestion_scope(session, "writer")
    member = ingestion_scope(session, "member", role="member")
    other = ingestion_scope(session, "other")
    payload = {
        "source_type": "upload",
        "uri": "upload://note.md",
        "filename": "note.md",
        "media_type": "text/markdown",
        "content_base64": base64.b64encode(b"# Note\n\nSafe").decode(),
    }

    denied = await client.post("/api/v1/ingestions", headers=member, json=payload)
    created = await client.post("/api/v1/ingestions", headers=writer, json=payload)
    hidden = await client.get(
        f"/api/v1/ingestions/{created.json()['id']}", headers=other
    )

    assert denied.status_code == 403
    assert created.status_code == 201
    assert hidden.status_code == 404


async def test_ingestion_rejects_invalid_base64_without_creating_audit_rows(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers = ingestion_scope(session, "base64")

    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://bad.md",
            "filename": "bad.md",
            "media_type": "text/markdown",
            "content_base64": "not valid ***",
        },
    )

    assert response.status_code == 422
    assert session.scalar(select(IngestionRun)) is None
    assert session.scalar(select(Source)) is None


async def test_unexpected_parser_failure_is_audited_without_leaking_details(
    client: httpx.AsyncClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = ingestion_scope(session, "parser-crash")

    def crash(_: bytes):
        raise RuntimeError("sensitive internal parser detail")

    monkeypatch.setitem(parsers.PARSERS, "text/markdown", crash)
    response = await client.post(
        "/api/v1/ingestions",
        headers=headers,
        json={
            "source_type": "upload",
            "uri": "upload://crash.md",
            "filename": "crash.md",
            "media_type": "text/markdown",
            "content_base64": base64.b64encode(b"# Crash").decode(),
        },
    )

    assert response.status_code == 422
    assert "sensitive" not in response.text
    run = session.get(IngestionRun, UUID(response.json()["detail"]["run_id"]))
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "parser_error"
    assert run.error_message == "Parser failed unexpectedly"
