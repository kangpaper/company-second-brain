import json
import os
import uuid
from datetime import UTC, datetime
from hashlib import sha256

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

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


def add_scope(session: Session, *, suffix: str, token: str) -> tuple[Organization, Workspace]:
    organization = Organization(name=f"Phase 10 {suffix}", slug=f"phase10-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Runtime",
        slug=f"runtime-{suffix}",
    )
    user = User(
        organization_id=organization.id,
        email=f"phase10-{suffix}@example.com",
        display_name="Phase 10 Runtime",
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
    session.flush()
    return organization, workspace


def headers(token: str, organization: Organization, workspace: Workspace) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL or DATABASE_URL is required")
    api_url = os.environ.get("PHASE10_API_URL", "http://127.0.0.1:8025")
    suffix = uuid.uuid4().hex[:10]
    token = f"phase10-runtime-{suffix}"
    other_token = f"phase10-other-{suffix}"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            organization, workspace = add_scope(
                session, suffix=suffix, token=token
            )
            other_organization, other_workspace = add_scope(
                session, suffix=f"other-{suffix}", token=other_token
            )
            customer = Entity(
                organization_id=organization.id,
                workspace_id=workspace.id,
                entity_type=EntityType.CUSTOMER,
                name="Runtime Customer",
                normalized_name="runtime customer",
            )
            order = Entity(
                organization_id=organization.id,
                workspace_id=workspace.id,
                entity_type=EntityType.ORDER,
                name="SO-AI",
                normalized_name="so ai",
                metadata_={
                    "state": "sale",
                    "amount_total": 275.0,
                    "currency": "USD",
                    "date_order": datetime.now(UTC).isoformat(),
                },
            )
            source = Source(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_type="runtime",
                uri="runtime://phase10",
            )
            session.add_all([customer, order, source])
            session.flush()
            relationship = Relationship(
                organization_id=organization.id,
                workspace_id=workspace.id,
                from_entity_id=customer.id,
                to_entity_id=order.id,
                relationship_type="CUSTOMER_HAS_ORDER",
            )
            evidence = Evidence(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_id=source.id,
                evidence_type="field",
                pointer={"field": "amount_total"},
                quote="275 USD",
            )
            session.add_all([relationship, evidence])
            session.flush()
            session.add(
                EvidenceLink(
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    evidence_id=evidence.id,
                    entity_id=order.id,
                )
            )
            session.commit()
            customer_id = customer.id
            evidence_id = evidence.id
            primary_headers = headers(token, organization, workspace)
            other_headers = headers(other_token, other_organization, other_workspace)

        as_of = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with httpx.Client(base_url=api_url, timeout=10) as client:
            health = client.get("/health")
            health.raise_for_status()
            ask = client.post(
                "/api/v1/ai/ask",
                headers=primary_headers,
                json={
                    "question": "Tình hình khách hàng hiện tại thế nào?",
                    "customer_id": str(customer_id),
                    "as_of": as_of,
                },
            )
            ask.raise_for_status()
            body = ask.json()
            assert len(body["context_hash"]) == 64
            assert body["citation_ids"] == [str(evidence_id)]
            assert body["metrics"]["revenue_total"]["values"] == [
                {"currency": "USD", "value": 275.0}
            ]
            assert body["metrics"]["revenue_total"]["evidence_ids"] == [
                str(evidence_id)
            ]
            assert body["uncertainty"]
            run_id = body["reasoning_run_id"]
            readback = client.get(
                f"/api/v1/reasoning-runs/{run_id}", headers=primary_headers
            )
            readback.raise_for_status()
            run_body = readback.json()
            assert run_body["status"] == "succeeded"
            assert run_body["context_hash"] == body["context_hash"]
            assert run_body["citation_ids"] == body["citation_ids"]
            cross_tenant = client.get(
                f"/api/v1/reasoning-runs/{run_id}", headers=other_headers
            )
            assert cross_tenant.status_code == 404

            failure = client.post(
                "/api/v1/ai/ask",
                headers=primary_headers,
                json={
                    "question": "Tổng quan khách hàng",
                    "customer_id": str(customer_id),
                    "as_of": as_of,
                },
            )
            assert failure.status_code == 502, (
                failure.status_code,
                failure.text,
            )
            assert failure.json() == {"detail": "AI provider failed"}
            assert "runtime-provider-secret-must-not-leak" not in failure.text
            with Session(engine) as session:
                runs = session.scalars(
                    select(ReasoningRun).where(
                        ReasoningRun.organization_id == organization.id,
                        ReasoningRun.workspace_id == workspace.id,
                    )
                ).all()
                assert len(runs) == 2
                failed_runs = [run for run in runs if run.status == "failed"]
                assert len(failed_runs) == 1
                failed_run = failed_runs[0]
                assert failed_run.error_code == "provider_failure"
                assert failed_run.error_message == "AI provider failed"
                assert "runtime-provider-secret-must-not-leak" not in (
                    failed_run.error_message or ""
                )
                failed_run_id = str(failed_run.id)
            failed_readback = client.get(
                f"/api/v1/reasoning-runs/{failed_run_id}", headers=primary_headers
            )
            failed_readback.raise_for_status()
            assert failed_readback.json()["status"] == "failed"
            assert failed_readback.json()["error_code"] == "provider_failure"
            failed_cross_tenant = client.get(
                f"/api/v1/reasoning-runs/{failed_run_id}", headers=other_headers
            )
            assert failed_cross_tenant.status_code == 404

        with Session(engine) as session:
            persisted = session.scalar(
                select(ReasoningRun).where(ReasoningRun.id == uuid.UUID(run_id))
            )
            assert persisted is not None
            assert persisted.status == "succeeded"
            assert persisted.citation_ids == [str(evidence_id)]
            assert persisted.context_hash == body["context_hash"]

        print(
            json.dumps(
                {
                    "health": "ok",
                    "ai_ask": "ok",
                    "deterministic_metrics": "preserved",
                    "citation": "tenant_evidence_validated",
                    "uncertainty": "present",
                    "reasoning_run": "persisted_and_readable",
                    "provider_failure": "sanitized_audited_and_readable",
                    "failure_secret_leak": "none",
                    "cross_tenant_readback": "404",
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
