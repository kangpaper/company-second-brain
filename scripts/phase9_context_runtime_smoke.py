import json
import os
import uuid
from hashlib import sha256

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityType,
    Evidence,
    EvidenceLink,
    Membership,
    Organization,
    Relationship,
    Source,
    User,
    Workspace,
)


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL or DATABASE_URL is required")
    api_url = os.environ.get("PHASE9_API_URL", "http://127.0.0.1:8024")
    suffix = uuid.uuid4().hex[:10]
    token = f"phase9-runtime-{suffix}"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            organization = Organization(name="Phase 9 Runtime", slug=f"phase9-{suffix}")
            session.add(organization)
            session.flush()
            workspace = Workspace(
                organization_id=organization.id, name="Runtime", slug=f"runtime-{suffix}"
            )
            user = User(
                organization_id=organization.id,
                email=f"phase9-{suffix}@example.com",
                display_name="Phase 9 Runtime",
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
                name="Runtime Customer",
                normalized_name="runtime customer",
            )
            order = Entity(
                organization_id=organization.id,
                workspace_id=workspace.id,
                entity_type=EntityType.ORDER,
                name="SO-CONTEXT",
                normalized_name="so context",
                metadata_={
                    "state": "sale",
                    "amount_total": 275.0,
                    "currency": "USD",
                    "date_order": "2026-07-01T00:00:00Z",
                },
            )
            source = Source(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_type="runtime",
                uri="runtime://phase9",
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
            organization_id = organization.id
            workspace_id = workspace.id
            customer_id = customer.id
            evidence_id = evidence.id

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization_id),
            "X-Workspace-ID": str(workspace_id),
        }
        first_request = {
            "question": "Tình hình khách hàng hiện tại thế nào?",
            "customer_id": str(customer_id),
            "as_of": "2026-08-14T00:00:00Z",
        }
        second_request = {
            **first_request,
            "question": "Give me the current customer overview",
        }
        with httpx.Client(base_url=api_url, timeout=10) as client:
            health = client.get("/health")
            health.raise_for_status()
            first = client.post("/api/v1/context/build", headers=headers, json=first_request)
            first.raise_for_status()
            second = client.post("/api/v1/context/build", headers=headers, json=second_request)
            second.raise_for_status()
            body = first.json()
            assert body["schema_version"] == "customer_360.v1"
            assert body["intent"] == "CUSTOMER_360"
            assert body["entity"]["id"] == str(customer_id)
            assert body["context"]["metrics"]["revenue_total"]["values"] == [
                {"currency": "USD", "value": 275.0}
            ]
            assert body["context"]["metrics"]["revenue_total"]["evidence_ids"] == [
                str(evidence_id)
            ]
            assert len(body["context_hash"]) == 64
            assert body["context_hash"] == second.json()["context_hash"]
            assert body["context"] == second.json()["context"]
            assert "missing_activity_history" in body["context"]["data_gaps"]

        print(
            json.dumps(
                {
                    "health": "ok",
                    "context_build": "ok",
                    "intent": "CUSTOMER_360",
                    "schema_version": "customer_360.v1",
                    "deterministic_hash": "stable_across_paraphrases",
                    "revenue_total": "275.0 USD",
                    "evidence": "present",
                    "data_gaps": "present",
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
