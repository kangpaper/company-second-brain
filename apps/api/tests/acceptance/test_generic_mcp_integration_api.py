from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from company_brain.api.generic_mcp_integrations import (
    get_allowed_mcp_hosts,
    get_mcp_connector_factory,
    get_mcp_sync_session_factory,
)
from company_brain.db.session import get_session
from company_brain.domain.models import (
    Document,
    DocumentVersion,
    Evidence,
    EvidenceLink,
    ExtractionCandidate,
    IngestionRun,
    IntegrationAudit,
    MCPConnection,
    MCPDiscoveredResource,
    MCPResourceCheckpoint,
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
from company_brain.integrations.mcp.adapter import MCPResourceContent
from company_brain.main import app


class FakeMCPConnector:
    def __init__(self) -> None:
        self.initialize_calls = 0
        self.list_calls = 0
        self.read_uris: list[str] = []
        self.closed = False
        self.fail_read = False
        self.fail_close = False
        self.text_override: str | None = None
        self.name_override: str | None = None

    def initialize(self) -> dict[str, Any]:
        self.initialize_calls += 1
        return {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "knowledge-mcp", "version": "1.0"},
            "capabilities": {"resources": {}},
        }

    def list_resources(self, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        self.list_calls += 1
        assert cursor is None
        return ([{"uri": "kb://policies/payment", "name": "Payment Policy"}], None)

    def read_resource(self, uri: str) -> MCPResourceContent:
        self.read_uris.append(uri)
        if self.fail_read:
            raise RuntimeError("remote secret=should-not-leak")
        return MCPResourceContent(
            uri=uri,
            name=self.name_override or "Payment Policy",
            mime_type="text/markdown",
            text=self.text_override or "# Payment Policy\n\nInvoices are due in 30 days.",
        )

    def close(self) -> None:
        if self.fail_close:
            raise RuntimeError("cleanup secret=should-not-leak")
        self.closed = True


def seed_editor(session: Session, *, role: str = "editor") -> dict[str, str]:
    suffix = uuid4().hex
    token = f"phase13-{suffix}"
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug="main",
        settings={},
    )
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
        display_name="Phase 13 Editor",
        api_token_hash=sha256(token.encode()).hexdigest(),
    )
    session.add_all([workspace, user])
    session.flush()
    session.add(
        Membership(
            organization_id=organization.id,
            workspace_id=workspace.id,
            user_id=user.id,
            role=role,
        )
    )
    session.commit()
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization.id),
        "X-Workspace-ID": str(workspace.id),
    }


async def test_direct_import_endpoint_is_not_exposed(session: Session) -> None:
    constructed = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal constructed
        constructed += 1
        return FakeMCPConnector()

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/mcp/resources/import",
                headers=seed_editor(session),
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert constructed == 0
    assert session.scalar(select(Document)) is None
    assert session.scalar(select(Evidence)) is None


async def test_resource_intake_enters_shared_review_pipeline_without_canonical_creation(
    session: Session,
) -> None:
    connector = FakeMCPConnector()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            response = await client.post(
                "/api/v1/integrations/mcp/resources/intake",
                headers=headers,
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
            queue = await client.get(
                "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["review_status"] == "pending"
    assert body["classification"]["document_type"] == "invoice"
    assert body["normalized_markdown"].startswith("---\n")
    assert body["document_id"] is None
    assert connector.read_uris == ["kb://policies/payment"]
    assert connector.closed is True
    assert body["id"] in {item["id"] for item in queue.json()}

    run = session.get(IngestionRun, UUID(body["id"]))
    assert run is not None and run.source_asset_id is not None
    source = session.get(Source, run.source_id)
    asset = session.get(SourceAsset, run.source_asset_id)
    assert source is not None and source.source_type == "mcp_instance"
    assert source.uri == "https://knowledge.example.com/mcp"
    assert asset is not None
    assert asset.content == b"# Payment Policy\n\nInvoices are due in 30 days."
    assert asset.source_id == source.id
    assert session.scalar(select(Document)) is None
    assert session.scalar(select(Evidence)) is None
    assert len(list(session.scalars(select(ExtractionCandidate)))) >= 1
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None and audit.operation == "intake_resource"
    assert audit.outcome == "succeeded"
    assert "request-scoped-secret" not in str(audit.__dict__)


async def test_resource_intake_normalization_failure_preserves_raw_asset_and_audit(
    session: Session,
) -> None:
    connector = FakeMCPConnector()
    connector.text_override = "x" * 2_000_000
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            response = await client.post(
                "/api/v1/integrations/mcp/resources/intake",
                headers=headers,
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
            queue = await client.get(
                "/api/v1/ingestions?review_status=pending&limit=25", headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "normalization_error"
    assert response.json()["detail"]["message"] == "MCP resource intake failed"
    assert queue.status_code == 200
    assert queue.json() == []
    assert connector.closed is True

    run = session.scalar(select(IngestionRun))
    asset = session.scalar(select(SourceAsset))
    audit = session.scalar(select(IntegrationAudit))
    assert run is not None
    assert asset is not None
    assert audit is not None
    assert run.status == "failed"
    assert run.review_status == "pending"
    assert run.source_asset_id == asset.id
    assert asset.byte_size == 2_000_000
    assert asset.content == b"x" * 2_000_000
    assert audit.operation == "intake_resource"
    assert audit.outcome == "failed"
    assert audit.error_code == "normalization_error"
    assert audit.error_message == "MCP resource intake failed"
    assert session.scalar(select(Document)) is None
    assert session.scalar(select(Evidence)) is None
    serialized = str(audit.__dict__)
    assert "request-scoped-secret" not in serialized
    assert "kb://policies/payment" not in serialized


@pytest.mark.parametrize("control", ["\u202e", "\u0007", "\u200f"])
async def test_resource_intake_rejects_unicode_control_in_remote_title_before_staging(
    session: Session,
    control: str,
) -> None:
    connector = FakeMCPConnector()
    connector.name_override = f"Payment {control}Policy"
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            response = await client.post(
                "/api/v1/integrations/mcp/resources/intake",
                headers=headers,
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "MCP resource intake failed"}
    assert connector.closed is True
    assert session.scalar(select(SourceAsset)) is None
    assert session.scalar(select(IngestionRun)) is None
    assert session.scalar(select(Document)) is None
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.operation == "intake_resource"
    assert audit.outcome == "failed"
    assert audit.error_code == "connector_error"
    assert control not in str(audit.__dict__)
    assert "request-scoped-secret" not in str(audit.__dict__)


async def test_saved_connection_uses_server_owned_credential_for_reviewed_intake(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    constructed: list[tuple[str, str]] = []

    def factory(endpoint: str, token: str) -> FakeMCPConnector:
        constructed.append((endpoint, token))
        return connector

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            created = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            listed = await client.get("/api/v1/integrations/mcp/connections", headers=headers)
            connection_id = created.json().get("id")
            intake = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/intake",
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    created_body = created.json()
    assert created_body == {
        "id": created_body["id"],
        "name": "Knowledge production",
        "endpoint_url": "https://knowledge.example.com/mcp",
        "enabled": True,
        "credential_configured": True,
    }
    assert "credential_key" not in created.text
    assert "server-owned-secret" not in created.text
    assert listed.status_code == 200
    assert listed.json() == [created_body]
    assert intake.status_code == 201
    assert intake.json()["review_status"] == "pending"
    assert intake.json()["document_id"] is None
    assert constructed == [("https://knowledge.example.com/mcp", "server-owned-secret")]

    row = (
        session.execute(
            text(
                "SELECT c.name, s.uri AS endpoint_url, c.credential_key, c.enabled "
                "FROM mcp_connections AS c "
                "JOIN sources AS s ON s.organization_id = c.organization_id "
                "AND s.workspace_id = c.workspace_id AND s.id = c.source_id"
            )
        )
        .mappings()
        .one()
    )
    assert dict(row) == {
        "name": "Knowledge production",
        "endpoint_url": "https://knowledge.example.com/mcp",
        "credential_key": "knowledge-prod",
        "enabled": 1,
    }
    serialized = " ".join(
        str(value)
        for value in (
            row,
            list(session.scalars(select(IntegrationAudit))),
            list(session.scalars(select(Source))),
            list(session.scalars(select(IngestionRun))),
        )
    )
    assert "server-owned-secret" not in serialized
    assert [audit.operation for audit in session.scalars(select(IntegrationAudit))] == [
        "create_connection",
        "intake_saved_resource",
    ]


async def test_saved_connection_discovers_and_persists_resource_catalog_without_intake(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            discovered = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            rediscovered = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            listed = await client.get(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources",
                headers=headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert connection.status_code == 201
    assert discovered.status_code == 200
    assert rediscovered.status_code == 200
    assert listed.status_code == 200
    expected = [
        {
            "id": discovered.json()[0]["id"],
            "connection_id": connection_id,
            "resource_uri": "kb://policies/payment",
            "name": "Payment Policy",
            "description": None,
            "mime_type": None,
            "size": None,
            "available": True,
        }
    ]
    assert discovered.json() == expected
    assert rediscovered.json() == expected
    assert listed.json() == expected
    assert connector.list_calls == 2
    assert connector.read_uris == []
    assert connector.closed is True
    assert session.scalar(select(IngestionRun)) is None
    assert session.scalar(select(SourceAsset)) is None
    assert session.scalar(select(Document)) is None
    assert [audit.operation for audit in session.scalars(select(IntegrationAudit))] == [
        "create_connection",
        "discover_resources",
        "discover_resources",
    ]


async def test_discovery_cursor_cycle_only_marks_missing_resources_after_final_page(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PagedConnector(FakeMCPConnector):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[str | None] = []

        def list_resources(
            self, cursor: str | None = None
        ) -> tuple[list[dict[str, Any]], str | None]:
            self.list_calls += 1
            self.cursors.append(cursor)
            if self.list_calls == 1:
                return ([{"uri": "kb://resource/a", "name": "A"}], "page-2")
            if self.list_calls == 2:
                return ([{"uri": "kb://resource/b", "name": "B"}], None)
            assert cursor is None
            return ([{"uri": "kb://resource/b", "name": "B"}], None)

    connector = PagedConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Paged knowledge",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            path = (
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/resources/discover"
            )
            first_page = await client.post(path, headers=headers)
            final_page = await client.post(path, headers=headers)
            next_cycle = await client.post(path, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert [first_page.status_code, final_page.status_code, next_cycle.status_code] == [
        200,
        200,
        200,
    ]
    assert connector.cursors == [None, "page-2", None]
    assert first_page.headers["x-mcp-discovery-cycle-complete"] == "false"
    assert final_page.headers["x-mcp-discovery-cycle-complete"] == "true"
    assert next_cycle.headers["x-mcp-discovery-cycle-complete"] == "true"
    first_by_uri = {item["resource_uri"]: item for item in first_page.json()}
    final_by_uri = {item["resource_uri"]: item for item in final_page.json()}
    next_by_uri = {item["resource_uri"]: item for item in next_cycle.json()}
    assert first_by_uri["kb://resource/a"]["available"] is True
    assert final_by_uri["kb://resource/a"]["available"] is True
    assert final_by_uri["kb://resource/b"]["available"] is True
    assert next_by_uri["kb://resource/a"]["available"] is False
    assert next_by_uri["kb://resource/b"]["available"] is True
    assert connector.read_uris == []
    assert session.scalar(select(MCPResourceCheckpoint)) is None
    assert session.scalar(select(IngestionRun)) is None


async def test_discovery_failure_preserves_catalog_and_cursor_atomically(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPageConnector(FakeMCPConnector):
        def list_resources(
            self, cursor: str | None = None
        ) -> tuple[list[dict[str, Any]], str | None]:
            self.list_calls += 1
            if self.list_calls == 1:
                assert cursor is None
                return ([{"uri": "kb://resource/stable", "name": "Stable"}], "page-2")
            assert cursor == "page-2"
            raise RuntimeError("remote token=must-not-leak")

    connector = FailingPageConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Atomic discovery",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            path = f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover"
            first = await client.post(path, headers=headers)
            failed = await client.post(path, headers=headers)
            catalog = await client.get(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources",
                headers=headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert failed.status_code == 502
    assert "must-not-leak" not in failed.text
    assert [resource["resource_uri"] for resource in catalog.json()] == ["kb://resource/stable"]
    persisted = session.get(MCPConnection, UUID(connection_id))
    assert persisted is not None
    assert persisted.discovery_cursor == "page-2"
    assert persisted.discovery_cycle_id is not None
    assert persisted.discovery_lease_owner is None
    assert persisted.discovery_lease_expires_at is None
    failed_audit = list(session.scalars(select(IntegrationAudit)))[-1]
    assert failed_audit.outcome == "failed"
    assert failed_audit.error_message == "MCP discovery failed"
    assert "must-not-leak" not in str(failed_audit.__dict__)


async def test_discovered_resources_can_be_scheduled_and_run_now_without_network(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Scheduled knowledge",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            created = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Every five minutes",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            listed = await client.get(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
            )
            run_now = await client.post(
                f"/api/v1/integrations/mcp/schedules/{created.json().get('id')}/run-now",
                headers=headers,
            )
            disabled = await client.patch(
                f"/api/v1/integrations/mcp/schedules/{created.json().get('id')}",
                headers=headers,
                json={"enabled": False},
            )
            disabled_run = await client.post(
                f"/api/v1/integrations/mcp/schedules/{created.json().get('id')}/run-now",
                headers=headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    schedule = created.json()
    assert schedule == {
        "id": schedule["id"],
        "connection_id": connection_id,
        "name": "Every five minutes",
        "interval_seconds": 300,
        "enabled": True,
        "next_due_at": schedule["next_due_at"],
        "resource_uris": ["kb://policies/payment"],
    }
    assert listed.status_code == 200
    assert listed.json() == [schedule]
    assert run_now.status_code == 201
    assert run_now.json()["status"] == "queued"
    assert run_now.json()["max_concurrency"] == 4
    assert run_now.json()["max_attempts"] == 3
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled_run.status_code == 409
    assert connector.list_calls == 1
    assert connector.read_uris == []


async def test_schedule_mutations_revalidate_current_connection_authority(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    allowed_hosts = {"knowledge.example.com"}
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: allowed_hosts
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Authority knowledge",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "")
            denied_create = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Denied authority schedule",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
            created = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Authority schedule",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            schedule_path = f"/api/v1/integrations/mcp/schedules/{created.json()['id']}"
            disabled = await client.patch(schedule_path, headers=headers, json={"enabled": False})
            monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "")
            denied_enable = await client.patch(
                schedule_path, headers=headers, json={"enabled": True}
            )
            monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
            allowed_hosts.clear()
            denied_interval = await client.patch(
                schedule_path, headers=headers, json={"interval_seconds": 600}
            )
            denied_bundled_disable_mutation = await client.patch(
                schedule_path,
                headers=headers,
                json={"enabled": False, "interval_seconds": 900},
            )
            denied_explicit_null_interval = await client.patch(
                schedule_path,
                headers=headers,
                json={"enabled": False, "interval_seconds": None},
            )
            denied_explicit_null_resources = await client.patch(
                schedule_path,
                headers=headers,
                json={"enabled": False, "resource_uris": None},
            )
    finally:
        app.dependency_overrides.clear()

    assert denied_create.status_code == 409
    assert created.status_code == 201
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert denied_enable.status_code == 409
    assert denied_interval.status_code == 422
    assert denied_bundled_disable_mutation.status_code == 422
    assert denied_explicit_null_interval.status_code == 422
    assert denied_explicit_null_resources.status_code == 422
    failed_schedule_audits = list(
        session.scalars(
            select(IntegrationAudit).where(
                IntegrationAudit.operation.in_({"create_sync_schedule", "update_sync_schedule"}),
                IntegrationAudit.outcome == "failed",
            )
        )
    )
    assert len(failed_schedule_audits) == 6
    assert all(
        audit.error_message == "MCP sync schedule mutation rejected"
        for audit in failed_schedule_audits
    )


async def test_schedule_authority_denial_never_persists_endpoint_secrets(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_hosts = {"knowledge.example.com"}
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: allowed_hosts
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Secret endpoint schedule",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = UUID(connection.json()["id"])
            await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            schedule = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Secret endpoint baseline",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            schedule_path = f"/api/v1/integrations/mcp/schedules/{schedule.json()['id']}"
            saved_connection = session.get(MCPConnection, connection_id)
            assert saved_connection is not None
            source = session.get(Source, saved_connection.source_id)
            assert source is not None
            source.uri = "https://user:CANARY-SECRET@evil.example/mcp"
            session.commit()

            denied = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Rejected secret endpoint",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            disabled = await client.patch(schedule_path, headers=headers, json={"enabled": False})
            denied_run = await client.post(f"{schedule_path}/run-now", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert denied.status_code == 422
    assert disabled.status_code == 200
    assert denied_run.status_code == 409
    audits = list(
        session.scalars(
            select(IntegrationAudit).where(
                IntegrationAudit.operation.in_(
                    {"create_sync_schedule", "update_sync_schedule", "run_sync_schedule_now"}
                )
            )
        )
    )
    assert len(audits) == 4
    assert all(audit.endpoint == "invalid://invalid/" for audit in audits[-3:])
    assert all("CANARY-SECRET" not in audit.endpoint for audit in audits)


async def test_saved_intake_credential_denial_never_persists_endpoint_secrets(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Secret endpoint intake",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = UUID(connection.json()["id"])
            saved_connection = session.get(MCPConnection, connection_id)
            assert saved_connection is not None
            source = session.get(Source, saved_connection.source_id)
            assert source is not None
            source.uri = "https://user:INTAKE-CANARY@evil.example/mcp?token=secret"
            session.commit()
            monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "")

            denied = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/intake",
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
    finally:
        app.dependency_overrides.clear()

    assert denied.status_code == 409
    audit = session.scalar(
        select(IntegrationAudit).where(
            IntegrationAudit.operation == "intake_saved_resource",
            IntegrationAudit.outcome == "failed",
        )
    )
    assert audit is not None
    assert audit.endpoint == "invalid://invalid/"
    assert "INTAKE-CANARY" not in audit.endpoint


async def test_run_now_revalidates_catalog_availability(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Unavailable catalog knowledge",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            schedule = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Unavailable catalog schedule",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            discovered = session.scalar(
                select(MCPDiscoveredResource).where(
                    MCPDiscoveredResource.connection_id == UUID(connection_id)
                )
            )
            assert discovered is not None
            discovered.available = False
            session.commit()
            denied_run = await client.post(
                f"/api/v1/integrations/mcp/schedules/{schedule.json()['id']}/run-now",
                headers=headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert denied_run.status_code == 422
    assert session.scalar(select(func.count()).select_from(MCPSyncRun)) == 0


async def test_scheduler_tick_dispatches_one_due_slot_without_network(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Due knowledge",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            schedule = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Due schedule",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            await client.patch(
                f"/api/v1/integrations/mcp/schedules/{schedule.json()['id']}",
                headers=headers,
                json={"enabled": False},
            )
            session.execute(
                text(
                    "UPDATE mcp_sync_schedules SET enabled = true, "
                    "next_due_at = CURRENT_TIMESTAMP WHERE id = :schedule_id"
                ),
                {"schedule_id": UUID(schedule.json()["id"]).hex},
            )
            session.commit()
            first = await client.post(
                "/api/v1/integrations/mcp/scheduler/dispatch-due", headers=headers
            )
            second = await client.post(
                "/api/v1/integrations/mcp/scheduler/dispatch-due", headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["dispatched_count"] == 1
    assert len(first.json()["sync_run_ids"]) == 1
    assert second.status_code == 200
    assert second.json() == {"dispatched_count": 0, "sync_run_ids": []}
    run_id = first.json()["sync_run_ids"][0]
    persisted_run = session.get(MCPSyncRun, UUID(run_id))
    assert persisted_run is not None
    assert persisted_run.status == "queued"
    assert persisted_run.max_concurrency == 4
    assert persisted_run.max_attempts == 3
    assert connector.list_calls == 1
    assert connector.read_uris == []


async def test_invalid_due_schedules_advance_and_do_not_starve_valid_work(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "invalid-prod,valid-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_INVALID_PROD", "server-owned-invalid")
    monkeypatch.setenv("MCP_CREDENTIAL_VALID_PROD", "server-owned-valid")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {
        "invalid.example.com",
        "valid.example.com",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection_ids: list[str] = []
            for name, host, credential_key in (
                ("Invalid knowledge", "invalid.example.com", "invalid-prod"),
                ("Valid knowledge", "valid.example.com", "valid-prod"),
            ):
                connection = await client.post(
                    "/api/v1/integrations/mcp/connections",
                    headers=headers,
                    json={
                        "name": name,
                        "endpoint_url": f"https://{host}/mcp",
                        "credential_key": credential_key,
                    },
                )
                connection_ids.append(connection.json()["id"])
                await client.post(
                    f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/resources/discover",
                    headers=headers,
                )
            schedule_ids: list[str] = []
            for index in range(4):
                schedule = await client.post(
                    f"/api/v1/integrations/mcp/connections/{connection_ids[0]}/schedules",
                    headers=headers,
                    json={
                        "name": f"Invalid schedule {index}",
                        "interval_seconds": 300,
                        "resource_uris": ["kb://policies/payment"],
                    },
                )
                schedule_ids.append(schedule.json()["id"])
            valid_schedule = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_ids[1]}/schedules",
                headers=headers,
                json={
                    "name": "Valid schedule",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            schedule_ids.append(valid_schedule.json()["id"])
            database_now = datetime.now(UTC)
            for index, schedule_id in enumerate(schedule_ids):
                schedule = session.get(MCPSyncSchedule, UUID(schedule_id))
                assert schedule is not None
                schedule.next_due_at = database_now - timedelta(seconds=10 - index)
            session.commit()
            monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "valid-prod")
            first = await client.post(
                "/api/v1/integrations/mcp/scheduler/dispatch-due", headers=headers
            )
            second = await client.post(
                "/api/v1/integrations/mcp/scheduler/dispatch-due", headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["dispatched_count"] == 0
    assert second.status_code == 200
    assert second.json()["dispatched_count"] == 1
    operations = [audit.operation for audit in session.scalars(select(IntegrationAudit))]
    assert operations.count("dispatch_sync_schedule_skipped") == 4


async def test_scheduler_recovery_cycle_executes_queued_scheduled_run_idempotently(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connectors: list[FakeMCPConnector] = []

    def factory(_: str, __: str) -> FakeMCPConnector:
        connector = FakeMCPConnector()
        connectors.append(connector)
        return connector

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    worker_sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    app.dependency_overrides[get_mcp_sync_session_factory] = lambda: worker_sessions
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Recovery knowledge",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = connection.json()["id"]
            await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/resources/discover",
                headers=headers,
            )
            schedule = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection_id}/schedules",
                headers=headers,
                json={
                    "name": "Recovery schedule",
                    "interval_seconds": 300,
                    "resource_uris": ["kb://policies/payment"],
                },
            )
            queued = await client.post(
                f"/api/v1/integrations/mcp/schedules/{schedule.json()['id']}/run-now",
                headers=headers,
            )
            first = await client.post(
                "/api/v1/integrations/mcp/scheduler/run-cycle", headers=headers
            )
            second = await client.post(
                "/api/v1/integrations/mcp/scheduler/run-cycle", headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert queued.status_code == 201
    assert queued.json()["status"] == "queued"
    assert first.status_code == 200
    assert first.json() == {
        "attempted_count": 1,
        "terminal_count": 1,
        "sync_run_ids": [queued.json()["id"]],
    }
    assert second.status_code == 200
    assert second.json() == {
        "attempted_count": 0,
        "terminal_count": 0,
        "sync_run_ids": [],
    }
    assert sum(len(connector.read_uris) for connector in connectors) == 1
    assert session.get(MCPSyncRun, UUID(queued.json()["id"])).status == "succeeded"


async def test_saved_connection_creates_bounded_persistent_sync_run_without_network(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_calls = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal connector_calls
        connector_calls += 1
        return FakeMCPConnector()

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            response = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={
                    "resource_uris": [
                        "kb://policies/payment",
                        "kb://policies/refunds",
                    ]
                },
            )
            duplicate = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={
                    "resource_uris": [
                        "kb://policies/payment",
                        "kb://policies/payment",
                    ]
                },
            )
            over_limit = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={"resource_uris": [f"kb://policies/resource-{index}" for index in range(17)]},
            )
            saved_connection = session.get(MCPConnection, UUID(connection.json()["id"]))
            assert saved_connection is not None
            saved_source = session.get(Source, saved_connection.source_id)
            assert saved_source is not None
            saved_source.uri = "https://unapproved.example.net/mcp"
            session.commit()
            invalid_endpoint = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={"resource_uris": ["kb://policies/blocked"]},
            )
            other_tenant_headers = seed_editor(session)
            cross_tenant_read = await client.get(
                f"/api/v1/integrations/mcp/sync-runs/{response.json()['id']}",
                headers=other_tenant_headers,
            )
            cross_tenant_execute = await client.post(
                f"/api/v1/integrations/mcp/sync-runs/{response.json()['id']}/execute",
                headers=other_tenant_headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert connection.status_code == 201
    assert response.status_code == 201
    assert duplicate.status_code == 422
    assert over_limit.status_code == 422
    assert invalid_endpoint.status_code == 422
    assert cross_tenant_read.status_code == 404
    assert cross_tenant_execute.status_code == 404
    assert "kb://policies/payment" not in duplicate.text
    assert "kb://policies/resource-16" not in over_limit.text
    body = response.json()
    assert body == {
        "id": body["id"],
        "connection_id": connection.json()["id"],
        "status": "queued",
        "requested_count": 2,
        "completed_count": 0,
        "changed_count": 0,
        "unchanged_count": 0,
        "failed_count": 0,
        "max_concurrency": 4,
        "max_attempts": 3,
        "started_at": None,
        "finished_at": None,
        "items": [
            {
                "id": body["items"][0]["id"],
                "ordinal": 0,
                "resource_uri": "kb://policies/payment",
                "status": "queued",
                "attempt_count": 0,
                "ingestion_run_id": None,
                "error_code": None,
            },
            {
                "id": body["items"][1]["id"],
                "ordinal": 1,
                "resource_uri": "kb://policies/refunds",
                "status": "queued",
                "attempt_count": 0,
                "ingestion_run_id": None,
                "error_code": None,
            },
        ],
    }
    assert connector_calls == 0
    persisted_run = session.get(MCPSyncRun, UUID(body["id"]))
    assert persisted_run is not None
    persisted_items = list(
        session.scalars(
            select(MCPSyncItem)
            .where(MCPSyncItem.sync_run_id == persisted_run.id)
            .order_by(MCPSyncItem.ordinal)
        )
    )
    assert persisted_run.status == "queued"
    assert persisted_run.requested_count == 2
    assert len(persisted_items) == 2
    assert len(list(session.scalars(select(MCPSyncRun)))) == 1


async def test_sync_run_executes_items_persists_attempts_and_is_terminally_idempotent(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connectors: list[FakeMCPConnector] = []

    def factory(_: str, __: str) -> FakeMCPConnector:
        connector = FakeMCPConnector()
        connectors.append(connector)
        return connector

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    worker_sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    app.dependency_overrides[get_mcp_sync_session_factory] = lambda: worker_sessions
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            created = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={"resource_uris": ["kb://policies/payment"]},
            )
            fetched = await client.get(
                f"/api/v1/integrations/mcp/sync-runs/{created.json()['id']}",
                headers=headers,
            )
            execute_url = f"/api/v1/integrations/mcp/sync-runs/{created.json()['id']}/execute"
            executed = await client.post(execute_url, headers=headers)
            replay = await client.post(execute_url, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert executed.status_code == 200
    body = executed.json()
    assert body["status"] == "succeeded"
    assert body["requested_count"] == body["completed_count"] == 1
    assert body["changed_count"] == 1
    assert body["unchanged_count"] == body["failed_count"] == 0
    assert body["started_at"] is not None
    assert body["finished_at"] is not None
    assert body["items"][0]["status"] == "changed"
    assert body["items"][0]["attempt_count"] == 1
    assert body["items"][0]["ingestion_run_id"] is not None
    assert body["items"][0]["error_code"] is None
    assert replay.status_code == 200
    assert replay.json() == body
    assert len(connectors) == 1
    assert connectors[0].closed is True
    assert len(list(session.scalars(select(SourceAsset)))) == 1
    assert len(list(session.scalars(select(IngestionRun)))) == 1
    assert session.scalar(select(Document)) is None
    assert session.scalar(select(Evidence)) is None


async def test_sync_run_retries_transient_connector_failure_and_sanitizes_exhaustion(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    fail_forever = False

    class RetryConnector(FakeMCPConnector):
        def read_resource(self, uri: str) -> MCPResourceContent:
            nonlocal attempts
            attempts += 1
            if fail_forever or attempts <= 2:
                raise RuntimeError("server-owned-secret must not persist")
            return super().read_resource(uri)

    def factory(_: str, __: str) -> RetryConnector:
        return RetryConnector()

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    worker_sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    app.dependency_overrides[get_mcp_sync_session_factory] = lambda: worker_sessions
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            connection = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            first_run = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={"resource_uris": ["kb://policies/payment"]},
            )
            succeeded = await client.post(
                f"/api/v1/integrations/mcp/sync-runs/{first_run.json()['id']}/execute",
                headers=headers,
            )
            fail_forever = True
            attempts = 0
            second_run = await client.post(
                f"/api/v1/integrations/mcp/connections/{connection.json()['id']}/sync-runs",
                headers=headers,
                json={"resource_uris": ["kb://policies/refunds"]},
            )
            failed = await client.post(
                f"/api/v1/integrations/mcp/sync-runs/{second_run.json()['id']}/execute",
                headers=headers,
            )
            failed_replay = await client.post(
                f"/api/v1/integrations/mcp/sync-runs/{second_run.json()['id']}/execute",
                headers=headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert succeeded.status_code == 200
    assert succeeded.json()["status"] == "succeeded"
    assert succeeded.json()["items"][0]["attempt_count"] == 3
    assert succeeded.json()["items"][0]["status"] == "changed"
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["items"][0]["attempt_count"] == 3
    assert failed.json()["items"][0]["error_code"] == "connector_error"
    assert failed_replay.json() == failed.json()
    assert attempts == 3
    assert "server-owned-secret" not in failed.text
    persisted_item = session.get(MCPSyncItem, UUID(failed.json()["items"][0]["id"]))
    assert persisted_item is not None
    assert persisted_item.error_message == "MCP sync item failed"
    audit_text = " ".join(
        f"{audit.error_code} {audit.error_message}"
        for audit in session.scalars(select(IntegrationAudit))
    )
    assert "server-owned-secret" not in audit_text


async def test_saved_connection_checkpoints_unchanged_and_changed_resources(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            created = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            connection_id = created.json()["id"]
            intake_url = f"/api/v1/integrations/mcp/connections/{connection_id}/resources/intake"
            first = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            unchanged = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            unchanged_asset_count = len(list(session.scalars(select(SourceAsset))))
            unchanged_run_count = len(list(session.scalars(select(IngestionRun))))

            connector.text_override = "# Payment Policy\n\nInvoices are now due in 45 days."
            changed = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            changed_replay = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            connector.name_override = "Revised Payment Policy"
            metadata_changed = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            metadata_replay = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert first.status_code == 201
    assert unchanged.status_code == 200
    assert unchanged.json()["id"] == first.json()["id"]
    assert unchanged_asset_count == 1
    assert unchanged_run_count == 1
    assert changed.status_code == 201
    assert changed.json()["id"] != first.json()["id"]
    assert changed_replay.status_code == 200
    assert changed_replay.json()["id"] == changed.json()["id"]
    assert metadata_changed.status_code == 201
    assert metadata_changed.json()["id"] != changed.json()["id"]
    assert metadata_replay.status_code == 200
    assert metadata_replay.json()["id"] == metadata_changed.json()["id"]
    assert len(list(session.scalars(select(SourceAsset)))) == 3
    assert len(list(session.scalars(select(IngestionRun)))) == 3
    assert session.scalar(select(Document)) is None
    assert session.scalar(select(DocumentVersion)) is None
    assert session.scalar(select(Evidence)) is None
    assert session.scalar(select(EvidenceLink)) is None


async def test_saved_checkpoint_does_not_advance_when_connector_cleanup_fails(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeMCPConnector()
    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            created = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            intake_url = (
                f"/api/v1/integrations/mcp/connections/{created.json()['id']}/resources/intake"
            )
            first = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            checkpoint_before = session.scalar(select(MCPResourceCheckpoint))
            assert checkpoint_before is not None
            prior_run_id = checkpoint_before.ingestion_run_id

            connector.text_override = "# Payment Policy\n\nInvoices are now due in 45 days."
            connector.fail_close = True
            failed = await client.post(
                intake_url,
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201
    assert failed.status_code == 502
    assert failed.json() == {"detail": "MCP resource intake failed"}
    checkpoint_after = session.scalar(select(MCPResourceCheckpoint))
    assert checkpoint_after is not None
    assert checkpoint_after.ingestion_run_id == prior_run_id
    assert len(list(session.scalars(select(SourceAsset)))) == 1
    assert len(list(session.scalars(select(IngestionRun)))) == 1
    failed_audit = list(session.scalars(select(IntegrationAudit)))[-1]
    assert failed_audit.operation == "intake_saved_resource"
    assert failed_audit.outcome == "failed"
    assert failed_audit.error_code == "connector_error"
    assert "cleanup secret=should-not-leak" not in str(failed_audit.__dict__)


async def test_saved_connection_rejects_client_secret_without_reflecting_it(
    session: Session,
) -> None:
    canary = "client-secret-canary"
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=seed_editor(session),
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                    "access_token": canary,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert canary not in response.text
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


async def test_saved_connection_denies_cross_tenant_intake_without_connector(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_calls = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal connector_calls
        connector_calls += 1
        return FakeMCPConnector()

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            owner_headers = seed_editor(session)
            other_headers = seed_editor(session)
            created = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=owner_headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            response = await client.post(
                f"/api/v1/integrations/mcp/connections/{created.json()['id']}/resources/intake",
                headers=other_headers,
                json={"resource_uri": "kb://policies/payment"},
            )
            listed = await client.get("/api/v1/integrations/mcp/connections", headers=other_headers)
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert response.status_code == 404
    assert response.json() == {"detail": "MCP connection not found"}
    assert listed.status_code == 200
    assert listed.json() == []
    assert connector_calls == 0


async def test_saved_connection_fails_closed_when_server_credential_is_removed(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_calls = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal connector_calls
        connector_calls += 1
        return FakeMCPConnector()

    monkeypatch.setenv("MCP_CREDENTIAL_KEYS", "knowledge-prod")
    monkeypatch.setenv("MCP_CREDENTIAL_KNOWLEDGE_PROD", "server-owned-secret")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            created = await client.post(
                "/api/v1/integrations/mcp/connections",
                headers=headers,
                json={
                    "name": "Knowledge production",
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "credential_key": "knowledge-prod",
                },
            )
            monkeypatch.delenv("MCP_CREDENTIAL_KNOWLEDGE_PROD")
            response = await client.post(
                f"/api/v1/integrations/mcp/connections/{created.json()['id']}/resources/intake",
                headers=headers,
                json={"resource_uri": "kb://policies/payment"},
            )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert response.status_code == 409
    assert response.json() == {"detail": "MCP connection credential is unavailable"}
    assert connector_calls == 0
    assert session.scalar(select(IngestionRun)) is None
    audits = list(session.scalars(select(IntegrationAudit).order_by(IntegrationAudit.created_at)))
    assert [audit.operation for audit in audits] == [
        "create_connection",
        "intake_saved_resource",
    ]
    assert audits[-1].outcome == "failed"
    assert audits[-1].error_code == "credential_unavailable"
    assert "knowledge-prod" not in str(audits[-1].__dict__)
    assert "server-owned-secret" not in str(audits[-1].__dict__)


async def test_connection_and_resource_listing_use_standard_read_only_methods(
    session: Session,
) -> None:
    connector = FakeMCPConnector()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            credentials = {
                "endpoint_url": "https://knowledge.example.com/mcp",
                "access_token": "request-scoped-secret",
            }
            connected = await client.post(
                "/api/v1/integrations/mcp/test-connection",
                headers=headers,
                json=credentials,
            )
            listed = await client.post(
                "/api/v1/integrations/mcp/resources/list",
                headers=headers,
                json=credentials,
            )
    finally:
        app.dependency_overrides.clear()

    assert connected.status_code == 200
    assert connected.json() == {
        "connected": True,
        "server_info": {"name": "knowledge-mcp", "version": "1.0"},
    }
    assert listed.status_code == 200
    assert listed.json() == {
        "resources": [{"uri": "kb://policies/payment", "name": "Payment Policy"}],
        "next_cursor": None,
    }
    assert connector.list_calls == 1
    audits = list(session.scalars(select(IntegrationAudit).order_by(IntegrationAudit.created_at)))
    assert [item.operation for item in audits] == ["test_connection", "list_resources"]
    assert all(item.outcome == "succeeded" for item in audits)


@pytest.mark.parametrize(
    "resource_uri",
    [
        "kb://policy/item?access_token=must-not-leak",
        "kb://policy/item\u200b",
    ],
)
async def test_resource_listing_rejects_malicious_adapter_output_at_api_boundary(
    session: Session,
    resource_uri: str,
) -> None:
    connector = FakeMCPConnector()
    connector.list_resources = lambda cursor=None: (  # type: ignore[method-assign]
        [
            {
                "uri": resource_uri,
                "name": "Policy",
                "secret": "must-not-leak",
            }
        ],
        None,
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/mcp/resources/list",
                headers=seed_editor(session),
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "MCP operation failed"}
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.operation == "list_resources"
    assert audit.outcome == "failed"
    assert "must-not-leak" not in str(audit.__dict__)


async def test_member_cannot_intake_and_connector_is_not_constructed(session: Session) -> None:
    constructed = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal constructed
        constructed += 1
        return FakeMCPConnector()

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/mcp/resources/intake",
                headers=seed_editor(session, role="member"),
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert constructed == 0
    assert session.scalar(select(Document)) is None


async def test_unapproved_endpoint_is_denied_and_safely_audited_before_factory(
    session: Session,
) -> None:
    constructed = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal constructed
        constructed += 1
        return FakeMCPConnector()

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/mcp/resources/intake",
                headers=seed_editor(session),
                json={
                    "endpoint_url": "https://user:secret@evil.example/mcp?token=leak",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json() == {"detail": "MCP endpoint is not allowed"}
    assert constructed == 0
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "denied"
    assert audit.endpoint == "invalid://invalid/"
    assert "secret" not in str(audit.__dict__)
    assert "evil.example" not in str(audit.__dict__)


async def test_remote_failure_is_sanitized_audited_and_rolls_back_mapping(
    session: Session,
) -> None:
    connector = FakeMCPConnector()
    connector.fail_read = True
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: (
        lambda _endpoint, _token: connector
    )
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/mcp/resources/intake",
                headers=seed_editor(session),
                json={
                    "endpoint_url": "https://knowledge.example.com/mcp",
                    "access_token": "request-scoped-secret",
                    "resource_uri": "kb://policies/payment",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "MCP resource intake failed"}
    assert connector.closed is True
    assert session.scalar(select(Document)) is None
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "failed"
    assert audit.error_code == "connector_error"
    assert "should-not-leak" not in str(audit.__dict__)


async def test_sensitive_or_malformed_resource_uri_is_rejected_before_connector(
    session: Session,
) -> None:
    constructed = 0

    def factory(_: str, __: str) -> FakeMCPConnector:
        nonlocal constructed
        constructed += 1
        return FakeMCPConnector()

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_mcp_connector_factory] = lambda: factory
    app.dependency_overrides[get_allowed_mcp_hosts] = lambda: {"knowledge.example.com"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = seed_editor(session)
            base = {
                "endpoint_url": "https://knowledge.example.com/mcp",
                "access_token": "request-scoped-secret",
            }
            responses = [
                await client.post(
                    "/api/v1/integrations/mcp/resources/intake",
                    headers=headers,
                    json={**base, "resource_uri": uri},
                )
                for uri in (
                    "https://user:password@resource.example/item",
                    "kb://policies/payment?access_token=leak",
                    "kb://policies/payment#access_token=leak",
                    "kb://policies/payment#API.KEY=leak",
                    "kb://policies/payment#client-secret=leak",
                    "kb://policies/payment\nforged",
                    "relative/resource",
                    "kb://policies/payment with spaces",
                )
            ]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses] == [422] * 8
    assert constructed == 0
    assert session.scalar(select(IntegrationAudit)) is None
    assert session.scalar(select(Evidence)) is None


async def test_integration_audit_feed_is_member_readable_scoped_and_sanitized(
    session: Session,
) -> None:
    member_headers = seed_editor(session, role="member")
    other_headers = seed_editor(session)
    member_organization_id = UUID(member_headers["X-Organization-ID"])
    member_workspace_id = UUID(member_headers["X-Workspace-ID"])
    other_organization_id = UUID(other_headers["X-Organization-ID"])
    other_workspace_id = UUID(other_headers["X-Workspace-ID"])
    member_user = session.scalar(select(User).where(User.organization_id == member_organization_id))
    other_user = session.scalar(select(User).where(User.organization_id == other_organization_id))
    assert member_user is not None and other_user is not None

    created_at = datetime.now(UTC) - timedelta(minutes=1)
    visible_audit = IntegrationAudit(
        organization_id=member_organization_id,
        workspace_id=member_workspace_id,
        actor_user_id=member_user.id,
        provider="mcp",
        endpoint="https://secret-host.example/mcp",
        operation="import_resource",
        tool_name="resources/read",
        outcome="succeeded",
        error_code=None,
        error_message=None,
        request_metadata={"resource_uri": "kb://secret/resource"},
        created_at=created_at,
        updated_at=created_at,
    )
    session.add_all(
        [
            visible_audit,
            IntegrationAudit(
                organization_id=member_organization_id,
                workspace_id=member_workspace_id,
                actor_user_id=member_user.id,
                provider="odoo",
                endpoint="https://odoo.example",
                operation="list_records",
                tool_name="search_read",
                outcome="succeeded",
                error_code=None,
                error_message=None,
                request_metadata={},
            ),
            IntegrationAudit(
                organization_id=other_organization_id,
                workspace_id=other_workspace_id,
                actor_user_id=other_user.id,
                provider="mcp",
                endpoint="https://other.example/mcp",
                operation="test_connection",
                tool_name="initialize",
                outcome="succeeded",
                error_code=None,
                error_message=None,
                request_metadata={},
            ),
        ]
    )
    session.commit()

    app.dependency_overrides[get_session] = lambda: session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/integration-audits",
                headers=member_headers,
                params={"provider": "mcp", "limit": 20},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(visible_audit.id),
            "provider": "mcp",
            "operation": "import_resource",
            "tool_name": "resources/read",
            "outcome": "succeeded",
            "error_code": None,
            "error_message": None,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
        }
    ]
