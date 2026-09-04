import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    Evidence,
    EvidenceLink,
    ExtractionCandidate,
    IngestionRun,
    Organization,
    Source,
    SourceAsset,
    User,
    Workspace,
)

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def ingestion_fixture(session: Session):
    organization = Organization(name="Ingestion", slug=f"ingestion-{uuid4()}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug="main",
        settings={},
    )
    session.add(workspace)
    session.flush()
    first = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="upload",
        uri="upload://first.csv",
    )
    second = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="upload",
        uri="upload://second.csv",
    )
    session.add_all([first, second])
    session.flush()
    run = IngestionRun(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=first.id,
        status="succeeded",
        filename="first.csv",
        media_type="text/csv",
        content_hash="a" * 64,
        byte_size=10,
        extracted_text="valid",
        candidate_count=1,
    )
    session.add(run)
    session.flush()
    return organization, workspace, first, second, run


def test_candidate_source_must_match_ingestion_run_source() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        _, workspace, _, second, run = ingestion_fixture(session)
        session.add(
            ExtractionCandidate(
                organization_id=run.organization_id,
                workspace_id=workspace.id,
                ingestion_run_id=run.id,
                source_id=second.id,
                candidate_index=0,
                candidate_type="row",
                locator={"row": 2},
                data={},
                text="wrong provenance",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_ingestion_run_rejects_source_asset_from_different_source() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        _, workspace, first, second, _ = ingestion_fixture(session)
        asset = SourceAsset(
            organization_id=first.organization_id,
            workspace_id=workspace.id,
            source_id=second.id,
            filename="second.csv",
            media_type="text/csv",
            content_hash="d" * 64,
            byte_size=6,
            content=b"second",
        )
        session.add(asset)
        session.flush()
        session.add(
            IngestionRun(
                organization_id=first.organization_id,
                workspace_id=workspace.id,
                source_id=first.id,
                source_asset_id=asset.id,
                status="succeeded",
                filename="wrong-asset.csv",
                media_type="text/csv",
                content_hash="d" * 64,
                byte_size=6,
                extracted_text="second",
                candidate_count=0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_source_asset_is_immutable() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        _, workspace, first, _, _ = ingestion_fixture(session)
        asset = SourceAsset(
            organization_id=first.organization_id,
            workspace_id=workspace.id,
            source_id=first.id,
            filename="original.txt",
            media_type="text/plain",
            content_hash="e" * 64,
            byte_size=8,
            content=b"original",
        )
        session.add(asset)
        session.commit()

        asset.filename = "mutated.txt"
        with pytest.raises(ProgrammingError, match="source_assets are immutable"):
            session.flush()
        session.rollback()

        with pytest.raises(ProgrammingError, match="source_assets are immutable"):
            session.delete(asset)
            session.flush()
        session.rollback()


def promoted_ingestion_fixture(session: Session):
    organization, workspace, source, _, run = ingestion_fixture(session)
    reviewer = User(
        organization_id=organization.id,
        email=f"reviewer-{uuid4()}@example.com",
        display_name="Reviewer",
    )
    document = Document(
        organization_id=organization.id,
        workspace_id=workspace.id,
        title="Promoted document",
        path=f"promoted/{uuid4()}.md",
        properties={"title": "Promoted document"},
    )
    session.add_all([reviewer, document])
    session.flush()
    version = DocumentVersion(
        organization_id=organization.id,
        workspace_id=workspace.id,
        document_id=document.id,
        version_number=1,
        markdown="# Promoted document\n\nTrusted content",
        plain_text="Promoted document Trusted content",
        frontmatter={"title": "Promoted document"},
        tags=[],
        content_hash="f" * 64,
    )
    session.add(version)
    session.flush()
    chunk = DocumentChunk(
        organization_id=organization.id,
        workspace_id=workspace.id,
        document_id=document.id,
        version_id=version.id,
        chunk_index=0,
        heading_path=["Promoted document"],
        text="Trusted content",
        start_offset=0,
        end_offset=15,
        content_hash="1" * 64,
    )
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="ingested_document",
        pointer={
            "ingestion_run_id": str(run.id),
            "document_version_id": str(version.id),
            "content_hash": run.content_hash,
        },
        quote="Trusted content",
    )
    candidate = ExtractionCandidate(
        organization_id=organization.id,
        workspace_id=workspace.id,
        ingestion_run_id=run.id,
        source_id=source.id,
        candidate_index=0,
        candidate_type="section",
        locator={"start_line": 1},
        data={},
        text="Trusted content",
        status="accepted",
    )
    session.add_all([chunk, evidence, candidate])
    session.flush()
    link = EvidenceLink(
        organization_id=organization.id,
        workspace_id=workspace.id,
        evidence_id=evidence.id,
        document_id=document.id,
    )
    session.add(link)
    run.review_status = "promoted"
    run.reviewed_by = reviewer.id
    run.reviewed_at = reviewer.created_at
    run.document_id = document.id
    run.document_version_id = version.id
    session.commit()
    return run, candidate, chunk, evidence, link


def test_terminal_ingestion_review_and_candidates_are_immutable() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        run, candidate, _, _, _ = promoted_ingestion_fixture(session)
        run.review_reason = "rewrite terminal audit"
        with pytest.raises(ProgrammingError, match="terminal ingestion reviews are immutable"):
            session.flush()
        session.rollback()

        candidate.status = "rejected"
        with pytest.raises(ProgrammingError, match="terminal extraction candidates are immutable"):
            session.flush()
        session.rollback()


def test_promoted_chunks_and_provenance_are_immutable() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        _, _, chunk, evidence, link = promoted_ingestion_fixture(session)
        chunk.text = "rewritten chunk"
        with pytest.raises(ProgrammingError, match="document_chunks are immutable"):
            session.flush()
        session.rollback()

        evidence.quote = "rewritten evidence"
        with pytest.raises(ProgrammingError, match="promoted ingestion evidence is immutable"):
            session.flush()
        session.rollback()

        session.delete(link)
        with pytest.raises(
            ProgrammingError, match="promoted ingestion evidence links are immutable"
        ):
            session.flush()
        session.rollback()


def test_promoted_version_and_provenance_reject_new_members() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        run, _, chunk, evidence, link = promoted_ingestion_fixture(session)
        session.add(
            DocumentChunk(
                organization_id=chunk.organization_id,
                workspace_id=chunk.workspace_id,
                document_id=chunk.document_id,
                version_id=chunk.version_id,
                chunk_index=1,
                heading_path=["Injected"],
                text="Injected chunk",
                start_offset=16,
                end_offset=30,
                content_hash="2" * 64,
            )
        )
        with pytest.raises(
            ProgrammingError, match="promoted document chunk membership is sealed"
        ):
            session.flush()
        session.rollback()

        session.add(
            EvidenceLink(
                organization_id=link.organization_id,
                workspace_id=link.workspace_id,
                evidence_id=evidence.id,
                document_id=link.document_id,
            )
        )
        with pytest.raises(
            ProgrammingError, match="promoted ingestion evidence link membership is sealed"
        ):
            session.flush()
        session.rollback()

        session.add(
            Evidence(
                organization_id=evidence.organization_id,
                workspace_id=evidence.workspace_id,
                source_id=evidence.source_id,
                evidence_type="ingested_document",
                pointer={
                    "ingestion_run_id": str(run.id),
                    "document_version_id": str(chunk.version_id),
                    "content_hash": run.content_hash,
                },
                quote="Injected evidence",
            )
        )
        with pytest.raises(
            ProgrammingError, match="promoted ingestion evidence membership is sealed"
        ):
            session.flush()
        session.rollback()


def test_ingestion_run_rejects_cross_tenant_source() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, first, _, _ = ingestion_fixture(session)
        other = Organization(name="Other", slug=f"other-{uuid4()}")
        session.add(other)
        session.flush()
        other_workspace = Workspace(
            organization_id=other.id,
            name="Main",
            slug="main",
            settings={},
        )
        session.add(other_workspace)
        session.flush()
        session.add(
            IngestionRun(
                organization_id=other.id,
                workspace_id=other_workspace.id,
                source_id=first.id,
                status="failed",
                filename="cross.csv",
                media_type="text/csv",
                content_hash="b" * 64,
                byte_size=1,
                candidate_count=0,
                error_code="parse_error",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        assert organization.id != other.id
        assert workspace.id != other_workspace.id


@pytest.mark.parametrize(
    ("status", "extracted_text", "candidate_count", "error_code"),
    [
        ("running", None, 0, None),
        ("succeeded", None, 0, None),
        ("succeeded", "ok", 0, "unexpected"),
        ("failed", None, 0, None),
        ("failed", None, 1, "parse_error"),
    ],
)
def test_ingestion_run_enforces_audit_state_consistency(
    status: str,
    extracted_text: str | None,
    candidate_count: int,
    error_code: str | None,
) -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        _, workspace, first, _, _ = ingestion_fixture(session)
        session.add(
            IngestionRun(
                organization_id=first.organization_id,
                workspace_id=workspace.id,
                source_id=first.id,
                status=status,
                filename="state.csv",
                media_type="text/csv",
                content_hash="c" * 64,
                byte_size=1,
                extracted_text=extracted_text,
                candidate_count=candidate_count,
                error_code=error_code,
                error_message="parse failed" if error_code else None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
