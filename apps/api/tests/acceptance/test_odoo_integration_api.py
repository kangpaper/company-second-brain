from collections.abc import AsyncIterator
from hashlib import sha256
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from company_brain.api.odoo_integrations import (
    get_allowed_odoo_hosts,
    get_odoo_client_factory,
)
from company_brain.db.session import get_session
from company_brain.domain.models import (
    Entity,
    ExternalReference,
    IntegrationAudit,
    Membership,
    Organization,
    Source,
    User,
    Workspace,
)
from company_brain.main import app


class FakeOdooClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initialize(self) -> dict[str, Any]:
        return {"serverInfo": {"name": "odoo", "version": "19"}}

    def discover_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": "search_records"},
            {"name": "get_record"},
            {"name": "aggregate_records"},
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "get_record" and arguments.get("fields") == [
            "id",
            "name",
            "is_company",
            "customer_rank",
            "supplier_rank",
            "email",
            "phone",
            "vat",
            "active",
            "write_date",
        ]:
            return {
                "structuredContent": {
                    "record": {
                        "id": arguments["record_id"],
                        "name": "Mapped Acme",
                        "is_company": True,
                        "customer_rank": 2,
                    }
                }
            }
        return {"content": [{"type": "text", "text": f"{name} ok"}]}


def scope(session: Session, suffix: str, role: str = "editor") -> dict[str, str]:
    organization = Organization(name=f"Org {suffix}", slug=f"org-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id, name="Main", slug="main", settings={}
    )
    token = f"token-{suffix}"
    user = User(
        organization_id=organization.id,
        email=f"{suffix}@example.com",
        display_name=suffix,
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


@pytest.fixture
async def odoo_client(
    session: Session,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeOdooClient]]:
    fake = FakeOdooClient()

    def override_session() -> AsyncIterator[Session]:
        yield session

    def factory(_: str, __: str) -> FakeOdooClient:
        return fake

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_odoo_client_factory] = lambda: factory
    app.dependency_overrides[get_allowed_odoo_hosts] = lambda: {"odoo.example.com"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, fake
    app.dependency_overrides.clear()


def credentials() -> dict[str, str]:
    return {
        "endpoint_url": "https://odoo.example.com/mcp",
        "api_key": "super-secret-key",
    }


async def test_connection_and_discovery_are_audited_without_credentials(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, _ = odoo_client
    headers = scope(session, "discover")

    connected = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json=credentials(),
    )
    discovered = await client.post(
        "/api/v1/integrations/odoo/discover-tools",
        headers=headers,
        json=credentials(),
    )

    assert connected.status_code == 200
    assert connected.json()["connected"] is True
    assert discovered.status_code == 200
    assert [tool["name"] for tool in discovered.json()["tools"]] == [
        "search_records",
        "get_record",
        "aggregate_records",
    ]
    audits = list(session.scalars(select(IntegrationAudit).order_by(IntegrationAudit.created_at)))
    assert [audit.operation for audit in audits] == ["test_connection", "discover_tools"]
    assert all(audit.outcome == "succeeded" for audit in audits)
    serialized = " ".join(str(audit.__dict__) for audit in audits)
    assert "super-secret-key" not in serialized
    assert all(audit.endpoint == "https://odoo.example.com/mcp" for audit in audits)


@pytest.mark.parametrize(
    ("path", "body", "expected_tool"),
    [
        (
            "/api/v1/integrations/odoo/search",
            {
                **credentials(),
                "model": "res.partner",
                "domain": [["customer_rank", ">", 0]],
                "fields": ["id", "name"],
                "limit": 25,
            },
            "search_records",
        ),
        (
            "/api/v1/integrations/odoo/records/res.partner/42",
            {**credentials(), "fields": ["id", "name"]},
            "get_record",
        ),
        (
            "/api/v1/integrations/odoo/aggregate",
            {
                **credentials(),
                "model": "sale.order",
                "domain": [["state", "=", "sale"]],
                "fields": ["amount_total:sum"],
                "groupby": ["partner_id"],
            },
            "aggregate_records",
        ),
    ],
)
async def test_bounded_read_operations_call_only_read_tools_and_audit(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient],
    session: Session,
    path: str,
    body: dict[str, Any],
    expected_tool: str,
) -> None:
    client, fake = odoo_client
    headers = scope(session, f"read-{expected_tool}")

    response = await client.post(path, headers=headers, json=body)

    assert response.status_code == 200
    assert fake.calls[-1][0] == expected_tool
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.tool_name == expected_tool
    assert audit.outcome == "succeeded"


async def test_map_record_fetches_read_only_and_persists_canonical_provenance(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    headers = scope(session, "map-record")

    response = await client.post(
        "/api/v1/integrations/odoo/map/res.partner/42",
        headers=headers,
        json=credentials(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["entity"]["name"] == "Mapped Acme"
    assert body["entity"]["entity_type"] == "customer"
    assert fake.calls[-1] == (
        "get_record",
        {
            "model": "res.partner",
            "record_id": 42,
            "fields": [
                "id",
                "name",
                "is_company",
                "customer_rank",
                "supplier_rank",
                "email",
                "phone",
                "vat",
                "active",
                "write_date",
            ],
        },
    )
    entity = session.scalar(select(Entity))
    source = session.scalar(select(Source))
    reference = session.scalar(select(ExternalReference))
    audit = session.scalar(select(IntegrationAudit))
    assert entity is not None and source is not None and reference is not None
    assert reference.entity_id == entity.id
    assert reference.source_id == source.id
    assert audit is not None
    assert audit.operation == "map_record"
    assert audit.outcome == "succeeded"
    assert audit.request_metadata == {"model": "res.partner", "record_id": 42}


async def test_map_record_rejects_unsupported_model_before_connector(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    response = await client.post(
        "/api/v1/integrations/odoo/map/x.custom/42",
        headers=scope(session, "map-unknown"),
        json=credentials(),
    )

    assert response.status_code == 422
    assert fake.calls == []
    assert session.scalar(select(Entity)) is None


async def test_map_record_is_idempotent(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, _ = odoo_client
    headers = scope(session, "map-idempotent")

    first = await client.post(
        "/api/v1/integrations/odoo/map/res.partner/42",
        headers=headers,
        json=credentials(),
    )
    second = await client.post(
        "/api/v1/integrations/odoo/map/res.partner/42",
        headers=headers,
        json=credentials(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["entity"]["id"] == second.json()["entity"]["id"]
    assert len(list(session.scalars(select(Entity)))) == 1
    assert len(list(session.scalars(select(Source)))) == 1
    assert len(list(session.scalars(select(ExternalReference)))) == 1


async def test_map_record_malformed_remote_payload_rolls_back_canonical_writes(
    session: Session,
) -> None:
    class MalformedClient(FakeOdooClient):
        def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, arguments))
            return {"structuredContent": {"record": {"id": 42, "name": "   "}}}

    fake = MalformedClient()

    def override_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_odoo_client_factory] = lambda: (
        lambda _endpoint, _key: fake
    )
    app.dependency_overrides[get_allowed_odoo_hosts] = lambda: {"odoo.example.com"}
    headers = scope(session, "map-malformed")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/integrations/odoo/map/res.partner/42",
                headers=headers,
                json=credentials(),
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "Odoo MCP operation failed" in response.text
    assert session.scalar(select(Entity)) is None
    assert session.scalar(select(Source)) is None
    assert session.scalar(select(ExternalReference)) is None
    audit_row = session.scalar(select(IntegrationAudit))
    assert audit_row is not None
    assert audit_row.operation == "map_record"
    assert audit_row.outcome == "failed"
    assert audit_row.error_code == "connector_error"


async def test_member_cannot_invoke_odoo_and_invalid_endpoint_is_rejected_before_factory(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    member_headers = scope(session, "member", role="member")
    denied = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=member_headers,
        json=credentials(),
    )
    assert denied.status_code == 403

    writer_headers = scope(session, "endpoint")
    for endpoint in (
        "http://odoo.example.com/mcp",
        "https://user:pass@odoo.example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://evil.example/mcp",
        "https://odoo.example.com/not-mcp",
        "https://odoo.example.com/mcp?secret=value",
    ):
        response = await client.post(
            "/api/v1/integrations/odoo/test-connection",
            headers=writer_headers,
            json={"endpoint_url": endpoint, "api_key": "secret-key"},
        )
        assert response.status_code == 422, endpoint

    assert fake.calls == []


async def test_endpoint_policy_denial_is_audited_without_url_secrets(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    headers = scope(session, "policy-audit")
    response = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json={
            "endpoint_url": "https://user:password@evil.example/mcp?api_key=secret",
            "api_key": "request-secret-key",
        },
    )

    assert response.status_code == 422
    assert fake.calls == []
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "denied"
    assert audit.error_code == "endpoint_not_allowed"
    assert audit.endpoint == "invalid://invalid/"
    serialized = str(audit.__dict__)
    assert "password" not in serialized
    assert "secret" not in serialized


async def test_rejected_hostname_is_not_persisted(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    headers = scope(session, "secret-host")
    response = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json={
            "endpoint_url": "https://KEYSECRET.example.com/mcp",
            "api_key": "request-secret-key",
        },
    )
    assert response.status_code == 422
    assert fake.calls == []
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.endpoint == "invalid://invalid/"
    serialized = str(audit.__dict__)
    assert "keysecret" not in serialized.lower()
    assert "request-secret-key" not in serialized


async def test_connector_failure_is_sanitized_and_audited(
    session: Session,
) -> None:
    class FailingClient(FakeOdooClient):
        def initialize(self) -> dict[str, Any]:
            raise RuntimeError("secret remote traceback")

    def override_session() -> AsyncIterator[Session]:
        yield session

    def factory(_: str, __: str) -> FailingClient:
        return FailingClient()

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_odoo_client_factory] = lambda: factory
    app.dependency_overrides[get_allowed_odoo_hosts] = lambda: {"odoo.example.com"}
    headers = scope(session, "error")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/integrations/odoo/test-connection",
                headers=headers,
                json=credentials(),
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "secret" not in response.text
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "failed"
    assert audit.error_code == "connector_error"
    assert audit.error_message == "Odoo MCP operation failed"


async def test_connector_factory_failure_is_sanitized_and_audited(
    session: Session,
) -> None:
    def override_session() -> AsyncIterator[Session]:
        yield session

    def factory(_: str, __: str) -> FakeOdooClient:
        raise RuntimeError("secret client construction traceback")

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_odoo_client_factory] = lambda: factory
    app.dependency_overrides[get_allowed_odoo_hosts] = lambda: {"odoo.example.com"}
    headers = scope(session, "factory-error")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/integrations/odoo/test-connection",
                headers=headers,
                json=credentials(),
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert "secret" not in response.text
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "failed"
    assert audit.error_code == "connector_error"


@pytest.mark.parametrize(
    "field_patch",
    [{"limit": "200"}, {"offset": "10000"}, {"limit": True}],
)
async def test_search_integer_bounds_are_strict(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient],
    session: Session,
    field_patch: dict[str, Any],
) -> None:
    client, fake = odoo_client
    headers = scope(session, f"strict-{len(str(field_patch))}")
    response = await client.post(
        "/api/v1/integrations/odoo/search",
        headers=headers,
        json={**credentials(), "model": "res.partner", "fields": ["id"], **field_patch},
    )
    assert response.status_code == 422
    assert fake.calls == []


async def test_deeply_nested_domain_is_controlled_validation_error(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    headers = scope(session, "nested-domain")
    value: Any = "leaf"
    for _ in range(100):
        value = [value]
    response = await client.post(
        "/api/v1/integrations/odoo/search",
        headers=headers,
        json={
            **credentials(),
            "model": "res.partner",
            "domain": [["id", "in", value]],
            "fields": ["id"],
        },
    )
    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://odoo.example.com/mcp/",
        "https://odoo.example.com/mcp?",
        "https://odoo.example.com/mcp#",
        "https://odoo.example.com:444/mcp",
    ],
)
async def test_endpoint_must_be_exact_raw_mcp_url(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient],
    session: Session,
    endpoint: str,
) -> None:
    client, fake = odoo_client
    headers = scope(session, f"exact-{len(endpoint)}-{ord(endpoint[-1])}")
    response = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json={"endpoint_url": endpoint, "api_key": "secret-key"},
    )
    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://odoo.example.com:abc/mcp",
        "https://odoo.example.com:99999/mcp",
        "https://[not-an-ipv6/mcp",
    ],
)
async def test_malformed_endpoint_authority_is_denied_and_audited(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient],
    session: Session,
    endpoint: str,
) -> None:
    client, fake = odoo_client
    headers = scope(session, f"malformed-{len(endpoint)}")
    response = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json={"endpoint_url": endpoint, "api_key": "secret-key"},
    )
    assert response.status_code == 422
    assert fake.calls == []
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "denied"
    assert audit.error_code == "endpoint_not_allowed"
    assert "secret-key" not in str(audit.__dict__)


@pytest.mark.parametrize("control", ["\n", "\t", "\r", "\x00", "\x1f", "\x7f"])
async def test_endpoint_ascii_controls_are_denied_before_connector(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient],
    session: Session,
    control: str,
) -> None:
    client, fake = odoo_client
    headers = scope(session, f"control-{ord(control)}")
    response = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json={
            "endpoint_url": f"https://odoo.example.com/mcp{control}",
            "api_key": "secret-key",
        },
    )
    assert response.status_code == 422
    assert fake.calls == []
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "denied"


async def test_rejected_endpoint_path_is_not_persisted(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient], session: Session
) -> None:
    client, fake = odoo_client
    headers = scope(session, "secret-path")
    response = await client.post(
        "/api/v1/integrations/odoo/test-connection",
        headers=headers,
        json={
            "endpoint_url": "https://odoo.example.com/KEYSECRET",
            "api_key": "secret-key",
        },
    )
    assert response.status_code == 422
    assert fake.calls == []
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.endpoint == "https://odoo.example.com/mcp"
    assert "KEYSECRET" not in str(audit.__dict__)
    assert "secret-key" not in str(audit.__dict__)


async def test_unexpected_action_and_close_failures_are_sanitized_and_audited(
    session: Session,
) -> None:
    class UnexpectedClient(FakeOdooClient):
        def initialize(self) -> dict[str, Any]:
            raise TypeError("secret unexpected action")

        def close(self) -> None:
            raise AssertionError("secret close failure")

    def override_session() -> AsyncIterator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_odoo_client_factory] = lambda: (
        lambda _target, _key: UnexpectedClient()
    )
    app.dependency_overrides[get_allowed_odoo_hosts] = lambda: {"odoo.example.com"}
    headers = scope(session, "unexpected-errors")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/integrations/odoo/test-connection",
                headers=headers,
                json=credentials(),
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502
    assert "secret" not in response.text
    audit = session.scalar(select(IntegrationAudit))
    assert audit is not None
    assert audit.outcome == "failed"


@pytest.mark.parametrize(
    "domain",
    [
        [["name", "exec", "Acme"]],
        [["name", "=", "x" * 5000]],
        [["name", "="]],
        [["invalid field!", "=", "Acme"]],
    ],
)
async def test_search_rejects_unbounded_or_invalid_domain_before_connector(
    odoo_client: tuple[httpx.AsyncClient, FakeOdooClient],
    session: Session,
    domain: list[list[Any]],
) -> None:
    client, fake = odoo_client
    headers = scope(session, f"domain-{len(str(domain))}")
    response = await client.post(
        "/api/v1/integrations/odoo/search",
        headers=headers,
        json={
            **credentials(),
            "model": "res.partner",
            "domain": domain,
            "fields": ["id"],
        },
    )

    assert response.status_code == 422
    assert fake.calls == []
