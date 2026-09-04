from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.ai.orchestrator import AIProvider, ProviderDraft
from company_brain.api.ai import get_ai_provider
from company_brain.db.session import get_session
from company_brain.domain.models import (
    Entity,
    EntityType,
    Evidence,
    EvidenceLink,
    Membership,
    Organization,
    ReasoningRun,
    Relationship,
    Source,
    User,
    Workspace,
)
from company_brain.main import app


class StubProvider:
    provider_name = "stub"
    model_name = "stub-v1"

    def __init__(self, draft: ProviderDraft) -> None:
        self.draft = draft
        self.context: dict[str, object] | None = None

    def generate(self, *, question: str, context: dict[str, object]) -> ProviderDraft:
        self.context = context
        return self.draft


class ExplodingProviderFactory:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> AIProvider:
        self.called = True
        raise RuntimeError("factory-secret-details")


class BlankMetadataProvider:
    provider_name = "   "
    model_name = ""

    def generate(self, *, question: str, context: dict[str, object]) -> ProviderDraft:
        del question, context
        raise AssertionError("generate must not run with invalid provider metadata")


class NulMetadataProvider:
    provider_name = "provider\x00secret"
    model_name = "model-v1"

    def generate(self, *, question: str, context: dict[str, object]) -> ProviderDraft:
        del question, context
        raise AssertionError("generate must not run with invalid provider metadata")


class MetadataExplodingProvider:
    @property
    def provider_name(self) -> str:
        raise RuntimeError("metadata-secret-details")

    @property
    def model_name(self) -> str:
        raise RuntimeError("metadata-secret-details")

    def generate(self, *, question: str, context: dict[str, object]) -> ProviderDraft:
        del question, context
        raise AssertionError("generate must not be reached")


class ExplodingProvider:
    provider_name = "stub"
    model_name = "stub-v1"

    def generate(self, *, question: str, context: dict[str, object]) -> ProviderDraft:
        del question, context
        raise RuntimeError("provider-secret-details")


@pytest.fixture
async def client(session: Session) -> AsyncIterator[httpx.AsyncClient]:
    def override_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api_client:
        yield api_client
    app.dependency_overrides.clear()


def seed_scope(session: Session) -> tuple[dict[str, str], Entity, Evidence]:
    organization = Organization(name="AI Org", slug=f"ai-{uuid4().hex}")
    session.add(organization)
    session.flush()
    workspace = Workspace(organization_id=organization.id, name="Main", slug="main")
    token = f"ai-token-{uuid4().hex}"
    user = User(
        organization_id=organization.id,
        email=f"{uuid4().hex}@example.com",
        display_name="AI User",
        api_token_hash=sha256(token.encode()).hexdigest(),
    )
    session.add_all([workspace, user])
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role="member",
        )
    )
    customer = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="ABC Limited",
        normalized_name="abc limited",
        aliases=["ABC"],
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    order = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.ORDER,
        name="SO-AI",
        normalized_name="so ai",
        metadata_={
            "state": "sale",
            "amount_total": 900.0,
            "currency": "USD",
            "date_order": "2026-08-01T00:00:00Z",
        },
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="fixture",
        uri="fixture://phase10",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([customer, order, source])
    session.flush()
    relationship = Relationship(
        organization_id=organization.id,
        workspace_id=workspace.id,
        from_entity_id=customer.id,
        to_entity_id=order.id,
        relationship_type="CUSTOMER_HAS_ORDER",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    evidence = Evidence(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        evidence_type="field",
        pointer={"field": "amount_total"},
        quote="900 USD",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add_all([relationship, evidence])
    session.flush()
    session.add(
        EvidenceLink(
            organization_id=organization.id,
            workspace_id=workspace.id,
            evidence_id=evidence.id,
            entity_id=order.id,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    session.commit()
    return (
        {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization.id),
            "X-Workspace-ID": str(workspace.id),
        },
        customer,
        evidence,
    )


@pytest.mark.asyncio
async def test_ai_ask_grounds_provider_answer_and_audits_reasoning_run(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, evidence = seed_scope(session)
    provider = StubProvider(
        ProviderDraft(
            answer="ABC has 900 USD in evidenced revenue.",
            citation_ids=[evidence.id],
            uncertainty="Activity history is incomplete.",
        )
    )
    app.dependency_overrides[get_ai_provider] = lambda: lambda: provider

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "ABC has 900 USD in evidenced revenue."
    assert body["citation_ids"] == [str(evidence.id)]
    assert body["uncertainty"] == "Activity history is incomplete."
    assert body["metrics"]["revenue_total"]["values"] == [
        {"currency": "USD", "value": 900.0}
    ]
    assert provider.context is not None
    assert provider.context["metrics"] == body["metrics"]
    run = session.get(ReasoningRun, UUID(body["reasoning_run_id"]))
    assert run is not None
    assert run.status == "succeeded"
    assert run.context_hash == body["context_hash"]
    assert run.citation_ids == [str(evidence.id)]
    assert run.provider == "stub"
    assert run.model == "stub-v1"


@pytest.mark.asyncio
async def test_ai_ask_rejects_ungrounded_provider_output_and_audits_generic_failure(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, _ = seed_scope(session)
    provider = StubProvider(
        ProviderDraft(
            answer="Secret provider text must not leak.",
            citation_ids=[uuid4()],
            uncertainty="None.",
        )
    )
    app.dependency_overrides[get_ai_provider] = lambda: lambda: provider

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "AI provider returned an invalid grounded answer"}
    run = session.scalars(select(ReasoningRun).order_by(ReasoningRun.created_at.desc())).first()
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "invalid_grounding"
    assert run.answer is None
    assert "Secret provider" not in (run.error_message or "")


@pytest.mark.asyncio
async def test_ai_ask_rejects_nul_provider_text_with_sanitized_audit(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, evidence = seed_scope(session)
    provider = StubProvider(
        ProviderDraft(
            answer="grounded\x00provider-secret",
            citation_ids=[evidence.id],
            uncertainty="Bounded.",
        )
    )
    app.dependency_overrides[get_ai_provider] = lambda: lambda: provider

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "AI provider returned an invalid grounded answer"}
    run = session.scalars(select(ReasoningRun)).one()
    assert run.status == "failed"
    assert run.error_code == "invalid_grounding"
    assert "provider-secret" not in (run.error_message or "")


@pytest.mark.asyncio
async def test_ai_ask_fails_closed_and_audits_when_context_has_no_evidence(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, evidence = seed_scope(session)
    link = session.scalar(select(EvidenceLink).where(EvidenceLink.evidence_id == evidence.id))
    assert link is not None
    session.delete(link)
    session.flush()
    session.delete(evidence)
    session.commit()

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Insufficient evidence for grounded answer"}
    runs = session.scalars(select(ReasoningRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "failed"
    assert run.provider == "not-invoked"
    assert run.model == "not-invoked"
    assert run.error_code == "insufficient_evidence"
    assert run.error_message == "Insufficient evidence for grounded answer"
    assert len(run.context_hash) == 64


@pytest.mark.asyncio
async def test_ai_ask_sanitizes_provider_exception_and_audits_failure(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, _ = seed_scope(session)
    app.dependency_overrides[get_ai_provider] = lambda: ExplodingProvider

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "AI provider failed"}
    run = session.scalars(select(ReasoningRun).order_by(ReasoningRun.created_at.desc())).first()
    assert run is not None
    assert run.status == "failed"
    assert run.error_code == "provider_failure"
    assert run.error_message == "AI provider failed"


@pytest.mark.asyncio
async def test_ai_ask_sanitizes_provider_construction_failure_and_audits_it(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, _ = seed_scope(session)
    factory = ExplodingProviderFactory()
    app.dependency_overrides[get_ai_provider] = lambda: factory

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert factory.called is True
    assert response.status_code == 502
    assert response.json() == {"detail": "AI provider failed"}
    runs = session.scalars(select(ReasoningRun)).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].provider == "unknown"
    assert "factory-secret-details" not in (runs[0].error_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [BlankMetadataProvider, NulMetadataProvider])
async def test_ai_ask_sanitizes_invalid_provider_metadata_and_audits_it(
    client: httpx.AsyncClient,
    session: Session,
    provider_type: type[AIProvider],
) -> None:
    headers, customer, _ = seed_scope(session)
    app.dependency_overrides[get_ai_provider] = lambda: provider_type

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "AI provider failed"}
    runs = session.scalars(select(ReasoningRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "failed"
    assert run.provider == "unknown"
    assert run.model == "unknown"
    assert run.error_code == "provider_failure"


@pytest.mark.asyncio
async def test_ai_ask_sanitizes_provider_metadata_failure_and_audits_it(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, _ = seed_scope(session)
    app.dependency_overrides[get_ai_provider] = lambda: MetadataExplodingProvider

    response = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "AI provider failed"}
    runs = session.scalars(select(ReasoningRun)).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error_code == "provider_failure"
    assert "metadata-secret-details" not in (runs[0].error_message or "")


@pytest.mark.asyncio
async def test_reasoning_run_read_is_tenant_scoped(
    client: httpx.AsyncClient, session: Session
) -> None:
    headers, customer, evidence = seed_scope(session)
    provider = StubProvider(
        ProviderDraft(
            answer="Grounded.",
            citation_ids=[evidence.id],
            uncertainty="Some uncertainty.",
        )
    )
    app.dependency_overrides[get_ai_provider] = lambda: lambda: provider
    created = await client.post(
        "/api/v1/ai/ask",
        headers=headers,
        json={
            "question": "Tình hình ABC thế nào?",
            "customer_id": str(customer.id),
            "as_of": "2026-08-14T00:00:00Z",
        },
    )
    other_headers, _, _ = seed_scope(session)

    own = await client.get(
        f"/api/v1/reasoning-runs/{created.json()['reasoning_run_id']}", headers=headers
    )
    foreign = await client.get(
        f"/api/v1/reasoning-runs/{created.json()['reasoning_run_id']}",
        headers=other_headers,
    )

    assert own.status_code == 200
    assert own.json()["status"] == "succeeded"
    assert foreign.status_code == 404
