from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_serializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.api.dependencies import get_tenant_scope
from company_brain.db.session import get_session
from company_brain.domain.models import IntegrationAudit
from company_brain.domain.repositories import TenantScope

router = APIRouter(prefix="/api/v1/integration-audits", tags=["integration-audits"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]


class IntegrationAuditItem(BaseModel):
    id: UUID
    provider: str
    operation: str
    tool_name: str | None
    outcome: str
    error_code: str | None
    error_message: str | None
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return utc_value.isoformat().replace("+00:00", "Z")


@router.get("", response_model=list[IntegrationAuditItem])
def list_integration_audits(
    session: SessionDependency,
    scope: ScopeDependency,
    provider: Annotated[str, Query(min_length=1, max_length=50)] = "mcp",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[IntegrationAudit]:
    statement = (
        select(IntegrationAudit)
        .where(
            IntegrationAudit.organization_id == scope.organization_id,
            IntegrationAudit.workspace_id == scope.workspace_id,
            IntegrationAudit.provider == provider,
        )
        .order_by(IntegrationAudit.created_at.desc(), IntegrationAudit.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(session.scalars(statement))
