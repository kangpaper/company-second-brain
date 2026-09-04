from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.ai.orchestrator import (
    MAX_CITATIONS,
    AIProvider,
    GroundingError,
    ProviderDraft,
    validate_provider_draft,
)
from company_brain.api.context import ContextBuildRequest, build_context_response
from company_brain.api.dependencies import Principal, get_principal, get_tenant_scope
from company_brain.db.session import get_session
from company_brain.domain.models import ReasoningRun
from company_brain.domain.repositories import TenantScope

router = APIRouter(tags=["ai-orchestrator"])
PROMPT_VERSION = "customer_360-grounded-v1"


class DeterministicGroundedProvider:
    provider_name = "deterministic-local"
    model_name = "context-summary-v1"

    def generate(self, *, question: str, context: dict[str, Any]) -> ProviderDraft:
        del question
        customer = context["customer"]
        metrics = context["metrics"]
        evidence = context["evidence"]
        data_gaps = context["data_gaps"]
        revenue_values = metrics.get("revenue_total", {}).get("values", [])
        revenue_text = ", ".join(
            f"{item['value']} {item['currency']}" for item in revenue_values
        ) or "not available"
        citation_ids = [UUID(item["id"]) for item in evidence[:MAX_CITATIONS]]
        uncertainty = (
            "Data gaps: " + ", ".join(data_gaps)
            if data_gaps
            else "No material data gaps were reported by the context engine."
        )
        return ProviderDraft(
            answer=f"{customer['name']} has evidenced revenue of {revenue_text}.",
            citation_ids=citation_ids,
            uncertainty=uncertainty,
        )


ProviderFactory = Callable[[], AIProvider]


def get_ai_provider() -> ProviderFactory:
    return DeterministicGroundedProvider


def _provider_identity(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("provider metadata is missing or invalid")
    return value.strip()[:limit]


class AIAskRequest(ContextBuildRequest):
    pass


class AIAskResponse(BaseModel):
    reasoning_run_id: UUID
    context_hash: str
    answer: str
    citation_ids: list[UUID]
    uncertainty: str
    metrics: dict[str, Any]
    signals: list[dict[str, Any]]


class ReasoningRunResponse(BaseModel):
    id: UUID
    customer_id: UUID
    context_hash: str
    provider: str
    model: str
    prompt_version: str
    status: str
    answer: str | None
    citation_ids: list[UUID]
    uncertainty: str | None
    error_code: str | None
    error_message: str | None


def _run_response(run: ReasoningRun) -> ReasoningRunResponse:
    return ReasoningRunResponse(
        id=run.id,
        customer_id=run.customer_id,
        context_hash=run.context_hash,
        provider=run.provider,
        model=run.model,
        prompt_version=run.prompt_version,
        status=run.status,
        answer=run.answer,
        citation_ids=[UUID(item) for item in run.citation_ids],
        uncertainty=run.uncertainty,
        error_code=run.error_code,
        error_message=run.error_message,
    )


@router.post("/api/v1/ai/ask", response_model=AIAskResponse)
def ask_ai(
    payload: AIAskRequest,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(get_principal)],
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
    provider_factory: Annotated[ProviderFactory, Depends(get_ai_provider)],
) -> AIAskResponse:
    context_response = build_context_response(
        ContextBuildRequest(**payload.model_dump()), session, scope
    )
    context_payload = context_response.context.model_dump(mode="json")
    if not context_response.context.evidence:
        error_message = "Insufficient evidence for grounded answer"
        run = ReasoningRun(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            actor_user_id=principal.user_id,
            customer_id=context_response.entity.id,
            context_hash=context_response.context_hash,
            provider="not-invoked",
            model="not-invoked",
            prompt_version=PROMPT_VERSION,
            status="failed",
            answer=None,
            citation_ids=[],
            uncertainty=None,
            error_code="insufficient_evidence",
            error_message=error_message,
        )
        session.add(run)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error_message,
        )
    provider_name = "unknown"
    model_name = "unknown"
    try:
        provider = provider_factory()
        candidate_provider_name = _provider_identity(provider.provider_name, limit=100)
        candidate_model_name = _provider_identity(provider.model_name, limit=255)
        provider_name, model_name = candidate_provider_name, candidate_model_name
        draft = provider.generate(question=payload.question, context=context_payload)
        validated = validate_provider_draft(
            draft,
            evidence_ids={item.id for item in context_response.context.evidence},
        )
    except Exception as error:
        grounding_failure = isinstance(error, GroundingError)
        error_code = "invalid_grounding" if grounding_failure else "provider_failure"
        error_message = (
            "AI provider returned an invalid grounded answer"
            if grounding_failure
            else "AI provider failed"
        )
        run = ReasoningRun(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            actor_user_id=principal.user_id,
            customer_id=context_response.entity.id,
            context_hash=context_response.context_hash,
            provider=provider_name,
            model=model_name,
            prompt_version=PROMPT_VERSION,
            status="failed",
            answer=None,
            citation_ids=[],
            uncertainty=None,
            error_code=error_code,
            error_message=error_message,
        )
        session.add(run)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_message,
        ) from None
    run = ReasoningRun(
        organization_id=scope.organization_id,
        workspace_id=scope.workspace_id,
        actor_user_id=principal.user_id,
        customer_id=context_response.entity.id,
        context_hash=context_response.context_hash,
        provider=provider_name,
        model=model_name,
        prompt_version=PROMPT_VERSION,
        status="succeeded",
        answer=validated.answer,
        citation_ids=[str(item) for item in validated.citation_ids],
        uncertainty=validated.uncertainty,
        error_code=None,
        error_message=None,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return AIAskResponse(
        reasoning_run_id=run.id,
        context_hash=context_response.context_hash,
        answer=validated.answer,
        citation_ids=validated.citation_ids,
        uncertainty=validated.uncertainty,
        metrics=context_response.context.metrics,
        signals=context_response.context.signals,
    )


@router.get(
    "/api/v1/reasoning-runs/{run_id}", response_model=ReasoningRunResponse
)
def get_reasoning_run(
    run_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    scope: Annotated[TenantScope, Depends(get_tenant_scope)],
) -> ReasoningRunResponse:
    run = session.scalar(
        select(ReasoningRun).where(
            ReasoningRun.id == run_id,
            ReasoningRun.organization_id == scope.organization_id,
            ReasoningRun.workspace_id == scope.workspace_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reasoning run not found")
    return _run_response(run)
