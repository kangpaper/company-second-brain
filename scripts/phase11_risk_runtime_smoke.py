import json
import os
import uuid
from datetime import UTC, datetime
from hashlib import sha256

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
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

FIXTURE_CREATED_AT = datetime(2026, 8, 1, tzinfo=UTC)
AS_OF = "2026-08-14T00:00:00Z"


def _create_scope(session: Session, suffix: str) -> tuple[dict[str, str], Workspace]:
    organization = Organization(name=f"Phase 11 {suffix}", slug=f"p11-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Runtime",
        slug=f"runtime-{suffix}",
    )
    token = f"phase11-runtime-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"phase11-{suffix}@example.com",
        display_name="Phase 11 Runtime",
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
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }
    return headers, workspace


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL or DATABASE_URL is required")
    api_url = os.environ.get("PHASE11_API_URL", "http://127.0.0.1:8026")
    suffix = uuid.uuid4().hex[:10]
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            headers, workspace = _create_scope(session, suffix)
            other_headers, other_workspace = _create_scope(session, f"other-{suffix}")
            customer = Entity(
                organization_id=workspace.organization_id,
                workspace_id=workspace.id,
                entity_type=EntityType.CUSTOMER,
                name="Phase 11 Runtime Customer",
                normalized_name="phase 11 runtime customer",
                created_at=FIXTURE_CREATED_AT,
            )
            deleted_customer = Entity(
                organization_id=workspace.organization_id,
                workspace_id=workspace.id,
                entity_type=EntityType.CUSTOMER,
                name="Phase 11 Deleted Later",
                normalized_name="phase 11 deleted later",
                created_at=FIXTURE_CREATED_AT,
            )
            transferred_customer = Entity(
                organization_id=workspace.organization_id,
                workspace_id=workspace.id,
                entity_type=EntityType.CUSTOMER,
                name="Phase 11 Transferred Later",
                normalized_name="phase 11 transferred later",
                created_at=FIXTURE_CREATED_AT,
            )
            source = Source(
                organization_id=workspace.organization_id,
                workspace_id=workspace.id,
                source_type="runtime",
                uri="runtime://phase11",
                created_at=FIXTURE_CREATED_AT,
            )
            session.add_all(
                [customer, deleted_customer, transferred_customer, source]
            )
            session.flush()
            ticket_specs = [
                ("2026-06-20T00:00:00Z", None),
                ("2026-07-20T00:00:00Z", "delivery"),
                ("2026-07-25T00:00:00Z", "delivery_complaint"),
                ("2026-08-01T00:00:00Z", None),
                ("2026-08-10T00:00:00Z", None),
            ]
            evidence_ids: list[str] = []
            ticket_ids: list[uuid.UUID] = []
            for index, (opened_at, complaint_type) in enumerate(ticket_specs):
                attributes: dict[str, str] = {"opened_at": opened_at}
                if complaint_type is not None:
                    attributes["complaint_type"] = complaint_type
                ticket = Entity(
                    organization_id=workspace.organization_id,
                    workspace_id=workspace.id,
                    entity_type=EntityType.TICKET,
                    name=f"Runtime Ticket {index}",
                    normalized_name=f"runtime ticket {index}",
                    metadata_=attributes,
                    created_at=FIXTURE_CREATED_AT,
                )
                session.add(ticket)
                session.flush()
                ticket_ids.append(ticket.id)
                relationship = Relationship(
                    organization_id=workspace.organization_id,
                    workspace_id=workspace.id,
                    from_entity_id=customer.id,
                    to_entity_id=ticket.id,
                    relationship_type="CUSTOMER_HAS_TICKET",
                    created_at=FIXTURE_CREATED_AT,
                )
                evidence = Evidence(
                    organization_id=workspace.organization_id,
                    workspace_id=workspace.id,
                    source_id=source.id,
                    evidence_type="record",
                    pointer={"ticket": index},
                    created_at=FIXTURE_CREATED_AT,
                )
                session.add_all([relationship, evidence])
                session.flush()
                evidence_ids.append(str(evidence.id))
                session.add(
                    EvidenceLink(
                        organization_id=workspace.organization_id,
                        workspace_id=workspace.id,
                        evidence_id=evidence.id,
                        entity_id=ticket.id,
                        created_at=FIXTURE_CREATED_AT,
                    )
                )
            session.commit()
            customer_id = customer.id
            deleted_customer_id = deleted_customer.id
            transferred_customer_id = transferred_customer.id
            other_organization_id = other_workspace.organization_id
            other_workspace_id = other_workspace.id

        path = f"/api/v1/customers/{customer_id}/risk-assessment"
        with httpx.Client(base_url=api_url, timeout=10) as client:
            health = client.get("/health")
            health.raise_for_status()
            response = client.get(path, headers=headers, params={"as_of": AS_OF})
            response.raise_for_status()
            body = response.json()
            assert body["calculation_version"] == "customer-risk.v1"
            assert body["score"] == 45
            assert body["severity"] == "moderate"
            assert [signal["type"] for signal in body["signals"]] == [
                "DELIVERY_COMPLAINTS",
                "TICKET_INCREASE",
            ]
            cited = {
                evidence_id
                for signal in body["signals"]
                for evidence_id in signal["evidence_ids"]
            }
            assert cited == set(evidence_ids)

            deleted_path = (
                f"/api/v1/customers/{deleted_customer_id}/risk-assessment"
            )
            before_customer_delete = client.get(
                deleted_path, headers=headers, params={"as_of": AS_OF}
            )
            before_customer_delete.raise_for_status()
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM entities WHERE id = :customer_id"),
                    {"customer_id": deleted_customer_id},
                )
            historical_deleted_customer = client.get(
                deleted_path, headers=headers, params={"as_of": AS_OF}
            )
            historical_deleted_customer.raise_for_status()
            assert historical_deleted_customer.json() == before_customer_delete.json()
            assert (
                client.get(
                    deleted_path,
                    headers=headers,
                    params={"as_of": "2999-01-01T00:00:00Z"},
                ).status_code
                == 404
            )

            transferred_path = (
                f"/api/v1/customers/{transferred_customer_id}/risk-assessment"
            )
            before_scope_transfer = client.get(
                transferred_path, headers=headers, params={"as_of": AS_OF}
            )
            before_scope_transfer.raise_for_status()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE entities SET organization_id = :organization_id, "
                        "workspace_id = :workspace_id WHERE id = :customer_id"
                    ),
                    {
                        "organization_id": other_organization_id,
                        "workspace_id": other_workspace_id,
                        "customer_id": transferred_customer_id,
                    },
                )
            assert (
                client.get(
                    transferred_path,
                    headers=other_headers,
                    params={"as_of": AS_OF},
                ).status_code
                == 404
            )
            old_scope_historical = client.get(
                transferred_path, headers=headers, params={"as_of": AS_OF}
            )
            old_scope_historical.raise_for_status()
            assert old_scope_historical.json() == before_scope_transfer.json()
            assert (
                client.get(
                    transferred_path,
                    headers=headers,
                    params={"as_of": "2999-01-01T00:00:00Z"},
                ).status_code
                == 404
            )
            current_new_scope = client.get(
                transferred_path,
                headers=other_headers,
                params={"as_of": "2999-01-01T00:00:00Z"},
            )
            current_new_scope.raise_for_status()

            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE entities "
                        "SET metadata = (metadata::jsonb || "
                        "'{\"complaint_type\": \"delivery\"}'::jsonb)::json "
                        "WHERE id = :ticket_id"
                    ),
                    {"ticket_id": ticket_ids[0]},
                )
            repeated = client.get(path, headers=headers, params={"as_of": AS_OF})
            repeated.raise_for_status()
            assert repeated.json() == body

            forbidden_revision_mutations = (
                "UPDATE entity_revisions SET operation = 'update' "
                "WHERE revision_id = (SELECT revision_id FROM entity_revisions LIMIT 1)",
                "DELETE FROM entity_revisions WHERE revision_id = "
                "(SELECT revision_id FROM entity_revisions LIMIT 1)",
                "TRUNCATE entity_revisions",
            )
            for statement in forbidden_revision_mutations:
                try:
                    with engine.begin() as connection:
                        connection.execute(text(statement))
                except DBAPIError as error:
                    assert "append-only" in str(error)
                else:
                    raise AssertionError(
                        f"entity revision mutation was not rejected: {statement}"
                    )

            assert client.get(path, params={"as_of": AS_OF}).status_code == 401
            assert (
                client.get(
                    path,
                    headers=headers,
                    params={"as_of": "2026-08-14T00:00:00"},
                ).status_code
                == 422
            )
            assert (
                client.get(path, headers=other_headers, params={"as_of": AS_OF}).status_code
                == 404
            )
        print(
            json.dumps(
                {
                    "health": "ok",
                    "risk_score": 45,
                    "severity": "moderate",
                    "signals": ["DELIVERY_COMPLAINTS", "TICKET_INCREASE"],
                    "evidence": "complete",
                    "authentication": "enforced",
                    "timezone": "enforced",
                    "cross_tenant": "404",
                    "historical_replay": "stable_after_direct_entity_update",
                    "historical_delete_replay": "stable_before_delete_cutoff",
                    "historical_scope_transfer": "isolated_at_cutoff",
                    "entity_revisions": "append_only",
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
