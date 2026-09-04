from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from company_brain.api.customer_360 import (
    Customer360Response,
    build_customer_360_response,
)
from company_brain.api.dependencies import get_tenant_scope
from company_brain.context_engine.service import (
    detect_intent,
    is_potential_customer_status_question,
)
from company_brain.db.session import get_session
from company_brain.domain.repositories import TenantScope

router = APIRouter(prefix="/api/v1/context", tags=["context"])


class ContextBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    customer_id: UUID
    as_of: datetime


class ContextEntityReference(BaseModel):
    id: UUID
    type: Literal["customer"]
    name: str


class ContextBuildResponse(BaseModel):
    schema_version: Literal["customer_360.v1"]
    intent: Literal["CUSTOMER_360"]
    entity: ContextEntityReference
    as_of: datetime
    context_hash: str
    context: Customer360Response


def _canonical_context_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


def build_context_response(
    payload: ContextBuildRequest,
    session: Session,
    scope: TenantScope,
) -> ContextBuildResponse:
    if not is_potential_customer_status_question(payload.question):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported context intent",
        )
    context, normalized_as_of = build_customer_360_response(
        payload.customer_id, session, scope, payload.as_of
    )
    intent = detect_intent(
        payload.question,
        customer_labels=(context.customer.name, *context.customer.aliases),
    )
    if intent != "CUSTOMER_360":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported context intent",
        )
    entity = ContextEntityReference(
        id=context.customer.id,
        type="customer",
        name=context.customer.name,
    )
    hash_payload = {
        "schema_version": "customer_360.v1",
        "intent": intent,
        "entity": entity.model_dump(mode="json"),
        "as_of": normalized_as_of.isoformat(),
        "context": context.model_dump(mode="json"),
    }
    return ContextBuildResponse(
        schema_version="customer_360.v1",
        intent=intent,
        entity=entity,
        as_of=normalized_as_of,
        context_hash=_canonical_context_hash(hash_payload),
        context=context,
    )


@router.post("/build", response_model=ContextBuildResponse)
def build_context(
    payload: ContextBuildRequest,
    session: Annotated[Session, Depends(get_session)],
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
) -> ContextBuildResponse:
    return build_context_response(payload, session, scope)
