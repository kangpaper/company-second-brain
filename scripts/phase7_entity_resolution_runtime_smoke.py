import json
import os
import uuid
from hashlib import sha256

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityResolutionAudit,
    EntityType,
    Membership,
    Organization,
    User,
    Workspace,
)


def main() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("TEST_DATABASE_URL or DATABASE_URL is required")
    api_url = os.environ.get("PHASE7_API_URL", "http://127.0.0.1:8022")
    suffix = uuid.uuid4().hex[:10]
    token = f"phase7-runtime-{suffix}"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            organization = Organization(name="Phase 7 Runtime", slug=f"phase7-runtime-{suffix}")
            session.add(organization)
            session.flush()
            organization_id = organization.id
            workspace = Workspace(
                organization_id=organization.id,
                name="Runtime",
                slug=f"runtime-{suffix}",
            )
            user = User(
                organization_id=organization.id,
                email=f"phase7-runtime-{suffix}@example.com",
                display_name="Phase 7 Runtime",
                api_token_hash=sha256(token.encode()).hexdigest(),
            )
            session.add_all([workspace, user])
            session.flush()
            session.add(
                Membership(
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="owner",
                )
            )
            entities = [
                Entity(
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    entity_type=EntityType.CUSTOMER,
                    name=name,
                    normalized_name=name.casefold(),
                )
                for name in (
                    "Acme Corporation",
                    "Acme Company",
                    "Merge Source",
                    "Merge Target",
                )
            ]
            session.add_all(entities)
            session.commit()
            workspace_id = workspace.id
            merge_source_id = entities[2].id
            merge_target_id = entities[3].id

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization_id),
            "X-Workspace-ID": str(workspace_id),
        }
        with httpx.Client(base_url=api_url, timeout=10) as client:
            health = client.get("/health")
            health.raise_for_status()
            resolution = client.post(
                "/api/v1/entity-resolution/resolve",
                headers=headers,
                json={"entity_type": "customer", "name": "Acme Co"},
            )
            assert resolution.status_code == 202, resolution.text
            resolution_body = resolution.json()
            assert resolution_body["outcome"] == "review_required"
            assert resolution_body["candidates"]
            case_id = resolution_body["case_id"]
            candidate_id = resolution_body["candidates"][0]["entity_id"]
            decision = client.post(
                f"/api/v1/entity-resolution/cases/{case_id}/decision",
                headers=headers,
                json={"action": "match", "entity_id": candidate_id},
            )
            decision.raise_for_status()
            assert decision.json()["status"] == "resolved"

            merged = client.post(
                "/api/v1/entity-resolution/merge",
                headers=headers,
                json={
                    "source_entity_id": str(merge_source_id),
                    "target_entity_id": str(merge_target_id),
                },
            )
            merged.raise_for_status()
            merge_id = merged.json()["merge_id"]
            split = client.post(
                f"/api/v1/entity-resolution/merges/{merge_id}/split",
                headers=headers,
            )
            split.raise_for_status()
            assert split.json()["status"] == "split"

        with Session(engine) as session:
            source = session.get(Entity, merge_source_id)
            assert source is not None and source.lifecycle_status == "active"
            actions = list(
                session.scalars(
                    select(EntityResolutionAudit.action).where(
                        EntityResolutionAudit.organization_id == organization_id
                    )
                )
            )
            assert actions == ["match", "merge", "split"]
        print(
            json.dumps(
                {
                    "health": "ok",
                    "ambiguous_resolution": "review_required",
                    "case_decision": "resolved",
                    "merge_split_round_trip": "split",
                    "audit_actions": actions,
                }
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
