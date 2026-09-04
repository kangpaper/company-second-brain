import base64
import binascii
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.api.documents import (
    new_version,
    parse_payload,
    persist_chunks,
    persist_links,
    resolve_links_for_title,
)
from company_brain.db.session import get_session
from company_brain.domain.models import (
    Document,
    Evidence,
    EvidenceLink,
    ExtractionCandidate,
    IngestionRun,
)
from company_brain.domain.repositories import TenantScope
from company_brain.ingestion.parsers import MAX_INPUT_BYTES
from company_brain.ingestion.service import IntakeInput, IntakeProcessingError, stage_intake

router = APIRouter(prefix="/api/v1/ingestions", tags=["ingestions"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
WriterDependency = Annotated[Principal, Depends(require_writer)]
MAX_BASE64_CHARS = ((MAX_INPUT_BYTES + 2) // 3) * 4


class IngestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str = Field(min_length=1, max_length=100)
    uri: str = Field(min_length=1, max_length=2048)
    filename: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=MAX_BASE64_CHARS)


class PromotionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=2048, pattern=r"^[^\\]+\.md$")


class RejectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=2000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("reason must contain at least 3 non-whitespace characters")
        return normalized


class CandidateRead(BaseModel):
    id: UUID
    candidate_index: int
    candidate_type: str
    locator: dict[str, Any]
    data: dict[str, Any]
    text: str
    status: str


class ClassificationRead(BaseModel):
    document_type: str
    confidence: float
    method: str
    reason: str


class IngestionRead(BaseModel):
    id: UUID
    source_id: UUID
    source_asset_id: UUID | None
    status: str
    filename: str
    media_type: str
    content_hash: str
    byte_size: int
    parser_metadata: dict[str, Any]
    candidate_count: int
    classification: ClassificationRead | None
    normalized_markdown: str | None
    review_status: str
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_reason: str | None
    document_id: UUID | None
    document_version_id: UUID | None
    error_code: str | None
    error_message: str | None


class IngestionDetail(IngestionRead):
    candidates: list[CandidateRead]


def ingestion_read(run: IngestionRun) -> IngestionRead:
    classification = None
    if (
        run.document_type is not None
        and run.classification_confidence is not None
        and run.classification_method is not None
        and run.classification_reason is not None
    ):
        classification = ClassificationRead(
            document_type=run.document_type,
            confidence=run.classification_confidence,
            method=run.classification_method,
            reason=run.classification_reason,
        )
    return IngestionRead(
        id=run.id,
        source_id=run.source_id,
        source_asset_id=run.source_asset_id,
        status=run.status,
        filename=run.filename,
        media_type=run.media_type,
        content_hash=run.content_hash,
        byte_size=run.byte_size,
        parser_metadata=run.parser_metadata,
        candidate_count=run.candidate_count,
        classification=classification,
        normalized_markdown=run.normalized_markdown,
        review_status=run.review_status,
        reviewed_by=run.reviewed_by,
        reviewed_at=run.reviewed_at,
        review_reason=run.review_reason,
        document_id=run.document_id,
        document_version_id=run.document_version_id,
        error_code=run.error_code,
        error_message=run.error_message,
    )


def actionable_markdown(run: IngestionRun) -> str:
    if (
        run.status != "succeeded"
        or run.source_asset_id is None
        or run.document_type is None
        or run.classification_confidence is None
        or run.classification_method is None
        or run.classification_reason is None
        or run.normalized_markdown is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only complete successful ingestions can be reviewed",
        )
    return run.normalized_markdown


def decode_content(payload: IngestionCreate) -> bytes:
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="content_base64 must be valid base64",
        ) from error
    if len(content) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Decoded content exceeds 10 MiB",
        )
    return content


@router.post("", response_model=IngestionRead, status_code=status.HTTP_201_CREATED)
def create_ingestion(
    payload: IngestionCreate,
    session: SessionDependency,
    scope: ScopeDependency,
    _: WriterDependency,
) -> IngestionRead:
    content = decode_content(payload)
    intake = IntakeInput(
        source_type=payload.source_type,
        uri=payload.uri,
        filename=payload.filename,
        media_type=payload.media_type,
    )
    try:
        run = stage_intake(session, scope, intake, content)
    except IntakeProcessingError as error:
        session.commit()
        session.refresh(error.run)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "run_id": str(error.run.id),
                "code": error.code,
                "message": error.message,
            },
        ) from error
    session.commit()
    session.refresh(run)
    return ingestion_read(run)


@router.get("", response_model=list[IngestionRead])
def list_ingestions(
    session: SessionDependency,
    scope: ScopeDependency,
    review_status: Annotated[
        str, Query(pattern="^(pending|promoted|rejected)$")
    ] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IngestionRead]:
    runs = session.scalars(
        select(IngestionRun)
        .where(
            IngestionRun.organization_id == scope.organization_id,
            IngestionRun.workspace_id == scope.workspace_id,
            IngestionRun.status == "succeeded",
            IngestionRun.review_status == review_status,
            IngestionRun.source_asset_id.is_not(None),
            IngestionRun.document_type.is_not(None),
            IngestionRun.classification_confidence.is_not(None),
            IngestionRun.classification_method.is_not(None),
            IngestionRun.classification_reason.is_not(None),
            IngestionRun.normalized_markdown.is_not(None),
        )
        .order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc())
        .limit(limit)
    ).all()
    return [ingestion_read(run) for run in runs]


@router.post(
    "/upload",
    response_model=IngestionRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_ingestion(
    file: Annotated[UploadFile, File()],
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
) -> IngestionRead:
    filename = (file.filename or "").strip()
    if not filename or len(filename) > 500:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded filename is required and must not exceed 500 characters",
        )
    content = await file.read(MAX_INPUT_BYTES + 1)
    await file.close()
    if len(content) > MAX_INPUT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Uploaded content exceeds 10 MiB",
        )
    media_type = (file.content_type or "application/octet-stream").strip()
    payload = IngestionCreate(
        source_type="upload",
        uri=f"upload://{uuid4()}/{filename}",
        filename=filename,
        media_type=media_type,
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    return create_ingestion(payload, session, scope, principal)


@router.post(
    "/{run_id}/promote",
    response_model=IngestionRead,
    status_code=status.HTTP_201_CREATED,
)
def promote_ingestion(
    run_id: UUID,
    payload: PromotionCreate,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
) -> IngestionRead:
    run = session.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.id == run_id,
            IngestionRun.organization_id == scope.organization_id,
            IngestionRun.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion not found")
    normalized_markdown = actionable_markdown(run)
    if run.review_status != "pending" or run.document_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ingestion has already been reviewed",
        )

    parsed = parse_payload(normalized_markdown)
    document = Document(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        title=str(parsed.frontmatter["title"]).strip(),
        path=payload.path,
        properties=parsed.frontmatter,
    )
    try:
        session.add(document)
        session.flush()
        version = new_version(document, 1, normalized_markdown, parsed)
        session.add(version)
        session.flush()
        persist_chunks(document, version, session)
        persist_links(document, version, parsed, session)
        resolve_links_for_title(document.title, document, session)

        evidence = Evidence(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            source_id=run.source_id,
            evidence_type="ingested_document",
            pointer={
                "ingestion_run_id": str(run.id),
                "source_asset_id": str(run.source_asset_id),
                "document_version_id": str(version.id),
                "content_hash": run.content_hash,
            },
            quote=(run.extracted_text or "")[:2000] or None,
        )
        session.add(evidence)
        session.flush()
        session.add(
            EvidenceLink(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                evidence_id=evidence.id,
                document_id=document.id,
            )
        )
        session.execute(
            update(ExtractionCandidate)
            .where(
                ExtractionCandidate.ingestion_run_id == run.id,
                ExtractionCandidate.organization_id == scope.organization_id,
                ExtractionCandidate.workspace_id == scope.workspace_id,
                ExtractionCandidate.status == "pending",
            )
            .values(status="accepted")
        )
        run.review_status = "promoted"
        run.reviewed_by = principal.user_id
        run.reviewed_at = datetime.now(UTC)
        run.document_id = document.id
        run.document_version_id = version.id
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document path already exists or promotion conflicted",
        ) from error
    session.refresh(run)
    return ingestion_read(run)


@router.post("/{run_id}/reject", response_model=IngestionRead)
def reject_ingestion(
    run_id: UUID,
    payload: RejectionCreate,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
) -> IngestionRead:
    run = session.scalar(
        select(IngestionRun)
        .where(
            IngestionRun.id == run_id,
            IngestionRun.organization_id == scope.organization_id,
            IngestionRun.workspace_id == scope.workspace_id,
        )
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion not found")
    actionable_markdown(run)
    if run.review_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ingestion has already been reviewed",
        )

    session.execute(
        update(ExtractionCandidate)
        .where(
            ExtractionCandidate.ingestion_run_id == run.id,
            ExtractionCandidate.organization_id == scope.organization_id,
            ExtractionCandidate.workspace_id == scope.workspace_id,
            ExtractionCandidate.status == "pending",
        )
        .values(status="rejected")
    )
    run.review_status = "rejected"
    run.reviewed_by = principal.user_id
    run.reviewed_at = datetime.now(UTC)
    run.review_reason = payload.reason.strip()
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ingestion review conflicted",
        ) from error
    session.refresh(run)
    return ingestion_read(run)


@router.get("/{run_id}", response_model=IngestionDetail)
def get_ingestion(
    run_id: UUID, session: SessionDependency, scope: ScopeDependency
) -> IngestionDetail:
    run = session.scalar(
        select(IngestionRun).where(
            IngestionRun.id == run_id,
            IngestionRun.organization_id == scope.organization_id,
            IngestionRun.workspace_id == scope.workspace_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion not found")
    candidates = list(
        session.scalars(
            select(ExtractionCandidate)
            .where(
                ExtractionCandidate.ingestion_run_id == run.id,
                ExtractionCandidate.organization_id == scope.organization_id,
                ExtractionCandidate.workspace_id == scope.workspace_id,
            )
            .order_by(ExtractionCandidate.candidate_index)
            .limit(5000)
        )
    )
    return IngestionDetail(
        **ingestion_read(run).model_dump(),
        candidates=[
            CandidateRead.model_validate(candidate, from_attributes=True)
            for candidate in candidates
        ],
    )
