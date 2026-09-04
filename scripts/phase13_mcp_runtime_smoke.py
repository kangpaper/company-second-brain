import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    IngestionRun,
    IntegrationAudit,
    MCPConnection,
    MCPDiscoveredResource,
    MCPResourceCheckpoint,
    MCPScheduleTick,
    MCPSyncItem,
    MCPSyncRun,
    MCPSyncSchedule,
    Membership,
    Organization,
    Source,
    SourceAsset,
    User,
    Workspace,
)

RESOURCE_URI = "kb://policies/runtime-payment"


def seed_users(session: Session) -> tuple[dict[str, str], dict[str, str]]:
    suffix = uuid4().hex
    organization = Organization(name="Phase 13 Runtime", slug=f"phase13-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
        settings={},
    )
    session.add(workspace)
    session.flush()
    headers: dict[str, dict[str, str]] = {}
    for role in ("editor", "member"):
        token = f"phase13-{suffix}-{role}"
        user = User(
            organization_id=organization.id,
            email=f"{suffix}-{role}@example.com",
            display_name=f"Phase 13 {role}",
            api_token_hash=sha256(token.encode()).hexdigest(),
        )
        session.add(user)
        session.flush()
        session.add(
            Membership(
                organization_id=organization.id,
                workspace_id=workspace.id,
                user_id=user.id,
                role=role,
            )
        )
        headers[role] = {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization.id),
            "X-Workspace-ID": str(workspace.id),
        }
    session.commit()
    return headers["editor"], headers["member"]


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    api_url = os.environ.get("PHASE13_API_URL")
    if not database_url or not api_url:
        raise RuntimeError("DATABASE_URL and PHASE13_API_URL are required")
    engine = create_engine(database_url)
    with Session(engine) as session:
        editor_headers, member_headers = seed_users(session)
    organization_id = UUID(editor_headers["X-Organization-ID"])
    workspace_id = UUID(editor_headers["X-Workspace-ID"])

    credentials = {
        "endpoint_url": "https://runtime.mcp.example/mcp",
        "access_token": "runtime-mcp-token",
    }
    import_payload = {**credentials, "resource_uri": RESOURCE_URI}
    with httpx.Client(base_url=api_url, timeout=10) as client:
        connected = client.post(
            "/api/v1/integrations/mcp/test-connection",
            headers=editor_headers,
            json=credentials,
        )
        connected.raise_for_status()
        assert connected.json()["server_info"]["name"] == "phase13-mock-mcp"

        listed = client.post(
            "/api/v1/integrations/mcp/resources/list",
            headers=editor_headers,
            json=credentials,
        )
        listed.raise_for_status()
        assert listed.json()["resources"][0]["uri"] == RESOURCE_URI

        denied_intake = client.post(
            "/api/v1/integrations/mcp/resources/intake",
            headers=member_headers,
            json=import_payload,
        )
        assert denied_intake.status_code == 403

        intake = client.post(
            "/api/v1/integrations/mcp/resources/intake",
            headers=editor_headers,
            json=import_payload,
        )
        intake.raise_for_status()
        assert intake.status_code == 201
        intake_body = intake.json()
        assert intake_body["status"] == "succeeded"
        assert intake_body["review_status"] == "pending"
        assert intake_body["document_id"] is None
        assert intake_body["document_version_id"] is None
        assert intake_body["normalized_markdown"].startswith("---\n")

        review_queue = client.get(
            "/api/v1/ingestions?review_status=pending&limit=50",
            headers=member_headers,
        )
        review_queue.raise_for_status()
        assert [run["id"] for run in review_queue.json()] == [intake_body["id"]]

        created_connection = client.post(
            "/api/v1/integrations/mcp/connections",
            headers=editor_headers,
            json={
                "name": "Runtime knowledge",
                "endpoint_url": credentials["endpoint_url"],
                "credential_key": "runtime-knowledge",
            },
        )
        created_connection.raise_for_status()
        connection_body = created_connection.json()
        assert created_connection.status_code == 201
        assert connection_body["credential_configured"] is True
        assert "credential_key" not in created_connection.text
        assert "runtime-server-owned-token" not in created_connection.text

        saved_connections = client.get(
            "/api/v1/integrations/mcp/connections",
            headers=member_headers,
        )
        saved_connections.raise_for_status()
        assert saved_connections.json() == [connection_body]

        denied_saved_intake = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/resources/intake",
            headers=member_headers,
            json={"resource_uri": RESOURCE_URI},
        )
        assert denied_saved_intake.status_code == 403

        saved_intake = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/resources/intake",
            headers=editor_headers,
            json={"resource_uri": RESOURCE_URI},
        )
        saved_intake.raise_for_status()
        assert saved_intake.status_code == 201
        saved_intake_body = saved_intake.json()
        assert saved_intake_body["review_status"] == "pending"
        assert saved_intake_body["document_id"] is None

        saved_unchanged = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/resources/intake",
            headers=editor_headers,
            json={"resource_uri": RESOURCE_URI},
        )
        saved_unchanged.raise_for_status()
        assert saved_unchanged.status_code == 200
        assert saved_unchanged.json()["id"] == saved_intake_body["id"]

        saved_changed = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/resources/intake",
            headers=editor_headers,
            json={"resource_uri": RESOURCE_URI},
        )
        saved_changed.raise_for_status()
        assert saved_changed.status_code == 201
        assert saved_changed.json()["id"] != saved_intake_body["id"]

        saved_changed_replay = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/resources/intake",
            headers=editor_headers,
            json={"resource_uri": RESOURCE_URI},
        )
        saved_changed_replay.raise_for_status()
        assert saved_changed_replay.status_code == 200
        assert saved_changed_replay.json()["id"] == saved_changed.json()["id"]

        denied_sync_create = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/sync-runs",
            headers=member_headers,
            json={"resource_uris": [RESOURCE_URI]},
        )
        assert denied_sync_create.status_code == 403

        created_sync = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/sync-runs",
            headers=editor_headers,
            json={"resource_uris": [RESOURCE_URI]},
        )
        created_sync.raise_for_status()
        assert created_sync.status_code == 201
        created_sync_body = created_sync.json()
        assert created_sync_body["status"] == "queued"
        assert created_sync_body["requested_count"] == 1
        assert created_sync_body["completed_count"] == 0
        assert created_sync_body["items"][0]["attempt_count"] == 0
        assert created_sync_body["items"][0]["ingestion_run_id"] is None

        member_read = client.get(
            f"/api/v1/integrations/mcp/sync-runs/{created_sync_body['id']}",
            headers=member_headers,
        )
        member_read.raise_for_status()
        assert member_read.json() == created_sync_body

        denied_sync_execute = client.post(
            f"/api/v1/integrations/mcp/sync-runs/{created_sync_body['id']}/execute",
            headers=member_headers,
        )
        assert denied_sync_execute.status_code == 403

        executed_sync = client.post(
            f"/api/v1/integrations/mcp/sync-runs/{created_sync_body['id']}/execute",
            headers=editor_headers,
        )
        executed_sync.raise_for_status()
        executed_sync_body = executed_sync.json()
        assert executed_sync_body["status"] == "succeeded"
        assert executed_sync_body["completed_count"] == 1
        assert executed_sync_body["unchanged_count"] == 1
        assert executed_sync_body["changed_count"] == 0
        assert executed_sync_body["failed_count"] == 0
        assert executed_sync_body["items"][0]["status"] == "unchanged"
        assert executed_sync_body["items"][0]["attempt_count"] == 1
        assert executed_sync_body["items"][0]["ingestion_run_id"] == saved_changed.json()["id"]

        replayed_sync = client.post(
            f"/api/v1/integrations/mcp/sync-runs/{created_sync_body['id']}/execute",
            headers=editor_headers,
        )
        replayed_sync.raise_for_status()
        assert replayed_sync.json() == executed_sync_body

        discovered = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/resources/discover",
            headers=editor_headers,
        )
        discovered.raise_for_status()
        assert [resource["resource_uri"] for resource in discovered.json()] == [RESOURCE_URI]
        schedule = client.post(
            f"/api/v1/integrations/mcp/connections/{connection_body['id']}/schedules",
            headers=editor_headers,
            json={
                "name": "Runtime schedule",
                "interval_seconds": 300,
                "resource_uris": [RESOURCE_URI],
            },
        )
        schedule.raise_for_status()
        scheduled_run = client.post(
            f"/api/v1/integrations/mcp/schedules/{schedule.json()['id']}/run-now",
            headers=editor_headers,
        )
        scheduled_run.raise_for_status()
        assert scheduled_run.json()["status"] == "queued"
        scheduler_environment = os.environ.copy()
        scheduler_environment["MCP_SCHEDULER_TOKEN"] = editor_headers["Authorization"].removeprefix(
            "Bearer "
        )
        scheduler_environment["MCP_SCHEDULER_ORGANIZATION_ID"] = editor_headers["X-Organization-ID"]
        scheduler_environment["MCP_SCHEDULER_WORKSPACE_ID"] = editor_headers["X-Workspace-ID"]
        scheduler_environment["MCP_SCHEDULER_API_URL"] = os.environ["PHASE13_API_URL"]
        scheduler_process = subprocess.run(
            [sys.executable, "scripts/run_mcp_scheduler.py", "--once"],
            cwd=Path(__file__).resolve().parents[1],
            env=scheduler_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert scheduler_process.returncode == 0, scheduler_process.stderr[-2000:]
        scheduler_result = json.loads(scheduler_process.stdout.strip().splitlines()[-1])
        assert scheduler_result == {
            "attempted_count": 1,
            "dispatched_count": 0,
            "terminal_count": 1,
        }
        recovered_run = client.get(
            f"/api/v1/integrations/mcp/sync-runs/{scheduled_run.json()['id']}",
            headers=editor_headers,
        )
        recovered_run.raise_for_status()
        assert recovered_run.json()["status"] == "succeeded", recovered_run.json()
        cycle_replay = client.post(
            "/api/v1/integrations/mcp/scheduler/run-cycle",
            headers=editor_headers,
        )
        cycle_replay.raise_for_status()
        assert cycle_replay.json()["attempted_count"] == 0

        retired_direct_import = client.post(
            "/api/v1/integrations/mcp/resources/import",
            headers=editor_headers,
            json=import_payload,
        )
        assert retired_direct_import.status_code == 404

        arbitrary_tool = client.post(
            "/api/v1/integrations/mcp/tools/call",
            headers=editor_headers,
            json={**credentials, "tool": "delete_everything"},
        )
        assert arbitrary_tool.status_code == 404

        denied_endpoint = client.post(
            "/api/v1/integrations/mcp/resources/intake",
            headers=editor_headers,
            json={
                **import_payload,
                "endpoint_url": "https://evil.example/mcp?token=leak",
            },
        )
        assert denied_endpoint.status_code == 422
        assert denied_endpoint.json() == {"detail": "MCP endpoint is not allowed"}

    with Session(engine) as session:
        ingestion_runs = list(
            session.scalars(
                select(IngestionRun).where(
                    IngestionRun.organization_id == organization_id,
                    IngestionRun.workspace_id == workspace_id,
                )
            )
        )
        source_assets = list(
            session.scalars(
                select(SourceAsset).where(
                    SourceAsset.organization_id == organization_id,
                    SourceAsset.workspace_id == workspace_id,
                )
            )
        )
        connections = list(
            session.scalars(
                select(MCPConnection).where(
                    MCPConnection.organization_id == organization_id,
                    MCPConnection.workspace_id == workspace_id,
                )
            )
        )
        checkpoints = list(
            session.scalars(
                select(MCPResourceCheckpoint).where(
                    MCPResourceCheckpoint.organization_id == organization_id,
                    MCPResourceCheckpoint.workspace_id == workspace_id,
                )
            )
        )
        sync_runs = list(
            session.scalars(
                select(MCPSyncRun).where(
                    MCPSyncRun.organization_id == organization_id,
                    MCPSyncRun.workspace_id == workspace_id,
                )
            )
        )
        sync_items = list(
            session.scalars(
                select(MCPSyncItem).where(
                    MCPSyncItem.organization_id == organization_id,
                    MCPSyncItem.workspace_id == workspace_id,
                )
            )
        )
        discovered_resources = list(
            session.scalars(
                select(MCPDiscoveredResource).where(
                    MCPDiscoveredResource.organization_id == organization_id,
                    MCPDiscoveredResource.workspace_id == workspace_id,
                )
            )
        )
        schedules = list(
            session.scalars(
                select(MCPSyncSchedule).where(
                    MCPSyncSchedule.organization_id == organization_id,
                    MCPSyncSchedule.workspace_id == workspace_id,
                )
            )
        )
        schedule_ticks = list(
            session.scalars(
                select(MCPScheduleTick).where(
                    MCPScheduleTick.organization_id == organization_id,
                    MCPScheduleTick.workspace_id == workspace_id,
                )
            )
        )
        sources = list(
            session.scalars(
                select(Source).where(
                    Source.organization_id == organization_id,
                    Source.workspace_id == workspace_id,
                    Source.source_type == "mcp_instance",
                )
            )
        )

        audits = list(
            session.scalars(
                select(IntegrationAudit)
                .where(
                    IntegrationAudit.organization_id == organization_id,
                    IntegrationAudit.workspace_id == workspace_id,
                    IntegrationAudit.provider == "mcp",
                )
                .order_by(IntegrationAudit.created_at, IntegrationAudit.id)
            )
        )
        assert len(sources) == 1
        assert len(connections) == 1
        assert connections[0].source_id == sources[0].id
        assert connections[0].credential_key == "runtime-knowledge"
        assert len(checkpoints) == 1
        assert checkpoints[0].connection_id == connections[0].id
        assert checkpoints[0].resource_uri == RESOURCE_URI
        assert str(checkpoints[0].ingestion_run_id) == saved_changed.json()["id"]
        assert len(discovered_resources) == len(schedules) == len(schedule_ticks) == 1
        assert discovered_resources[0].resource_uri == RESOURCE_URI
        assert discovered_resources[0].available is True
        assert schedule_ticks[0].trigger == "manual"
        assert len(sync_runs) == len(sync_items) == 2
        assert all(run.status == "succeeded" for run in sync_runs)
        assert all(run.completed_count == 1 for run in sync_runs)
        assert all(run.unchanged_count == 1 for run in sync_runs)
        assert all(run.lease_owner is None for run in sync_runs)
        assert all(item.status == "unchanged" for item in sync_items)
        assert all(item.attempt_count == 1 for item in sync_items)
        assert all(item.ingestion_run_id == checkpoints[0].ingestion_run_id for item in sync_items)
        assert all(item.lease_owner is None for item in sync_items)
        assert len(ingestion_runs) == len(source_assets) == 3
        assert all(run.review_status == "pending" for run in ingestion_runs)
        assert all(run.document_id is None for run in ingestion_runs)
        asset_contents = [asset.content for asset in source_assets]
        assert (
            asset_contents.count(b"# Runtime Payment Policy\n\nInvoices are due in 30 days.") == 2
        )
        assert (
            asset_contents.count(b"# Runtime Payment Policy\n\nInvoices are due in 45 days.") == 1
        )
        assert all(asset.source_id == sources[0].id for asset in source_assets)

        actual_audit_operations = [audit.operation for audit in audits]
        assert actual_audit_operations == [
            "test_connection",
            "list_resources",
            "intake_resource",
            "create_connection",
            "intake_saved_resource",
            "intake_saved_resource_unchanged",
            "intake_saved_resource",
            "intake_saved_resource_unchanged",
            "create_sync_run",
            "sync_resource_unchanged",
            "discover_resources",
            "create_sync_schedule",
            "run_sync_schedule_now",
            "sync_resource_unchanged",
            "intake_resource",
        ], actual_audit_operations
        serialized = " ".join(
            [*(str(audit.__dict__) for audit in audits), str(connections[0].__dict__)]
        )
        assert "runtime-mcp-token" not in serialized
        assert "runtime-server-owned-token" not in serialized
        assert "token=leak" not in serialized
        audits[0].error_message = "tamper"
        try:
            session.commit()
        except DBAPIError:
            session.rollback()
        else:
            raise AssertionError("integration audit update was not rejected")

    engine.dispose()
    print("phase13_tcp_connection_resources=passed")
    print("phase13_tcp_direct_canonical_import_absent=passed")
    print("phase13_tcp_permissions_read_only=passed")
    print("phase13_tcp_idempotency=passed")
    print("phase13_tcp_audit_provenance=passed")
    print("phase16a_tcp_mcp_intake_review_queue=passed")
    print("phase16a_tcp_raw_asset_preservation=passed")
    print("phase16a_tcp_no_prepromotion_canonical_creation=passed")
    print("phase16b1_tcp_saved_connection=passed")
    print("phase16b1_tcp_server_owned_credential=passed")
    print("phase16b1_tcp_saved_intake_review_queue=passed")
    print("phase16b2_tcp_unchanged_resource_noop=passed")
    print("phase16b2_tcp_changed_resource_checkpoint=passed")
    print("phase16b3_tcp_persistent_sync_run=passed")
    print("phase16b3_tcp_writer_execute_member_read=passed")
    print("phase16b3_tcp_terminal_replay=passed")
    print("phase16b3_tcp_no_prepromotion_canonical_creation=passed")
    print("phase16b4_tcp_persistent_discovery_catalog=passed")
    print("phase16b4_tcp_schedule_run_now=passed")
    print("phase16b4_tcp_autonomous_recovery_cycle=passed")
    print("phase16b4_tcp_terminal_replay=passed")


if __name__ == "__main__":
    main()
