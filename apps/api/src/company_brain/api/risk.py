from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from company_brain.api.customer_360 import build_customer_360_response
from company_brain.api.dependencies import get_tenant_scope
from company_brain.db.session import get_session
from company_brain.domain.repositories import TenantScope
from company_brain.risk_engine.service import RiskTicket, calculate_risk_assessment

router = APIRouter(prefix="/api/v1/customers", tags=["risk-insights"])


class CustomerRiskAssessmentResponse(BaseModel):
    customer_id: UUID
    as_of: datetime
    calculation_version: Literal["customer-risk.v1"]
    score: int = Field(ge=0, le=100)
    severity: Literal["low", "moderate", "high", "critical"]
    signals: list[dict[str, Any]]
    data_gaps: list[str]


@router.get(
    "/{customer_id}/risk-assessment",
    response_model=CustomerRiskAssessmentResponse,
)
def get_customer_risk_assessment(
    customer_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    as_of: Annotated[datetime, Query()],
) -> CustomerRiskAssessmentResponse:
    context, normalized_as_of = build_customer_360_response(
        customer_id, session, scope, as_of
    )
    assessment = calculate_risk_assessment(
        base_signals=[signal.copy() for signal in context.signals],
        tickets=[
            RiskTicket(
                id=ticket.id,
                attributes=ticket.attributes,
                evidence_ids=tuple(ticket.evidence_ids),
            )
            for ticket in context.tickets
        ],
        as_of=normalized_as_of,
    )
    return CustomerRiskAssessmentResponse(
        customer_id=customer_id,
        as_of=normalized_as_of,
        calculation_version=assessment.calculation_version,
        score=assessment.score,
        severity=assessment.severity,
        signals=assessment.signals,
        data_gaps=sorted(set([*context.data_gaps, *assessment.data_gaps])),
    )
