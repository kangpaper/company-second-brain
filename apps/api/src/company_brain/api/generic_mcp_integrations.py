import ipaddress
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_principal, require_writer
from company_brain.api.ingestions import IngestionRead, ingestion_read
from company_brain.config import Settings, load_mcp_credential_registry
from company_brain.db.session import SessionFactory, get_session
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
    MCPSyncScheduleResource,
    Source,
    utc_now,
)
from company_brain.ingestion.service import IntakeInput, IntakeProcessingError, stage_intake
from company_brain.integrations.mcp.adapter import (
    MCPResourceContent,
    ReadOnlyMCPAdapter,
    has_disallowed_unicode,
    project_mcp_resource_descriptor,
    validate_mcp_resource_uri,
)
from company_brain.integrations.mcp.client import MCPClient

router = APIRouter(prefix="/api/v1/integrations/mcp", tags=["mcp-integration"])


class MCPRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    access_token: SecretStr = Field(min_length=8, max_length=4096)


class ImportResourceRequest(MCPRequest):
    resource_uri: str = Field(min_length=1, max_length=2048)

    @field_validator("resource_uri")
    @classmethod
    def validate_resource_uri(cls, value: str) -> str:
        return validate_mcp_resource_uri(value)


class ConnectionResponse(BaseModel):
    connected: bool
    server_info: dict[str, Any]


class ResourcesResponse(BaseModel):
    resources: list[dict[str, Any]]
    next_cursor: str | None


class MCPConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=200)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    credential_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if has_disallowed_unicode(value):
            raise ValueError("MCP connection name is invalid")
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("MCP connection name is invalid")
        return normalized


class SavedResourceIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_uri: str = Field(min_length=1, max_length=2048)

    @field_validator("resource_uri")
    @classmethod
    def validate_resource_uri(cls, value: str) -> str:
        return validate_mcp_resource_uri(value)


class MCPConnectionRead(BaseModel):
    id: UUID
    name: str
    endpoint_url: str
    enabled: bool
    credential_configured: bool


class MCPDiscoveredResourceRead(BaseModel):
    id: UUID
    connection_id: UUID
    resource_uri: str
    name: str | None
    description: str | None
    mime_type: str | None
    size: int | None
    available: bool


class MCPSyncScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=200)
    interval_seconds: int = Field(ge=300, le=86400)
    resource_uris: list[str] = Field(min_length=1, max_length=16)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if has_disallowed_unicode(value):
            raise ValueError("MCP sync schedule name is invalid")
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("MCP sync schedule name is invalid")
        return normalized

    @field_validator("resource_uris")
    @classmethod
    def validate_resource_uris(cls, values: list[str]) -> list[str]:
        validated = [validate_mcp_resource_uri(value) for value in values]
        if len(set(validated)) != len(validated):
            raise ValueError("MCP sync schedule resources must be unique")
        return validated


class MCPSyncSchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=300, le=86400)
    resource_uris: list[str] | None = Field(default=None, min_length=1, max_length=16)

    @field_validator("resource_uris")
    @classmethod
    def validate_resource_uris(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        validated = [validate_mcp_resource_uri(value) for value in values]
        if len(set(validated)) != len(validated):
            raise ValueError("MCP sync schedule resources must be unique")
        return validated

    @model_validator(mode="after")
    def require_update(self) -> "MCPSyncSchedulePatch":
        if not self.model_fields_set:
            raise ValueError("MCP sync schedule update is empty")
        return self


class MCPSyncScheduleRead(BaseModel):
    id: UUID
    connection_id: UUID
    name: str
    interval_seconds: int
    enabled: bool
    next_due_at: datetime
    resource_uris: list[str]


class MCPSchedulerDispatchRead(BaseModel):
    dispatched_count: int
    sync_run_ids: list[UUID]


class MCPSchedulerCycleRead(BaseModel):
    attempted_count: int
    terminal_count: int
    sync_run_ids: list[UUID]


class MCPSyncRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_uris: list[str] = Field(min_length=1, max_length=16)

    @field_validator("resource_uris")
    @classmethod
    def validate_resource_uris(cls, values: list[str]) -> list[str]:
        validated = [validate_mcp_resource_uri(value) for value in values]
        if len(set(validated)) != len(validated):
            raise ValueError("MCP sync resources must be unique")
        return validated


class MCPSyncItemRead(BaseModel):
    id: UUID
    ordinal: int
    resource_uri: str
    status: str
    attempt_count: int
    ingestion_run_id: UUID | None
    error_code: str | None


class MCPSyncRunRead(BaseModel):
    id: UUID
    connection_id: UUID
    status: str
    requested_count: int
    completed_count: int
    changed_count: int
    unchanged_count: int
    failed_count: int
    max_concurrency: int
    max_attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    items: list[MCPSyncItemRead]


MCPConnectorFactory = Callable[[str, str], ReadOnlyMCPAdapter]
MCPSyncSessionFactory = Callable[[], Session]


def get_allowed_mcp_hosts() -> set[str]:
    settings = Settings()
    return {host.strip().lower() for host in settings.mcp_allowed_hosts.split(",") if host.strip()}


def get_mcp_credential_registry() -> dict[str, SecretStr]:
    return load_mcp_credential_registry()


def get_mcp_sync_session_factory() -> MCPSyncSessionFactory:
    return SessionFactory


def _pinned_endpoint(endpoint: str) -> tuple[str, str]:
    hostname = urlsplit(endpoint).hostname
    if hostname is None:
        raise ValueError("missing hostname")
    addresses = {
        ipaddress.ip_address(item[4][0])
        for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("non-public MCP endpoint")
    address = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    address_text = f"[{address}]" if address.version == 6 else str(address)
    return f"https://{address_text}/mcp", hostname


def get_mcp_connector_factory() -> MCPConnectorFactory:
    def factory(endpoint: str, access_token: str) -> ReadOnlyMCPAdapter:
        connect_url, hostname = _pinned_endpoint(endpoint)
        http = httpx.Client(
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return MCPClient(
            connect_url,
            access_token,
            http_client=http,
            owns_http_client=True,
            host_header=hostname,
            server_hostname=hostname,
        )

    return factory


def _safe_endpoint(value: str, allowed_hosts: set[str]) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        return "invalid://invalid/"
    if host is None or host.lower() not in allowed_hosts:
        return "invalid://invalid/"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}/mcp"


def _validate_endpoint(value: str, allowed_hosts: set[str]) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            not any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value)
            and parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and "?" not in value
            and "#" not in value
            and parsed.path == "/mcp"
            and parsed.port in (None, 443)
            and parsed.hostname is not None
            and parsed.hostname.lower() in allowed_hosts
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="MCP endpoint is not allowed",
        )
    return value


def _add_audit(
    session: Session,
    principal: Principal,
    endpoint: str,
    outcome: str,
    *,
    operation: str = "import_resource",
    tool_name: str | None = "resources/read",
    error_code: str | None = None,
    error_message: str | None = None,
    resource_uri: str | None = None,
) -> None:
    session.add(
        IntegrationAudit(
            organization_id=principal.scope.organization_id,
            workspace_id=principal.scope.workspace_id,
            actor_user_id=principal.user_id,
            provider="mcp",
            endpoint=endpoint,
            operation=operation,
            tool_name=tool_name,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            request_metadata=(
                {"resource_uri_hash": sha256(resource_uri.encode()).hexdigest()}
                if resource_uri is not None
                else {}
            ),
        )
    )


def _canonical_title(content: MCPResourceContent) -> str:
    if not isinstance(content.name, str) or has_disallowed_unicode(content.name):
        raise ValueError("MCP resource title is invalid")
    title = " ".join(content.name.split())
    if not title or len(title) > 500:
        raise ValueError("MCP resource title is invalid")
    return title


def _mcp_instance_source(
    session: Session,
    principal: Principal,
    endpoint: str,
) -> Source:
    scope = principal.scope
    source = session.scalar(
        select(Source).where(
            Source.organization_id == scope.organization_id,
            Source.workspace_id == scope.workspace_id,
            Source.source_type == "mcp_instance",
            Source.uri == endpoint,
        )
    )
    if source is None:
        source = Source(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            source_type="mcp_instance",
            uri=endpoint,
            metadata_={"source_system": "mcp"},
        )
        session.add(source)
        session.flush()
    return source


SessionDependency = Annotated[Session, Depends(get_session)]
PrincipalDependency = Annotated[Principal, Depends(get_principal)]
WriterDependency = Annotated[Principal, Depends(require_writer)]
FactoryDependency = Annotated[MCPConnectorFactory, Depends(get_mcp_connector_factory)]
SyncSessionFactoryDependency = Annotated[
    MCPSyncSessionFactory, Depends(get_mcp_sync_session_factory)
]
AllowedHostsDependency = Annotated[set[str], Depends(get_allowed_mcp_hosts)]
CredentialRegistryDependency = Annotated[dict[str, SecretStr], Depends(get_mcp_credential_registry)]


def _list_resource_descriptors(
    connector: ReadOnlyMCPAdapter,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    resources, next_cursor = connector.list_resources(cursor)
    if (
        not isinstance(resources, list)
        or len(resources) > 200
        or (
            next_cursor is not None
            and (not isinstance(next_cursor, str) or len(next_cursor) > 2048)
        )
    ):
        raise ValueError("MCP resource list is invalid")
    descriptors = [project_mcp_resource_descriptor(item) for item in resources]
    uris = [descriptor["uri"] for descriptor in descriptors]
    if len(set(uris)) != len(uris):
        raise ValueError("MCP resource list is invalid")
    return descriptors, next_cursor


def _run_read_operation(
    payload: MCPRequest,
    operation: str,
    tool_name: str | None,
    action: Callable[[ReadOnlyMCPAdapter], Any],
    session: Session,
    principal: Principal,
    factory: MCPConnectorFactory,
    allowed_hosts: set[str],
) -> Any:
    safe_endpoint = _safe_endpoint(payload.endpoint_url, allowed_hosts)
    try:
        endpoint = _validate_endpoint(payload.endpoint_url, allowed_hosts)
    except HTTPException:
        session.rollback()
        _add_audit(
            session,
            principal,
            safe_endpoint,
            "denied",
            operation=operation,
            tool_name=tool_name,
            error_code="endpoint_not_allowed",
            error_message="MCP endpoint is not allowed",
        )
        session.commit()
        raise
    connector: ReadOnlyMCPAdapter | None = None
    try:
        connector = factory(endpoint, payload.access_token.get_secret_value())
        result = action(connector)
        connector.close()
        connector = None
        _add_audit(
            session,
            principal,
            endpoint,
            "succeeded",
            operation=operation,
            tool_name=tool_name,
        )
        session.commit()
        return result
    except Exception as error:
        if connector is not None:
            with suppress(Exception):
                connector.close()
        session.rollback()
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation=operation,
            tool_name=tool_name,
            error_code="connector_error",
            error_message="MCP operation failed",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MCP operation failed",
        ) from error


@router.post("/test-connection", response_model=ConnectionResponse)
def test_connection(
    payload: MCPRequest,
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ConnectionResponse:
    initialized = _run_read_operation(
        payload,
        "test_connection",
        None,
        lambda connector: connector.initialize(),
        session,
        principal,
        factory,
        allowed_hosts,
    )
    server_info = initialized.get("serverInfo") if isinstance(initialized, dict) else None
    return ConnectionResponse(
        connected=True,
        server_info=server_info if isinstance(server_info, dict) else {},
    )


@router.post("/resources/list", response_model=ResourcesResponse)
def list_resources(
    payload: MCPRequest,
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ResourcesResponse:
    resources, next_cursor = _run_read_operation(
        payload,
        "list_resources",
        "resources/list",
        _list_resource_descriptors,
        session,
        principal,
        factory,
        allowed_hosts,
    )
    return ResourcesResponse(resources=resources, next_cursor=next_cursor)


def _connection_read(
    connection: MCPConnection,
    source: Source,
    credentials: dict[str, SecretStr],
) -> MCPConnectionRead:
    return MCPConnectionRead(
        id=connection.id,
        name=connection.name,
        endpoint_url=source.uri,
        enabled=connection.enabled,
        credential_configured=connection.credential_key in credentials,
    )


def _discovered_resource_read(
    resource: MCPDiscoveredResource,
) -> MCPDiscoveredResourceRead:
    return MCPDiscoveredResourceRead(
        id=resource.id,
        connection_id=resource.connection_id,
        resource_uri=resource.resource_uri,
        name=resource.name,
        description=resource.description,
        mime_type=resource.mime_type,
        size=resource.size,
        available=resource.available,
    )


def _sync_run_read(session: Session, sync_run: MCPSyncRun) -> MCPSyncRunRead:
    items = list(
        session.scalars(
            select(MCPSyncItem)
            .where(
                MCPSyncItem.organization_id == sync_run.organization_id,
                MCPSyncItem.workspace_id == sync_run.workspace_id,
                MCPSyncItem.sync_run_id == sync_run.id,
            )
            .order_by(MCPSyncItem.ordinal, MCPSyncItem.id)
            .limit(16)
        )
    )
    return MCPSyncRunRead(
        id=sync_run.id,
        connection_id=sync_run.connection_id,
        status=sync_run.status,
        requested_count=sync_run.requested_count,
        completed_count=sync_run.completed_count,
        changed_count=sync_run.changed_count,
        unchanged_count=sync_run.unchanged_count,
        failed_count=sync_run.failed_count,
        max_concurrency=sync_run.max_concurrency,
        max_attempts=sync_run.max_attempts,
        started_at=sync_run.started_at,
        finished_at=sync_run.finished_at,
        items=[
            MCPSyncItemRead(
                id=item.id,
                ordinal=item.ordinal,
                resource_uri=item.resource_uri,
                status=item.status,
                attempt_count=item.attempt_count,
                ingestion_run_id=item.ingestion_run_id,
                error_code=item.error_code,
            )
            for item in items
        ],
    )


def _scoped_connection(
    session: Session,
    principal: Principal,
    connection_id: UUID,
    *,
    for_update: bool = False,
) -> tuple[MCPConnection, Source]:
    statement = (
        select(MCPConnection, Source)
        .join(
            Source,
            (Source.organization_id == MCPConnection.organization_id)
            & (Source.workspace_id == MCPConnection.workspace_id)
            & (Source.id == MCPConnection.source_id),
        )
        .where(
            MCPConnection.organization_id == principal.scope.organization_id,
            MCPConnection.workspace_id == principal.scope.workspace_id,
            MCPConnection.id == connection_id,
            Source.source_type == "mcp_instance",
        )
    )
    if for_update:
        # Intake rows can already hold FK KEY SHARE locks on these records.
        # NO KEY UPDATE still excludes concurrent authority mutations/deletes
        # without deadlocking sibling resource commits on those FK locks.
        statement = statement.execution_options(populate_existing=True).with_for_update(
            of=(MCPConnection, Source), key_share=True
        )
    row = session.execute(statement).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP connection not found",
        )
    return row[0], row[1]


def _lock_resource_checkpoint(
    session: Session,
    principal: Principal,
    connection: MCPConnection,
    resource_uri: str,
) -> MCPResourceCheckpoint:
    resource_uri_hash = sha256(resource_uri.encode("utf-8")).hexdigest()
    if session.get_bind().dialect.name == "postgresql":
        lock_material = (
            f"{principal.scope.organization_id}:{principal.scope.workspace_id}:"
            f"{connection.id}:{resource_uri_hash}"
        ).encode()
        lock_key = int.from_bytes(sha256(lock_material).digest()[:8], "big", signed=True)
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
    checkpoint = session.scalar(
        select(MCPResourceCheckpoint)
        .where(
            MCPResourceCheckpoint.organization_id == principal.scope.organization_id,
            MCPResourceCheckpoint.workspace_id == principal.scope.workspace_id,
            MCPResourceCheckpoint.connection_id == connection.id,
            MCPResourceCheckpoint.resource_uri_hash == resource_uri_hash,
        )
        .with_for_update()
    )
    if checkpoint is None:
        checkpoint = MCPResourceCheckpoint(
            organization_id=principal.scope.organization_id,
            workspace_id=principal.scope.workspace_id,
            connection_id=connection.id,
            source_id=connection.source_id,
            resource_uri=resource_uri,
            resource_uri_hash=resource_uri_hash,
        )
        session.add(checkpoint)
        session.flush()
    elif checkpoint.resource_uri != resource_uri:
        raise ValueError("MCP resource identity collision")
    return checkpoint


@router.post(
    "/connections",
    response_model=MCPConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    payload: MCPConnectionCreate,
    session: SessionDependency,
    principal: WriterDependency,
    allowed_hosts: AllowedHostsDependency,
    credentials: CredentialRegistryDependency,
) -> MCPConnectionRead:
    safe_endpoint = _safe_endpoint(payload.endpoint_url, allowed_hosts)
    try:
        endpoint = _validate_endpoint(payload.endpoint_url, allowed_hosts)
    except HTTPException:
        session.rollback()
        _add_audit(
            session,
            principal,
            safe_endpoint,
            "denied",
            operation="create_connection",
            tool_name=None,
            error_code="endpoint_not_allowed",
            error_message="MCP endpoint is not allowed",
        )
        session.commit()
        raise
    if payload.credential_key not in credentials:
        session.rollback()
        _add_audit(
            session,
            principal,
            endpoint,
            "denied",
            operation="create_connection",
            tool_name=None,
            error_code="credential_not_configured",
            error_message="MCP credential is not configured",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="MCP credential is not configured",
        )
    try:
        source = _mcp_instance_source(session, principal, endpoint)
        connection = MCPConnection(
            organization_id=principal.scope.organization_id,
            workspace_id=principal.scope.workspace_id,
            source_id=source.id,
            created_by_user_id=principal.user_id,
            name=payload.name,
            credential_key=payload.credential_key,
            enabled=True,
        )
        session.add(connection)
        session.flush()
        _add_audit(
            session,
            principal,
            endpoint,
            "succeeded",
            operation="create_connection",
            tool_name=None,
        )
        session.commit()
        session.refresh(connection)
        return _connection_read(connection, source, credentials)
    except IntegrityError as error:
        session.rollback()
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation="create_connection",
            tool_name=None,
            error_code="connection_conflict",
            error_message="MCP connection already exists",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection already exists",
        ) from error


@router.get("/connections", response_model=list[MCPConnectionRead])
def list_saved_connections(
    session: SessionDependency,
    principal: PrincipalDependency,
    credentials: CredentialRegistryDependency,
) -> list[MCPConnectionRead]:
    rows = session.execute(
        select(MCPConnection, Source)
        .join(
            Source,
            (Source.organization_id == MCPConnection.organization_id)
            & (Source.workspace_id == MCPConnection.workspace_id)
            & (Source.id == MCPConnection.source_id),
        )
        .where(
            MCPConnection.organization_id == principal.scope.organization_id,
            MCPConnection.workspace_id == principal.scope.workspace_id,
            Source.source_type == "mcp_instance",
        )
        .order_by(MCPConnection.created_at, MCPConnection.id)
        .limit(100)
    ).all()
    return [_connection_read(row[0], row[1], credentials) for row in rows]


@router.post(
    "/connections/{connection_id}/resources/discover",
    response_model=list[MCPDiscoveredResourceRead],
)
def discover_saved_connection_resources(
    connection_id: UUID,
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
    credentials: CredentialRegistryDependency,
    response: Response,
) -> list[MCPDiscoveredResourceRead]:
    connection, source = _scoped_connection(session, principal, connection_id, for_update=True)
    try:
        endpoint = _validate_endpoint(source.uri, allowed_hosts)
    except HTTPException:
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "failed",
            operation="discover_resources",
            tool_name="resources/list",
            error_code="endpoint_not_allowed",
            error_message="MCP discovery rejected",
        )
        session.commit()
        raise
    if not connection.enabled:
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation="discover_resources",
            tool_name="resources/list",
            error_code="connection_disabled",
            error_message="MCP discovery rejected",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection is disabled",
        )
    access_token = credentials.get(connection.credential_key)
    if access_token is None:
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation="discover_resources",
            tool_name="resources/list",
            error_code="credential_unavailable",
            error_message="MCP discovery rejected",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection credential is unavailable",
        )
    claim_now = _lease_now(session)
    if _lease_is_active(connection.discovery_lease_expires_at, claim_now):
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation="discover_resources",
            tool_name="resources/list",
            error_code="discovery_already_running",
            error_message="MCP discovery rejected",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP resource discovery is already running",
        )
    discovery_owner = uuid4()
    cursor_before = connection.discovery_cursor
    cycle_id = connection.discovery_cycle_id or uuid4()
    connection.discovery_lease_owner = discovery_owner
    connection.discovery_lease_expires_at = claim_now + timedelta(seconds=30)
    session.commit()

    connector: ReadOnlyMCPAdapter | None = None
    try:
        connector = factory(endpoint, access_token.get_secret_value())
        descriptors, next_cursor = _list_resource_descriptors(connector, cursor_before)
        connector.close()
        connector = None

        session.expire_all()
        connection, _source = _scoped_connection(session, principal, connection_id, for_update=True)
        existing = list(
            session.scalars(
                select(MCPDiscoveredResource)
                .where(
                    MCPDiscoveredResource.organization_id == principal.scope.organization_id,
                    MCPDiscoveredResource.workspace_id == principal.scope.workspace_id,
                    MCPDiscoveredResource.connection_id == connection.id,
                )
                .order_by(MCPDiscoveredResource.resource_uri_hash)
                .with_for_update()
            )
        )
        now = _lease_now(session)
        if connection.discovery_lease_owner != discovery_owner or not _lease_is_active(
            connection.discovery_lease_expires_at, now
        ):
            session.rollback()
            raise RuntimeError("MCP discovery lease was lost")
        current_endpoint = _require_saved_connection_authority(
            connection, _source, credentials, allowed_hosts
        )
        if current_endpoint != endpoint:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="MCP connection authority changed during discovery",
            )
        by_hash = {resource.resource_uri_hash: resource for resource in existing}
        for descriptor in descriptors:
            resource_uri = descriptor["uri"]
            resource_uri_hash = sha256(resource_uri.encode("utf-8")).hexdigest()
            resource = by_hash.get(resource_uri_hash)
            if resource is None:
                resource = MCPDiscoveredResource(
                    organization_id=principal.scope.organization_id,
                    workspace_id=principal.scope.workspace_id,
                    connection_id=connection.id,
                    source_id=connection.source_id,
                    resource_uri=resource_uri,
                    resource_uri_hash=resource_uri_hash,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_seen_cycle_id=cycle_id,
                )
                session.add(resource)
                by_hash[resource_uri_hash] = resource
            elif resource.resource_uri != resource_uri:
                raise ValueError("MCP resource identity collision")
            resource.name = descriptor.get("name")
            resource.description = descriptor.get("description")
            resource.mime_type = descriptor.get("mimeType")
            resource.size = descriptor.get("size")
            resource.available = True
            resource.last_seen_at = now
            resource.last_seen_cycle_id = cycle_id
        if next_cursor is None:
            for resource in existing:
                if resource.last_seen_cycle_id != cycle_id:
                    resource.available = False
            connection.discovery_cursor = None
            connection.discovery_cycle_id = None
        else:
            connection.discovery_cursor = next_cursor
            connection.discovery_cycle_id = cycle_id
        connection.discovery_lease_owner = None
        connection.discovery_lease_expires_at = None
        _add_audit(
            session,
            principal,
            endpoint,
            "succeeded",
            operation="discover_resources",
            tool_name="resources/list",
        )
        session.commit()
    except HTTPException:
        session.rollback()
        claimed_connection, _source = _scoped_connection(
            session, principal, connection_id, for_update=True
        )
        if claimed_connection.discovery_lease_owner == discovery_owner:
            claimed_connection.discovery_lease_owner = None
            claimed_connection.discovery_lease_expires_at = None
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation="discover_resources",
            tool_name="resources/list",
            error_code="authority_revoked",
            error_message="MCP discovery rejected",
        )
        session.commit()
        raise
    except Exception as error:
        if connector is not None:
            with suppress(Exception):
                connector.close()
        session.rollback()
        claimed_connection, _source = _scoped_connection(
            session, principal, connection_id, for_update=True
        )
        if claimed_connection.discovery_lease_owner == discovery_owner:
            claimed_connection.discovery_lease_owner = None
            claimed_connection.discovery_lease_expires_at = None
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation="discover_resources",
            tool_name="resources/list",
            error_code="connector_error",
            error_message="MCP discovery failed",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MCP discovery failed",
        ) from error

    response.headers["X-MCP-Discovery-Cycle-Complete"] = str(
        connection.discovery_cursor is None
    ).lower()
    resources = list(
        session.scalars(
            select(MCPDiscoveredResource)
            .where(
                MCPDiscoveredResource.organization_id == principal.scope.organization_id,
                MCPDiscoveredResource.workspace_id == principal.scope.workspace_id,
                MCPDiscoveredResource.connection_id == connection.id,
            )
            .order_by(MCPDiscoveredResource.resource_uri, MCPDiscoveredResource.id)
            .limit(200)
        )
    )
    return [_discovered_resource_read(resource) for resource in resources]


@router.get(
    "/connections/{connection_id}/resources",
    response_model=list[MCPDiscoveredResourceRead],
)
def list_saved_connection_resources(
    connection_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> list[MCPDiscoveredResourceRead]:
    connection, _source = _scoped_connection(session, principal, connection_id)
    resources = list(
        session.scalars(
            select(MCPDiscoveredResource)
            .where(
                MCPDiscoveredResource.organization_id == principal.scope.organization_id,
                MCPDiscoveredResource.workspace_id == principal.scope.workspace_id,
                MCPDiscoveredResource.connection_id == connection.id,
            )
            .order_by(MCPDiscoveredResource.resource_uri, MCPDiscoveredResource.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return [_discovered_resource_read(resource) for resource in resources]


def _scoped_schedule(
    session: Session,
    principal: Principal,
    schedule_id: UUID,
    *,
    for_update: bool = False,
) -> MCPSyncSchedule:
    statement = select(MCPSyncSchedule).where(
        MCPSyncSchedule.organization_id == principal.scope.organization_id,
        MCPSyncSchedule.workspace_id == principal.scope.workspace_id,
        MCPSyncSchedule.id == schedule_id,
    )
    if for_update:
        statement = statement.with_for_update()
    schedule = session.scalar(statement)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return schedule


def _schedule_resource_uris(session: Session, schedule: MCPSyncSchedule) -> list[str]:
    return list(
        session.scalars(
            select(MCPSyncScheduleResource.resource_uri)
            .where(
                MCPSyncScheduleResource.organization_id == schedule.organization_id,
                MCPSyncScheduleResource.workspace_id == schedule.workspace_id,
                MCPSyncScheduleResource.schedule_id == schedule.id,
            )
            .order_by(MCPSyncScheduleResource.ordinal)
            .limit(16)
        )
    )


def _schedule_read(session: Session, schedule: MCPSyncSchedule) -> MCPSyncScheduleRead:
    return MCPSyncScheduleRead(
        id=schedule.id,
        connection_id=schedule.connection_id,
        name=schedule.name,
        interval_seconds=schedule.interval_seconds,
        enabled=schedule.enabled,
        next_due_at=schedule.next_due_at,
        resource_uris=_schedule_resource_uris(session, schedule),
    )


def _validate_catalog_selection(
    session: Session,
    principal: Principal,
    connection: MCPConnection,
    resource_uris: list[str],
) -> None:
    selected_hashes = {
        sha256(resource_uri.encode("utf-8")).hexdigest(): resource_uri
        for resource_uri in resource_uris
    }
    resources = list(
        session.scalars(
            select(MCPDiscoveredResource).where(
                MCPDiscoveredResource.organization_id == principal.scope.organization_id,
                MCPDiscoveredResource.workspace_id == principal.scope.workspace_id,
                MCPDiscoveredResource.connection_id == connection.id,
                MCPDiscoveredResource.available.is_(True),
                MCPDiscoveredResource.resource_uri_hash.in_(selected_hashes),
            )
        )
    )
    if len(resources) != len(resource_uris) or any(
        selected_hashes.get(resource.resource_uri_hash) != resource.resource_uri
        for resource in resources
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="MCP sync schedule resources are unavailable",
        )


def _require_saved_connection_authority(
    connection: MCPConnection,
    source: Source,
    credentials: dict[str, SecretStr],
    allowed_hosts: set[str],
) -> str:
    endpoint = _validate_endpoint(source.uri, allowed_hosts)
    if not connection.enabled or connection.credential_key not in credentials:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection is unavailable",
        )
    return endpoint


def _replace_schedule_resources(
    session: Session,
    principal: Principal,
    schedule: MCPSyncSchedule,
    resource_uris: list[str],
) -> None:
    session.execute(
        delete(MCPSyncScheduleResource).where(
            MCPSyncScheduleResource.organization_id == principal.scope.organization_id,
            MCPSyncScheduleResource.workspace_id == principal.scope.workspace_id,
            MCPSyncScheduleResource.schedule_id == schedule.id,
        )
    )
    session.add_all(
        [
            MCPSyncScheduleResource(
                organization_id=principal.scope.organization_id,
                workspace_id=principal.scope.workspace_id,
                schedule_id=schedule.id,
                connection_id=schedule.connection_id,
                source_id=schedule.source_id,
                ordinal=ordinal,
                resource_uri=resource_uri,
                resource_uri_hash=sha256(resource_uri.encode("utf-8")).hexdigest(),
            )
            for ordinal, resource_uri in enumerate(resource_uris)
        ]
    )
    session.flush()


def _new_sync_run(
    session: Session,
    principal: Principal,
    connection: MCPConnection,
    source: Source,
    resource_uris: list[str],
) -> MCPSyncRun:
    sync_run = MCPSyncRun(
        organization_id=principal.scope.organization_id,
        workspace_id=principal.scope.workspace_id,
        connection_id=connection.id,
        source_id=source.id,
        created_by_user_id=principal.user_id,
        status="queued",
        requested_count=len(resource_uris),
        completed_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        max_concurrency=4,
        max_attempts=3,
    )
    session.add(sync_run)
    session.flush()
    session.add_all(
        [
            MCPSyncItem(
                organization_id=principal.scope.organization_id,
                workspace_id=principal.scope.workspace_id,
                sync_run_id=sync_run.id,
                connection_id=connection.id,
                source_id=source.id,
                ordinal=ordinal,
                resource_uri=resource_uri,
                resource_uri_hash=sha256(resource_uri.encode("utf-8")).hexdigest(),
                status="queued",
                attempt_count=0,
                max_attempts=sync_run.max_attempts,
            )
            for ordinal, resource_uri in enumerate(resource_uris)
        ]
    )
    session.flush()
    return sync_run


@router.post(
    "/connections/{connection_id}/schedules",
    response_model=MCPSyncScheduleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_sync_schedule(
    connection_id: UUID,
    payload: MCPSyncScheduleCreate,
    session: SessionDependency,
    principal: WriterDependency,
    credentials: CredentialRegistryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> MCPSyncScheduleRead:
    connection, source = _scoped_connection(session, principal, connection_id, for_update=True)
    try:
        _require_saved_connection_authority(connection, source, credentials, allowed_hosts)
        _validate_catalog_selection(session, principal, connection, payload.resource_uris)
    except HTTPException:
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "failed",
            operation="create_sync_schedule",
            tool_name=None,
            error_code="schedule_unavailable",
            error_message="MCP sync schedule mutation rejected",
        )
        session.commit()
        raise
    now = _lease_now(session)
    schedule = MCPSyncSchedule(
        organization_id=principal.scope.organization_id,
        workspace_id=principal.scope.workspace_id,
        connection_id=connection.id,
        source_id=connection.source_id,
        created_by_user_id=principal.user_id,
        name=payload.name,
        interval_seconds=payload.interval_seconds,
        enabled=True,
        next_due_at=now + timedelta(seconds=payload.interval_seconds),
    )
    session.add(schedule)
    try:
        session.flush()
        _replace_schedule_resources(session, principal, schedule, payload.resource_uris)
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "succeeded",
            operation="create_sync_schedule",
            tool_name=None,
        )
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP sync schedule already exists",
        ) from error
    session.refresh(schedule)
    return _schedule_read(session, schedule)


@router.get(
    "/connections/{connection_id}/schedules",
    response_model=list[MCPSyncScheduleRead],
)
def list_sync_schedules(
    connection_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[MCPSyncScheduleRead]:
    connection, _source = _scoped_connection(session, principal, connection_id)
    schedules = list(
        session.scalars(
            select(MCPSyncSchedule)
            .where(
                MCPSyncSchedule.organization_id == principal.scope.organization_id,
                MCPSyncSchedule.workspace_id == principal.scope.workspace_id,
                MCPSyncSchedule.connection_id == connection.id,
            )
            .order_by(MCPSyncSchedule.created_at, MCPSyncSchedule.id)
            .limit(100)
        )
    )
    return [_schedule_read(session, schedule) for schedule in schedules]


@router.patch("/schedules/{schedule_id}", response_model=MCPSyncScheduleRead)
def update_sync_schedule(
    schedule_id: UUID,
    payload: MCPSyncSchedulePatch,
    session: SessionDependency,
    principal: WriterDependency,
    credentials: CredentialRegistryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> MCPSyncScheduleRead:
    schedule = _scoped_schedule(session, principal, schedule_id, for_update=True)
    connection, source = _scoped_connection(
        session, principal, schedule.connection_id, for_update=True
    )
    pure_disable = payload.model_fields_set == {"enabled"} and payload.enabled is False
    try:
        if not pure_disable:
            _require_saved_connection_authority(connection, source, credentials, allowed_hosts)
        if payload.resource_uris is not None:
            _validate_catalog_selection(session, principal, connection, payload.resource_uris)
    except HTTPException:
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "failed",
            operation="update_sync_schedule",
            tool_name=None,
            error_code="schedule_unavailable",
            error_message="MCP sync schedule mutation rejected",
        )
        session.commit()
        raise
    now = _lease_now(session)
    if payload.resource_uris is not None:
        _replace_schedule_resources(session, principal, schedule, payload.resource_uris)
    if payload.interval_seconds is not None:
        schedule.interval_seconds = payload.interval_seconds
        schedule.next_due_at = now + timedelta(seconds=payload.interval_seconds)
    if payload.enabled is not None:
        if payload.enabled and not schedule.enabled:
            schedule.next_due_at = now + timedelta(seconds=schedule.interval_seconds)
        schedule.enabled = payload.enabled
    _add_audit(
        session,
        principal,
        _safe_endpoint(source.uri, allowed_hosts),
        "succeeded",
        operation="update_sync_schedule",
        tool_name=None,
    )
    session.commit()
    session.refresh(schedule)
    return _schedule_read(session, schedule)


@router.post(
    "/schedules/{schedule_id}/run-now",
    response_model=MCPSyncRunRead,
    status_code=status.HTTP_201_CREATED,
)
def run_sync_schedule_now(
    schedule_id: UUID,
    session: SessionDependency,
    principal: WriterDependency,
    credentials: CredentialRegistryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> MCPSyncRunRead:
    schedule = _scoped_schedule(session, principal, schedule_id, for_update=True)
    connection, source = _scoped_connection(
        session, principal, schedule.connection_id, for_update=True
    )
    if not schedule.enabled:
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "failed",
            operation="run_sync_schedule_now",
            tool_name=None,
            error_code="schedule_disabled",
            error_message="MCP sync schedule run rejected",
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP sync schedule is disabled",
        )
    resource_uris = _schedule_resource_uris(session, schedule)
    try:
        _require_saved_connection_authority(connection, source, credentials, allowed_hosts)
        _validate_catalog_selection(session, principal, connection, resource_uris)
    except HTTPException:
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "failed",
            operation="run_sync_schedule_now",
            tool_name=None,
            error_code="schedule_unavailable",
            error_message="MCP sync schedule run rejected",
        )
        session.commit()
        raise
    sync_run = _new_sync_run(session, principal, connection, source, resource_uris)
    session.add(
        MCPScheduleTick(
            organization_id=principal.scope.organization_id,
            workspace_id=principal.scope.workspace_id,
            schedule_id=schedule.id,
            connection_id=connection.id,
            source_id=source.id,
            sync_run_id=sync_run.id,
            scheduled_for=_lease_now(session),
            trigger="manual",
        )
    )
    _add_audit(
        session,
        principal,
        _safe_endpoint(source.uri, allowed_hosts),
        "succeeded",
        operation="run_sync_schedule_now",
        tool_name=None,
    )
    session.commit()
    session.refresh(sync_run)
    return _sync_run_read(session, sync_run)


def _advance_schedule_due(schedule: MCPSyncSchedule, now: datetime) -> None:
    next_due_at = schedule.next_due_at
    if next_due_at.tzinfo is None:
        next_due_at = next_due_at.replace(tzinfo=UTC)
    elapsed = max(0, int((now - next_due_at).total_seconds()))
    skipped_intervals = elapsed // schedule.interval_seconds + 1
    schedule.next_due_at = next_due_at + timedelta(
        seconds=skipped_intervals * schedule.interval_seconds
    )


@router.post(
    "/scheduler/dispatch-due",
    response_model=MCPSchedulerDispatchRead,
)
def dispatch_due_sync_schedules(
    session: SessionDependency,
    principal: WriterDependency,
    credentials: CredentialRegistryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> MCPSchedulerDispatchRead:
    now = _lease_now(session)
    schedules = list(
        session.scalars(
            select(MCPSyncSchedule)
            .where(
                MCPSyncSchedule.organization_id == principal.scope.organization_id,
                MCPSyncSchedule.workspace_id == principal.scope.workspace_id,
                MCPSyncSchedule.enabled.is_(True),
                MCPSyncSchedule.next_due_at <= now,
            )
            .order_by(MCPSyncSchedule.next_due_at, MCPSyncSchedule.id)
            .limit(4)
            .with_for_update(skip_locked=True)
        )
    )
    dispatched: list[UUID] = []
    for schedule in schedules:
        connection, source = _scoped_connection(
            session, principal, schedule.connection_id, for_update=True
        )
        scheduled_for = schedule.next_due_at
        try:
            _require_saved_connection_authority(connection, source, credentials, allowed_hosts)
            resource_uris = _schedule_resource_uris(session, schedule)
            _validate_catalog_selection(session, principal, connection, resource_uris)
        except HTTPException as error:
            _advance_schedule_due(schedule, now)
            _add_audit(
                session,
                principal,
                _safe_endpoint(source.uri, allowed_hosts),
                "failed",
                operation="dispatch_sync_schedule_skipped",
                tool_name=None,
                error_code=(
                    "schedule_resources_unavailable"
                    if error.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
                    else "connection_unavailable"
                ),
                error_message="MCP sync schedule dispatch skipped",
            )
            continue
        prior_nonterminal_run = session.scalar(
            select(MCPSyncRun.id)
            .join(
                MCPScheduleTick,
                (MCPScheduleTick.organization_id == MCPSyncRun.organization_id)
                & (MCPScheduleTick.workspace_id == MCPSyncRun.workspace_id)
                & (MCPScheduleTick.sync_run_id == MCPSyncRun.id),
            )
            .where(
                MCPScheduleTick.organization_id == principal.scope.organization_id,
                MCPScheduleTick.workspace_id == principal.scope.workspace_id,
                MCPScheduleTick.schedule_id == schedule.id,
                MCPSyncRun.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        _advance_schedule_due(schedule, now)
        if prior_nonterminal_run is not None:
            continue
        sync_run = _new_sync_run(session, principal, connection, source, resource_uris)
        session.add(
            MCPScheduleTick(
                organization_id=principal.scope.organization_id,
                workspace_id=principal.scope.workspace_id,
                schedule_id=schedule.id,
                connection_id=connection.id,
                source_id=source.id,
                sync_run_id=sync_run.id,
                scheduled_for=scheduled_for,
                trigger="interval",
            )
        )
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "succeeded",
            operation="dispatch_sync_schedule",
            tool_name=None,
        )
        dispatched.append(sync_run.id)
    session.commit()
    return MCPSchedulerDispatchRead(dispatched_count=len(dispatched), sync_run_ids=dispatched)


@router.post(
    "/connections/{connection_id}/sync-runs",
    response_model=MCPSyncRunRead,
    status_code=status.HTTP_201_CREATED,
)
def create_sync_run(
    connection_id: UUID,
    payload: MCPSyncRunCreate,
    session: SessionDependency,
    principal: WriterDependency,
    credentials: CredentialRegistryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> MCPSyncRunRead:
    connection, source = _scoped_connection(session, principal, connection_id)
    _validate_endpoint(source.uri, allowed_hosts)
    if not connection.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection is disabled",
        )
    if connection.credential_key not in credentials:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection credential is unavailable",
        )
    sync_run = _new_sync_run(session, principal, connection, source, payload.resource_uris)
    _add_audit(
        session,
        principal,
        source.uri,
        "succeeded",
        operation="create_sync_run",
        tool_name=None,
    )
    session.commit()
    session.refresh(sync_run)
    return _sync_run_read(session, sync_run)


@router.post(
    "/connections/{connection_id}/resources/intake",
    response_model=IngestionRead,
    status_code=status.HTTP_201_CREATED,
)
def intake_saved_resource(
    connection_id: UUID,
    payload: SavedResourceIntakeRequest,
    response: Response,
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
    credentials: CredentialRegistryDependency,
) -> IngestionRead:
    connection, source = _scoped_connection(session, principal, connection_id)
    if not connection.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection is disabled",
        )
    access_token = credentials.get(connection.credential_key)
    if access_token is None:
        session.rollback()
        _add_audit(
            session,
            principal,
            _safe_endpoint(source.uri, allowed_hosts),
            "failed",
            operation="intake_saved_resource",
            tool_name="resources/read",
            error_code="credential_unavailable",
            error_message="MCP connection credential is unavailable",
            resource_uri=payload.resource_uri,
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP connection credential is unavailable",
        )
    return _perform_resource_intake(
        endpoint_url=source.uri,
        access_token=access_token,
        resource_uri=payload.resource_uri,
        operation="intake_saved_resource",
        session=session,
        principal=principal,
        factory=factory,
        allowed_hosts=allowed_hosts,
        checkpoint_connection=connection,
        response=response,
    )


def _perform_resource_intake(
    *,
    endpoint_url: str,
    access_token: SecretStr,
    resource_uri: str,
    operation: str,
    session: Session,
    principal: Principal,
    factory: MCPConnectorFactory,
    allowed_hosts: set[str],
    checkpoint_connection: MCPConnection | None = None,
    response: Response | None = None,
    before_commit: Callable[[], None] | None = None,
) -> IngestionRead:
    safe_endpoint = _safe_endpoint(endpoint_url, allowed_hosts)
    try:
        endpoint = _validate_endpoint(endpoint_url, allowed_hosts)
    except HTTPException:
        session.rollback()
        _add_audit(
            session,
            principal,
            safe_endpoint,
            "denied",
            operation=operation,
            tool_name="resources/read",
            error_code="endpoint_not_allowed",
            error_message="MCP endpoint is not allowed",
            resource_uri=resource_uri,
        )
        session.commit()
        raise

    connector: ReadOnlyMCPAdapter | None = None
    try:
        checkpoint = (
            _lock_resource_checkpoint(
                session,
                principal,
                checkpoint_connection,
                resource_uri,
            )
            if checkpoint_connection is not None
            else None
        )
        connector = factory(endpoint, access_token.get_secret_value())
        connector.initialize()
        content = connector.read_resource(resource_uri)
        if content.uri != resource_uri:
            raise ValueError("MCP resource identity mismatch")
        if content.mime_type not in {"text/plain", "text/markdown"}:
            raise ValueError("MCP resource content is invalid")
        raw_content = content.text.encode("utf-8")
        if len(raw_content) > 2 * 1024 * 1024:
            raise ValueError("MCP resource content is invalid")
        content_hash = sha256(raw_content).hexdigest()
        title = _canonical_title(content)
        checked_at = utc_now()
        if before_commit is not None:
            before_commit()
        if (
            checkpoint is not None
            and checkpoint.content_hash == content_hash
            and checkpoint.ingestion_run_id is not None
        ):
            run = session.scalar(
                select(IngestionRun).where(
                    IngestionRun.organization_id == principal.scope.organization_id,
                    IngestionRun.workspace_id == principal.scope.workspace_id,
                    IngestionRun.source_id == checkpoint.source_id,
                    IngestionRun.id == checkpoint.ingestion_run_id,
                    IngestionRun.content_hash == checkpoint.content_hash,
                    IngestionRun.status == "succeeded",
                )
            )
            if run is None:
                raise ValueError("MCP resource checkpoint is invalid")
            if run.filename == title and run.media_type == content.mime_type:
                checkpoint.updated_at = checked_at
                connector.close()
                connector = None
                _add_audit(
                    session,
                    principal,
                    endpoint,
                    "succeeded",
                    operation=f"{operation}_unchanged",
                    tool_name="resources/read",
                    resource_uri=resource_uri,
                )
                if before_commit is not None:
                    before_commit()
                session.commit()
                session.refresh(run)
                if response is not None:
                    response.status_code = status.HTTP_200_OK
                return ingestion_read(run)
        source = _mcp_instance_source(session, principal, endpoint)
        run = stage_intake(
            session,
            principal.scope,
            IntakeInput(
                source_type="mcp_instance",
                uri=content.uri,
                filename=title,
                media_type=content.mime_type,
            ),
            raw_content,
            source=source,
        )
        if checkpoint is not None:
            checkpoint.source_id = source.id
            checkpoint.content_hash = run.content_hash
            checkpoint.ingestion_run_id = run.id
            checkpoint.ingestion_status = run.status
            checkpoint.last_changed_at = checked_at
            checkpoint.updated_at = checked_at
            session.flush()
        connector.close()
        connector = None
        _add_audit(
            session,
            principal,
            endpoint,
            "succeeded",
            operation=operation,
            tool_name="resources/read",
            resource_uri=resource_uri,
        )
        if before_commit is not None:
            before_commit()
        session.commit()
        session.refresh(run)
        return ingestion_read(run)
    except _SyncCommitAuthorityLost as error:
        if connector is not None:
            with suppress(Exception):
                connector.close()
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP sync item authority was lost",
        ) from error
    except IntakeProcessingError as error:
        if connector is not None:
            with suppress(Exception):
                connector.close()
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation=operation,
            tool_name="resources/read",
            error_code=error.code,
            error_message="MCP resource intake failed",
            resource_uri=resource_uri,
        )
        if before_commit is not None:
            try:
                before_commit()
            except _SyncCommitAuthorityLost as authority_error:
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="MCP sync item authority was lost",
                ) from authority_error
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "run_id": str(error.run.id),
                "code": error.code,
                "message": "MCP resource intake failed",
            },
        ) from error
    except Exception as error:
        if connector is not None:
            with suppress(Exception):
                connector.close()
        session.rollback()
        _add_audit(
            session,
            principal,
            endpoint,
            "failed",
            operation=operation,
            tool_name="resources/read",
            error_code="connector_error",
            error_message="MCP resource intake failed",
            resource_uri=resource_uri,
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MCP resource intake failed",
        ) from error


@router.post(
    "/resources/intake",
    response_model=IngestionRead,
    status_code=status.HTTP_201_CREATED,
)
def intake_resource(
    payload: ImportResourceRequest,
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> IngestionRead:
    return _perform_resource_intake(
        endpoint_url=payload.endpoint_url,
        access_token=payload.access_token,
        resource_uri=payload.resource_uri,
        operation="intake_resource",
        session=session,
        principal=principal,
        factory=factory,
        allowed_hosts=allowed_hosts,
    )


_SYNC_TERMINAL_STATUSES = {"succeeded", "failed"}
_SYNC_ITEM_TERMINAL_STATUSES = {"changed", "unchanged", "failed"}
_SYNC_COORDINATOR_LEASE = timedelta(minutes=5)
_SYNC_ITEM_LEASE = timedelta(minutes=1)


class _SyncCommitAuthorityLost(RuntimeError):
    pass


def _lease_is_active(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now


def _lease_now(session: Session) -> datetime:
    bind = session.get_bind()
    clock = (
        func.clock_timestamp() if bind.dialect.name == "postgresql" else func.current_timestamp()
    )
    now = session.scalar(select(clock))
    if not isinstance(now, datetime):
        raise RuntimeError("Database lease clock is unavailable")
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _scoped_sync_run(
    session: Session,
    principal: Principal,
    sync_run_id: UUID,
    *,
    for_update: bool = False,
) -> MCPSyncRun:
    statement = select(MCPSyncRun).where(
        MCPSyncRun.organization_id == principal.scope.organization_id,
        MCPSyncRun.workspace_id == principal.scope.workspace_id,
        MCPSyncRun.id == sync_run_id,
    )
    if for_update:
        statement = statement.execution_options(populate_existing=True).with_for_update()
    sync_run = session.scalar(statement)
    if sync_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return sync_run


def _claim_sync_coordinator(
    session: Session,
    principal: Principal,
    sync_run_id: UUID,
) -> tuple[MCPSyncRun, UUID | None]:
    sync_run = _scoped_sync_run(
        session,
        principal,
        sync_run_id,
        for_update=True,
    )
    if sync_run.status in _SYNC_TERMINAL_STATUSES:
        return sync_run, None
    now = _lease_now(session)
    if sync_run.status == "running" and _lease_is_active(sync_run.lease_expires_at, now):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP sync run is already executing",
        )
    active_item_id = session.scalar(
        select(MCPSyncItem.id)
        .where(
            MCPSyncItem.organization_id == principal.scope.organization_id,
            MCPSyncItem.workspace_id == principal.scope.workspace_id,
            MCPSyncItem.sync_run_id == sync_run_id,
            MCPSyncItem.status == "running",
            MCPSyncItem.lease_expires_at > now,
        )
        .limit(1)
    )
    if active_item_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MCP sync run is already executing",
        )
    coordinator_owner = uuid4()
    sync_run.status = "running"
    sync_run.lease_owner = coordinator_owner
    sync_run.lease_expires_at = now + _SYNC_COORDINATOR_LEASE
    if sync_run.started_at is None:
        sync_run.started_at = now
    session.commit()
    return sync_run, coordinator_owner


def _claim_sync_item(
    session: Session,
    principal: Principal,
    sync_run_id: UUID,
    item_id: UUID,
    coordinator_owner: UUID,
) -> tuple[MCPSyncItem | None, UUID | None]:
    sync_run = _scoped_sync_run(
        session,
        principal,
        sync_run_id,
        for_update=True,
    )
    coordinator_now = _lease_now(session)
    if (
        sync_run.status != "running"
        or sync_run.lease_owner != coordinator_owner
        or not _lease_is_active(sync_run.lease_expires_at, coordinator_now)
    ):
        raise RuntimeError("MCP sync coordinator lease was lost")
    item = session.scalar(
        select(MCPSyncItem)
        .where(
            MCPSyncItem.organization_id == principal.scope.organization_id,
            MCPSyncItem.workspace_id == principal.scope.workspace_id,
            MCPSyncItem.sync_run_id == sync_run_id,
            MCPSyncItem.id == item_id,
        )
        .with_for_update()
    )
    now = _lease_now(session)
    if (
        sync_run.status != "running"
        or sync_run.lease_owner != coordinator_owner
        or not _lease_is_active(sync_run.lease_expires_at, now)
    ):
        session.rollback()
        raise RuntimeError("MCP sync coordinator lease was lost")
    if item is None:
        raise RuntimeError("MCP sync item is missing")
    if item.status in _SYNC_ITEM_TERMINAL_STATUSES:
        return item, None
    if item.status == "running" and _lease_is_active(item.lease_expires_at, now):
        return None, None
    if item.attempt_count >= item.max_attempts:
        item.status = "failed"
        item.lease_owner = None
        item.lease_expires_at = None
        item.error_code = "lease_expired_after_max_attempts"
        item.error_message = "MCP sync item failed"
        item.finished_at = now
        session.commit()
        return item, None
    item_owner = uuid4()
    item.status = "running"
    item.attempt_count += 1
    item.lease_owner = item_owner
    item.lease_expires_at = now + _SYNC_ITEM_LEASE
    if item.started_at is None:
        item.started_at = now
    session.commit()
    return item, item_owner


def _complete_sync_item(
    session: Session,
    principal: Principal,
    sync_run_id: UUID,
    item_id: UUID,
    item_owner: UUID,
    *,
    result: IngestionRead | None,
    outcome: str,
    error_code: str | None = None,
) -> bool:
    item = session.scalar(
        select(MCPSyncItem)
        .where(
            MCPSyncItem.organization_id == principal.scope.organization_id,
            MCPSyncItem.workspace_id == principal.scope.workspace_id,
            MCPSyncItem.sync_run_id == sync_run_id,
            MCPSyncItem.id == item_id,
        )
        .with_for_update()
    )
    now = _lease_now(session)
    if (
        item is None
        or item.status != "running"
        or item.lease_owner != item_owner
        or not _lease_is_active(item.lease_expires_at, now)
    ):
        session.rollback()
        return False
    item.lease_owner = None
    item.lease_expires_at = None
    if outcome in {"changed", "unchanged"} and result is not None:
        item.status = outcome
        item.ingestion_run_id = result.id
        item.content_hash = result.content_hash
        item.ingestion_status = result.status
        item.finished_at = utc_now()
    elif outcome == "failed" and error_code is not None:
        item.status = "failed"
        item.error_code = error_code
        item.error_message = "MCP sync item failed"
        item.finished_at = utc_now()
    elif outcome == "retry":
        item.status = "queued"
    else:
        session.rollback()
        raise RuntimeError("Invalid MCP sync item completion")
    session.commit()
    return True


def _authorize_sync_item_intake_commit(
    session: Session,
    principal: Principal,
    sync_run_id: UUID,
    item_id: UUID,
    item_owner: UUID,
    coordinator_owner: UUID,
    credentials: dict[str, SecretStr],
    allowed_hosts: set[str],
) -> None:
    sync_run = _scoped_sync_run(session, principal, sync_run_id, for_update=True)
    item = session.scalar(
        select(MCPSyncItem)
        .where(
            MCPSyncItem.organization_id == principal.scope.organization_id,
            MCPSyncItem.workspace_id == principal.scope.workspace_id,
            MCPSyncItem.sync_run_id == sync_run_id,
            MCPSyncItem.id == item_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if item is None:
        session.rollback()
        raise _SyncCommitAuthorityLost("MCP sync item authority was lost")
    connection, source = _scoped_connection(
        session, principal, item.connection_id, for_update=True
    )
    now = _lease_now(session)
    valid = (
        sync_run.status == "running"
        and sync_run.lease_owner == coordinator_owner
        and _lease_is_active(sync_run.lease_expires_at, now)
        and item.status == "running"
        and item.lease_owner == item_owner
        and _lease_is_active(item.lease_expires_at, now)
        and item.connection_id == sync_run.connection_id == connection.id
        and item.source_id == sync_run.source_id == connection.source_id == source.id
    )
    if not valid:
        session.rollback()
        raise _SyncCommitAuthorityLost("MCP sync item authority was lost")
    try:
        _require_saved_connection_authority(connection, source, credentials, allowed_hosts)
    except HTTPException as error:
        session.rollback()
        raise _SyncCommitAuthorityLost("MCP sync item authority was lost") from error


def _process_sync_item(
    *,
    session_factory: MCPSyncSessionFactory,
    principal: Principal,
    sync_run_id: UUID,
    item_id: UUID,
    coordinator_owner: UUID,
    factory: MCPConnectorFactory,
    allowed_hosts: set[str],
    credentials: dict[str, SecretStr],
) -> None:
    while True:
        with session_factory() as worker_session:
            item, item_owner = _claim_sync_item(
                worker_session,
                principal,
                sync_run_id,
                item_id,
                coordinator_owner,
            )
            if item is None or item_owner is None:
                return
            connection, source = _scoped_connection(
                worker_session,
                principal,
                item.connection_id,
            )
            if not connection.enabled:
                _complete_sync_item(
                    worker_session,
                    principal,
                    sync_run_id,
                    item_id,
                    item_owner,
                    result=None,
                    outcome="failed",
                    error_code="connection_disabled",
                )
                return
            access_token = credentials.get(connection.credential_key)
            if access_token is None:
                _complete_sync_item(
                    worker_session,
                    principal,
                    sync_run_id,
                    item_id,
                    item_owner,
                    result=None,
                    outcome="failed",
                    error_code="credential_unavailable",
                )
                return
            intake_response = Response(status_code=status.HTTP_201_CREATED)
            try:
                result = _perform_resource_intake(
                    endpoint_url=source.uri,
                    access_token=access_token,
                    resource_uri=item.resource_uri,
                    operation="sync_resource",
                    session=worker_session,
                    principal=principal,
                    factory=factory,
                    allowed_hosts=allowed_hosts,
                    checkpoint_connection=connection,
                    response=intake_response,
                    before_commit=partial(
                        _authorize_sync_item_intake_commit,
                        worker_session,
                        principal,
                        sync_run_id,
                        item_id,
                        item_owner,
                        coordinator_owner,
                        credentials,
                        allowed_hosts,
                    ),
                )
            except HTTPException as error:
                retryable = error.status_code == status.HTTP_502_BAD_GATEWAY
                if retryable and item.attempt_count < item.max_attempts:
                    completed = _complete_sync_item(
                        worker_session,
                        principal,
                        sync_run_id,
                        item_id,
                        item_owner,
                        result=None,
                        outcome="retry",
                    )
                    if completed:
                        continue
                    return
                error_code = "connector_error" if retryable else "intake_processing_error"
                _complete_sync_item(
                    worker_session,
                    principal,
                    sync_run_id,
                    item_id,
                    item_owner,
                    result=None,
                    outcome="failed",
                    error_code=error_code,
                )
                return
            outcome = (
                "unchanged" if intake_response.status_code == status.HTTP_200_OK else "changed"
            )
            _complete_sync_item(
                worker_session,
                principal,
                sync_run_id,
                item_id,
                item_owner,
                result=result,
                outcome=outcome,
            )
            return


def _finalize_sync_run(
    session: Session,
    principal: Principal,
    sync_run_id: UUID,
    coordinator_owner: UUID,
) -> MCPSyncRun:
    session.expire_all()
    sync_run = _scoped_sync_run(
        session,
        principal,
        sync_run_id,
        for_update=True,
    )
    now = _lease_now(session)
    if (
        sync_run.status != "running"
        or sync_run.lease_owner != coordinator_owner
        or not _lease_is_active(sync_run.lease_expires_at, now)
    ):
        raise RuntimeError("MCP sync coordinator lease was lost")
    items = list(
        session.scalars(
            select(MCPSyncItem)
            .where(
                MCPSyncItem.organization_id == principal.scope.organization_id,
                MCPSyncItem.workspace_id == principal.scope.workspace_id,
                MCPSyncItem.sync_run_id == sync_run_id,
            )
            .order_by(MCPSyncItem.ordinal)
            .with_for_update()
        )
    )
    now = _lease_now(session)
    if (
        sync_run.status != "running"
        or sync_run.lease_owner != coordinator_owner
        or not _lease_is_active(sync_run.lease_expires_at, now)
    ):
        session.rollback()
        raise RuntimeError("MCP sync coordinator lease was lost")
    if len(items) != sync_run.requested_count or any(
        item.status not in _SYNC_ITEM_TERMINAL_STATUSES for item in items
    ):
        raise RuntimeError("MCP sync run has incomplete items")
    sync_run.changed_count = sum(item.status == "changed" for item in items)
    sync_run.unchanged_count = sum(item.status == "unchanged" for item in items)
    sync_run.failed_count = sum(item.status == "failed" for item in items)
    sync_run.completed_count = len(items)
    if sync_run.failed_count == 0:
        sync_run.status = "succeeded"
    else:
        sync_run.status = "failed"
    sync_run.lease_owner = None
    sync_run.lease_expires_at = None
    sync_run.finished_at = utc_now()
    session.commit()
    session.refresh(sync_run)
    return sync_run


@router.get(
    "/sync-runs/{sync_run_id}",
    response_model=MCPSyncRunRead,
)
def get_sync_run(
    sync_run_id: UUID,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> MCPSyncRunRead:
    sync_run = _scoped_sync_run(session, principal, sync_run_id)
    return _sync_run_read(session, sync_run)


@router.post(
    "/sync-runs/{sync_run_id}/execute",
    response_model=MCPSyncRunRead,
)
def execute_sync_run(
    sync_run_id: UUID,
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    sync_session_factory: SyncSessionFactoryDependency,
    allowed_hosts: AllowedHostsDependency,
    credentials: CredentialRegistryDependency,
) -> MCPSyncRunRead:
    sync_run, coordinator_owner = _claim_sync_coordinator(
        session,
        principal,
        sync_run_id,
    )
    if coordinator_owner is None:
        return _sync_run_read(session, sync_run)
    item_ids = list(
        session.scalars(
            select(MCPSyncItem.id)
            .where(
                MCPSyncItem.organization_id == principal.scope.organization_id,
                MCPSyncItem.workspace_id == principal.scope.workspace_id,
                MCPSyncItem.sync_run_id == sync_run_id,
                MCPSyncItem.status.not_in(_SYNC_ITEM_TERMINAL_STATUSES),
            )
            .order_by(MCPSyncItem.ordinal)
            .limit(16)
        )
    )
    try:
        with ThreadPoolExecutor(max_workers=sync_run.max_concurrency) as executor:
            futures = [
                executor.submit(
                    _process_sync_item,
                    session_factory=sync_session_factory,
                    principal=principal,
                    sync_run_id=sync_run_id,
                    item_id=item_id,
                    coordinator_owner=coordinator_owner,
                    factory=factory,
                    allowed_hosts=allowed_hosts,
                    credentials=credentials,
                )
                for item_id in item_ids
            ]
            for future in futures:
                future.result()
        sync_run = _finalize_sync_run(
            session,
            principal,
            sync_run_id,
            coordinator_owner,
        )
    except HTTPException:
        raise
    except Exception as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="MCP sync run execution failed",
        ) from error
    return _sync_run_read(session, sync_run)


@router.post("/scheduler/run-cycle", response_model=MCPSchedulerCycleRead)
def run_scheduler_cycle(
    session: SessionDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    sync_session_factory: SyncSessionFactoryDependency,
    allowed_hosts: AllowedHostsDependency,
    credentials: CredentialRegistryDependency,
) -> MCPSchedulerCycleRead:
    """Execute bounded scheduled work; intended for a dedicated durable worker."""
    now = _lease_now(session)
    candidate_ids = list(
        session.scalars(
            select(MCPSyncRun.id)
            .join(
                MCPScheduleTick,
                (MCPScheduleTick.organization_id == MCPSyncRun.organization_id)
                & (MCPScheduleTick.workspace_id == MCPSyncRun.workspace_id)
                & (MCPScheduleTick.sync_run_id == MCPSyncRun.id),
            )
            .where(
                MCPSyncRun.organization_id == principal.scope.organization_id,
                MCPSyncRun.workspace_id == principal.scope.workspace_id,
                (MCPSyncRun.status == "queued")
                | ((MCPSyncRun.status == "running") & (MCPSyncRun.lease_expires_at <= now)),
            )
            .order_by(MCPScheduleTick.scheduled_for, MCPSyncRun.id)
            .limit(4)
        )
    )
    terminal_count = 0
    attempted_ids: list[UUID] = []
    for sync_run_id in candidate_ids:
        try:
            result = execute_sync_run(
                sync_run_id=sync_run_id,
                session=session,
                principal=principal,
                factory=factory,
                sync_session_factory=sync_session_factory,
                allowed_hosts=allowed_hosts,
                credentials=credentials,
            )
        except HTTPException as error:
            if error.status_code == status.HTTP_409_CONFLICT:
                session.rollback()
                continue
            raise
        attempted_ids.append(sync_run_id)
        if result.status in _SYNC_TERMINAL_STATUSES:
            terminal_count += 1
    return MCPSchedulerCycleRead(
        attempted_count=len(attempted_ids),
        terminal_count=terminal_count,
        sync_run_ids=attempted_ids,
    )
