from hashlib import sha256
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.db.session import get_session
from company_brain.domain.models import Document, DocumentChunk, DocumentLink, DocumentVersion
from company_brain.domain.repositories import TenantScope
from company_brain.knowledge.markdown import ParsedMarkdown, chunk_markdown, parse_markdown

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


class DocumentCreate(BaseModel):
    path: str = Field(min_length=1, max_length=2048)
    markdown: str = Field(max_length=2_000_000)


class DocumentUpdate(BaseModel):
    markdown: str = Field(max_length=2_000_000)


class DocumentRead(BaseModel):
    id: str
    title: str
    path: str
    current_version: int
    properties: dict[str, Any]
    tags: list[str]
    markdown: str


class DocumentVersionRead(BaseModel):
    version_number: int
    markdown: str
    properties: dict[str, Any]
    tags: list[str]


class DocumentLinkRead(BaseModel):
    raw_target: str
    resolved: bool


class BacklinkRead(BaseModel):
    document_id: str
    title: str
    raw_target: str


def parse_payload(markdown: str) -> ParsedMarkdown:
    try:
        parsed = parse_markdown(markdown)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    title = parsed.frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Frontmatter title is required",
        )
    return parsed


def normalize_title(title: str) -> str:
    return " ".join(title.casefold().split())


def get_document_or_404(
    document_id: UUID,
    scope: TenantScope,
    session: Session,
    *,
    for_update: bool = False,
) -> Document:
    statement = select(Document).where(
            Document.id == document_id,
            Document.organization_id == scope.organization_id,
            Document.workspace_id == scope.workspace_id,
        )
    if for_update:
        statement = statement.with_for_update()
    document = session.scalar(statement)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


def latest_version(document_id: UUID, scope: TenantScope, session: Session) -> DocumentVersion:
    version = session.scalar(
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.organization_id == scope.organization_id,
            DocumentVersion.workspace_id == scope.workspace_id,
        )
        .order_by(DocumentVersion.version_number.desc())
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return version


def to_document_read(document: Document, version: DocumentVersion) -> DocumentRead:
    return DocumentRead(
        id=str(document.id),
        title=document.title,
        path=document.path,
        current_version=version.version_number,
        properties=document.properties,
        tags=version.tags,
        markdown=version.markdown,
    )


def new_version(
    document: Document, version_number: int, markdown: str, parsed: ParsedMarkdown
) -> DocumentVersion:
    return DocumentVersion(
        organization_id=document.organization_id,
        workspace_id=document.workspace_id,
        document_id=document.id,
        version_number=version_number,
        markdown=markdown,
        plain_text=parsed.plain_text,
        frontmatter=parsed.frontmatter,
        tags=parsed.tags,
        content_hash=sha256(markdown.encode()).hexdigest(),
    )


def matching_documents(title: str, document: Document, session: Session) -> list[Document]:
    candidates = session.scalars(
        select(Document).where(
            Document.organization_id == document.organization_id,
            Document.workspace_id == document.workspace_id,
        )
    ).all()
    normalized = normalize_title(title)
    return [candidate for candidate in candidates if normalize_title(candidate.title) == normalized]


def persist_links(
    document: Document, version: DocumentVersion, parsed: ParsedMarkdown, session: Session
) -> None:
    for raw_target in parsed.links:
        matches = matching_documents(raw_target, document, session)
        session.add(
            DocumentLink(
                organization_id=document.organization_id,
                workspace_id=document.workspace_id,
                source_document_id=document.id,
                source_version_id=version.id,
                target_document_id=matches[0].id if len(matches) == 1 else None,
                raw_target=raw_target,
                normalized_target=normalize_title(raw_target),
                active=True,
            )
        )


def persist_chunks(document: Document, version: DocumentVersion, session: Session) -> None:
    for chunk in chunk_markdown(version.markdown):
        session.add(
            DocumentChunk(
                organization_id=document.organization_id,
                workspace_id=document.workspace_id,
                document_id=document.id,
                version_id=version.id,
                chunk_index=chunk.index,
                heading_path=chunk.heading_path,
                text=chunk.text,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                content_hash=chunk.content_hash,
            )
        )


def resolve_links_for_title(title: str, document: Document, session: Session) -> None:
    same_title = matching_documents(title, document, session)
    links = session.scalars(
        select(DocumentLink).where(
            DocumentLink.organization_id == document.organization_id,
            DocumentLink.workspace_id == document.workspace_id,
            DocumentLink.normalized_target == normalize_title(title),
            DocumentLink.active.is_(True),
        )
    ).all()
    target_id = same_title[0].id if len(same_title) == 1 else None
    for link in links:
        link.target_document_id = target_id


@router.post("", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: DocumentCreate,
    principal: Annotated[Principal, Depends(require_writer)],
    session: Annotated[Session, Depends(get_session)],
) -> DocumentRead:
    parsed = parse_payload(payload.markdown)
    document = Document(
        organization_id=principal.scope.organization_id,
        workspace_id=principal.scope.workspace_id,
        title=str(parsed.frontmatter["title"]).strip(),
        path=payload.path,
        properties=parsed.frontmatter,
    )
    try:
        session.add(document)
        session.flush()
        version = new_version(document, 1, payload.markdown, parsed)
        session.add(version)
        session.flush()
        persist_chunks(document, version, session)
        persist_links(document, version, parsed, session)
        resolve_links_for_title(document.title, document, session)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document path already exists",
        ) from error
    return to_document_read(document, version)


@router.get("", response_model=list[DocumentRead])
def list_documents(
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    session: Annotated[Session, Depends(get_session)],
) -> list[DocumentRead]:
    documents = session.scalars(
        select(Document)
        .where(
            Document.organization_id == scope.organization_id,
            Document.workspace_id == scope.workspace_id,
        )
        .order_by(Document.updated_at.desc())
    ).all()
    return [
        to_document_read(document, latest_version(document.id, scope, session))
        for document in documents
    ]


@router.patch("/{document_id}", response_model=DocumentRead)
def update_document(
    document_id: UUID,
    payload: DocumentUpdate,
    principal: Annotated[Principal, Depends(require_writer)],
    session: Annotated[Session, Depends(get_session)],
) -> DocumentRead:
    document = get_document_or_404(
        document_id, principal.scope, session, for_update=True
    )
    current = latest_version(document_id, principal.scope, session)
    parsed = parse_payload(payload.markdown)
    previous_title = document.title
    document.title = str(parsed.frontmatter["title"]).strip()
    document.properties = parsed.frontmatter
    try:
        version = new_version(document, current.version_number + 1, payload.markdown, parsed)
        session.add(version)
        session.flush()
        persist_chunks(document, version, session)
        session.execute(
            update(DocumentLink)
            .where(
                DocumentLink.source_document_id == document.id,
                DocumentLink.active.is_(True),
            )
            .values(active=False)
        )
        persist_links(document, version, parsed, session)
        resolve_links_for_title(previous_title, document, session)
        if normalize_title(previous_title) != normalize_title(document.title):
            resolve_links_for_title(document.title, document, session)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent document update conflict",
        ) from error
    return to_document_read(document, version)


@router.get("/{document_id}/versions", response_model=list[DocumentVersionRead])
def list_document_versions(
    document_id: UUID,
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    session: Annotated[Session, Depends(get_session)],
) -> list[DocumentVersionRead]:
    get_document_or_404(document_id, scope, session)
    versions = session.scalars(
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.organization_id == scope.organization_id,
            DocumentVersion.workspace_id == scope.workspace_id,
        )
        .order_by(DocumentVersion.version_number.desc())
    ).all()
    return [
        DocumentVersionRead(
            version_number=version.version_number,
            markdown=version.markdown,
            properties=version.frontmatter,
            tags=version.tags,
        )
        for version in versions
    ]


@router.get("/{document_id}/links", response_model=list[DocumentLinkRead])
def list_document_links(
    document_id: UUID,
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    session: Annotated[Session, Depends(get_session)],
) -> list[DocumentLinkRead]:
    get_document_or_404(document_id, scope, session)
    links = session.scalars(
        select(DocumentLink).where(
            DocumentLink.source_document_id == document_id,
            DocumentLink.organization_id == scope.organization_id,
            DocumentLink.workspace_id == scope.workspace_id,
            DocumentLink.active.is_(True),
        )
    ).all()
    return [
        DocumentLinkRead(raw_target=link.raw_target, resolved=link.target_document_id is not None)
        for link in links
    ]


@router.get("/{document_id}/backlinks", response_model=list[BacklinkRead])
def list_document_backlinks(
    document_id: UUID,
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    session: Annotated[Session, Depends(get_session)],
) -> list[BacklinkRead]:
    get_document_or_404(document_id, scope, session)
    rows = session.execute(
        select(DocumentLink, Document)
        .join(Document, Document.id == DocumentLink.source_document_id)
        .where(
            DocumentLink.target_document_id == document_id,
            DocumentLink.organization_id == scope.organization_id,
            DocumentLink.workspace_id == scope.workspace_id,
            DocumentLink.active.is_(True),
        )
    ).all()
    return [
        BacklinkRead(document_id=str(document.id), title=document.title, raw_target=link.raw_target)
        for link, document in rows
    ]
