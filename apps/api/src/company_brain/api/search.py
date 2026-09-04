from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from company_brain.api.dependencies import get_tenant_scope
from company_brain.db.session import get_session
from company_brain.domain.models import Document, DocumentVersion
from company_brain.domain.repositories import TenantScope

router = APIRouter(prefix="/api/v1/search", tags=["search"])


class SearchResult(BaseModel):
    document_id: str
    title: str
    snippet: str
    score: float


@router.get("", response_model=list[SearchResult])
def search_documents(
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    session: Annotated[Session, Depends(get_session)],
    q: Annotated[str, Query(min_length=1, max_length=500)],
    tag: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> list[SearchResult]:
    latest = (
        select(
            DocumentVersion.document_id,
            func.max(DocumentVersion.version_number).label("version_number"),
        )
        .where(
            DocumentVersion.organization_id == scope.organization_id,
            DocumentVersion.workspace_id == scope.workspace_id,
        )
        .group_by(DocumentVersion.document_id)
        .subquery()
    )
    statement = (
        select(Document, DocumentVersion)
        .join(latest, latest.c.document_id == Document.id)
        .join(
            DocumentVersion,
            (DocumentVersion.document_id == latest.c.document_id)
            & (DocumentVersion.version_number == latest.c.version_number),
        )
        .where(
            Document.organization_id == scope.organization_id,
            Document.workspace_id == scope.workspace_id,
        )
    )
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        vector = func.to_tsvector(
            "simple", func.concat(Document.title, " ", DocumentVersion.plain_text)
        )
        query = func.plainto_tsquery("simple", q)
        statement = statement.where(vector.op("@@")(query)).order_by(
            func.ts_rank(vector, query).desc()
        )
    rows = session.execute(statement).all()
    terms = q.casefold().split()
    results: list[SearchResult] = []
    for document, version in rows:
        haystack = f"{document.title} {version.plain_text}".casefold()
        if dialect != "postgresql" and not all(term in haystack for term in terms):
            continue
        if tag is not None and tag.casefold() not in {item.casefold() for item in version.tags}:
            continue
        results.append(
            SearchResult(
                document_id=str(document.id),
                title=document.title,
                snippet=version.plain_text[:240],
                score=1.0,
            )
        )
    return results
