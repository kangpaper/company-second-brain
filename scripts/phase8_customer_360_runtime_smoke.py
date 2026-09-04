import json
import os
import uuid
from datetime import UTC, datetime
from hashlib import sha256

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityType,
    Event,
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
    api_url = os.environ.get("PHASE8_API_URL", "http://127.0.0.1:8023")
    suffix = uuid.uuid4().hex[:10]
    token = f"phase8-runtime-{suffix}"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            organization = Organization(name="Phase 8 Runtime", slug=f"phase8-{suffix}")
            session.add(organization)
            session.flush()
            workspace = Workspace(
                organization_id=organization.id, name="Runtime", slug=f"runtime-{suffix}"
            )
            user = User(
                organization_id=organization.id,
                email=f"phase8-{suffix}@example.com",
                display_name="Phase 8 Runtime",
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
                name="SO-RUNTIME",
                normalized_name="so-runtime",
                metadata_={
                    "state": "sale",
                    "amount_total": 250.0,
                    "currency": "USD",
                    "date_order": "2026-07-01T00:00:00Z",
                },
            )
            source = Source(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_type="runtime",
                uri="runtime://phase8",
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
            event = Event(
                organization_id=organization.id,
                workspace_id=workspace.id,
                subject_entity_id=customer.id,
                event_type="customer_contacted",
                occurred_at=datetime(2026, 7, 15, tzinfo=UTC),
                payload={"channel": "email"},
            )
            evidence = Evidence(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_id=source.id,
                evidence_type="field",
                pointer={"field": "amount_total"},
                quote="250 USD",
            )
            session.add_all([relationship, event, evidence])
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
        params = {"as_of": "2026-08-14T00:00:00Z"}
        with httpx.Client(base_url=api_url, timeout=10) as client:
            health = client.get("/health")
            health.raise_for_status()
            customer_360 = client.get(
                f"/api/v1/customers/{customer_id}/360", headers=headers, params=params
            )
            customer_360.raise_for_status()
            body = customer_360.json()
            assert body["customer"]["name"] == "Runtime Customer"
            assert [item["name"] for item in body["orders"]] == ["SO-RUNTIME"]
            assert body["metrics"]["revenue_total"]["values"] == [
                {"currency": "USD", "value": 250.0}
            ]
            assert body["metrics"]["revenue_total"]["evidence_ids"] == [
                str(evidence_id)
            ]
            assert [item["event_type"] for item in body["timeline"]] == [
                "customer_contacted"
            ]

            metrics = client.get(
                f"/api/v1/customers/{customer_id}/metrics",
                headers=headers,
                params={**params, "window": "6m"},
            )
            metrics.raise_for_status()
            assert metrics.json()["window"] == "6m"
            risk = client.get(
                f"/api/v1/customers/{customer_id}/risk", headers=headers, params=params
            )
            risk.raise_for_status()
            assert isinstance(risk.json()["signals"], list)

        print(
            json.dumps(
                {
                    "health": "ok",
                    "customer_360": "ok",
                    "revenue_total": "250.0 USD",
                    "evidence": "present",
                    "timeline": "bounded_as_of",
                    "metrics_view": "ok",
                    "risk_view": "ok",
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
