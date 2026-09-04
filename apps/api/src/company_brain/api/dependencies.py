from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.db.session import get_session
from company_brain.domain.models import Membership, User, Workspace
from company_brain.domain.repositories import TenantScope


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    role: str
    scope: TenantScope


def get_principal(
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
    organization_id: Annotated[UUID | None, Header(alias="X-Organization-ID")] = None,
    workspace_id: Annotated[UUID | None, Header(alias="X-Workspace-ID")] = None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    if organization_id is None or workspace_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant scope required")
    token_hash = sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
    statement = (
        select(User.id, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .join(
            Workspace,
            (Workspace.id == Membership.workspace_id)
            & (Workspace.organization_id == Membership.organization_id),
        )
        .where(
            User.api_token_hash == token_hash,
            User.organization_id == organization_id,
            Membership.organization_id == organization_id,
            Membership.workspace_id == workspace_id,
        )
    )
    row = session.execute(statement).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace access denied")
    return Principal(
        user_id=row.id,
        role=row.role,
        scope=TenantScope(organization_id=organization_id, workspace_id=workspace_id),
    )


def get_tenant_scope(principal: Annotated[Principal, Depends(get_principal)]) -> TenantScope:
    return principal.scope


def require_writer(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if principal.role not in {"owner", "admin", "editor"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Write access denied")
    return principal
