import ipaddress
import json
import re
import socket
from collections.abc import Callable
from contextlib import suppress
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.api.schemas import EntityRead
from company_brain.config import Settings
from company_brain.db.session import get_session
from company_brain.domain.models import IntegrationAudit
from company_brain.domain.repositories import TenantScope
from company_brain.integrations.odoo.client import OdooMCPClient
from company_brain.integrations.odoo.mapping import (
    MAPPING_FIELDS,
    OdooMappingError,
    extract_mcp_record,
    map_odoo_record,
)
from company_brain.integrations.odoo.persistence import (
    MappingResult,
    persist_odoo_mapping,
)

router = APIRouter(prefix="/api/v1/integrations/odoo", tags=["odoo-integration"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
WriterDependency = Annotated[Principal, Depends(require_writer)]
MODEL_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]{0,99}$"
FIELD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.:]{0,99}$"
DOMAIN_OPERATORS = frozenset(
    {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "ilike", "=like", "=ilike"}
)


def validate_domain(domain: list[list[Any]]) -> list[list[Any]]:
    for clause in domain:
        if (
            len(clause) != 3
            or not isinstance(clause[0], str)
            or re.fullmatch(MODEL_PATTERN, clause[0]) is None
            or not isinstance(clause[1], str)
            or clause[1] not in DOMAIN_OPERATORS
        ):
            raise ValueError("invalid domain clause")
        value = clause[2]
        stack: list[tuple[Any, int]] = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > 1000 or depth > 20:
                raise ValueError("domain value is too complex")
            if isinstance(item, str):
                if len(item) > 4096:
                    raise ValueError("domain value is too large")
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, dict):
                stack.extend((key, depth + 1) for key in item)
                stack.extend((child, depth + 1) for child in item.values())
    try:
        encoded = json.dumps(domain, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("domain must be strict JSON") from error
    if len(encoded.encode()) > 16 * 1024:
        raise ValueError("domain is too large")
    return domain


class OdooCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr = Field(min_length=8, max_length=4096)


class SearchRequest(OdooCredentials):
    model: str = Field(pattern=MODEL_PATTERN)
    domain: list[list[Any]] = Field(default_factory=list, max_length=50)
    fields: list[str] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=80, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=10000)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, fields: list[str]) -> list[str]:
        if any(re.fullmatch(FIELD_PATTERN, field) is None for field in fields):
            raise ValueError("invalid field name")
        return fields

    @field_validator("domain")
    @classmethod
    def validate_domain_field(cls, domain: list[list[Any]]) -> list[list[Any]]:
        return validate_domain(domain)


class RecordRequest(OdooCredentials):
    fields: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, fields: list[str]) -> list[str]:
        return SearchRequest.validate_fields(fields)


class AggregateRequest(OdooCredentials):
    model: str = Field(pattern=MODEL_PATTERN)
    domain: list[list[Any]] = Field(default_factory=list, max_length=50)
    fields: list[str] = Field(min_length=1, max_length=100)
    groupby: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("fields", "groupby")
    @classmethod
    def validate_fields(cls, fields: list[str]) -> list[str]:
        return SearchRequest.validate_fields(fields)

    @field_validator("domain")
    @classmethod
    def validate_domain_field(cls, domain: list[list[Any]]) -> list[list[Any]]:
        return validate_domain(domain)


class ConnectionResult(BaseModel):
    connected: bool
    server_info: dict[str, Any]


class ToolsResult(BaseModel):
    tools: list[dict[str, Any]]


class ToolResult(BaseModel):
    result: dict[str, Any]


class MappingResultResponse(BaseModel):
    created: bool
    entity: EntityRead
    source_id: UUID
    external_reference_id: UUID


OdooClientFactory = Callable[[str, str], Any]


class OdooEndpointPolicyError(RuntimeError):
    pass


def pinned_endpoint(endpoint: str) -> tuple[str, str]:
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname
    if hostname is None:
        raise OdooEndpointPolicyError("endpoint hostname is missing")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as error:
        raise OdooEndpointPolicyError("endpoint DNS resolution failed") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise OdooEndpointPolicyError("endpoint resolved to a non-public address")
    address = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    address_text = f"[{address}]" if address.version == 6 else str(address)
    return f"https://{address_text}/mcp", hostname


def get_allowed_odoo_hosts() -> set[str]:
    settings = Settings()
    return {host.strip().lower() for host in settings.odoo_allowed_hosts.split(",") if host.strip()}


def get_odoo_client_factory() -> OdooClientFactory:
    def factory(endpoint: str, api_key: str) -> OdooMCPClient:
        connect_url, hostname = pinned_endpoint(endpoint)
        http = httpx.Client(
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return OdooMCPClient(
            connect_url,
            api_key,
            http_client=http,
            owns_http_client=True,
            host_header=hostname,
            server_hostname=hostname,
        )

    return factory


def validated_endpoint(value: str, allowed_hosts: set[str]) -> str:
    endpoint = value
    if any(ord(character) <= 0x1F or ord(character) == 0x7F for character in endpoint):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Odoo MCP endpoint is not allowed",
        )
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or "?" in endpoint
        or "#" in endpoint
        or parsed.path != "/mcp"
        or parsed.port not in (None, 443)
        or parsed.hostname is None
        or parsed.hostname.lower() not in allowed_hosts
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Odoo MCP endpoint is not allowed",
        )
    return endpoint


def sanitized_endpoint(value: str, allowed_hosts: set[str]) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or "invalid"
    except ValueError:
        return "invalid://invalid/"
    if host.lower() not in allowed_hosts:
        return "invalid://invalid/"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "invalid"
    return f"{scheme}://{host}/mcp"


def audit(
    session: Session,
    scope: TenantScope,
    principal: Principal,
    endpoint: str,
    operation: str,
    tool_name: str | None,
    outcome: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    request_metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        IntegrationAudit(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            actor_user_id=principal.user_id,
            provider="odoo",
            endpoint=endpoint,
            operation=operation,
            tool_name=tool_name,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            request_metadata=request_metadata or {},
        )
    )
    session.commit()


def execute(
    credentials: OdooCredentials,
    operation: str,
    tool_name: str | None,
    action: Callable[[Any], Any],
    session: Session,
    scope: TenantScope,
    principal: Principal,
    factory: OdooClientFactory,
    allowed_hosts: set[str],
    request_metadata: dict[str, Any] | None = None,
) -> Any:
    safe_endpoint = sanitized_endpoint(credentials.endpoint_url, allowed_hosts)
    try:
        endpoint = validated_endpoint(credentials.endpoint_url, allowed_hosts)
    except (HTTPException, ValueError) as error:
        session.rollback()
        audit(
            session,
            scope,
            principal,
            safe_endpoint,
            operation,
            tool_name,
            "denied",
            error_code="endpoint_not_allowed",
            error_message="Odoo MCP endpoint is not allowed",
            request_metadata=request_metadata,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Odoo MCP endpoint is not allowed",
        ) from error
    client: Any | None = None
    try:
        client = factory(endpoint, credentials.api_key.get_secret_value())
        try:
            result = action(client)
        except Exception:
            close = getattr(client, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            raise
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except OdooEndpointPolicyError as error:
        session.rollback()
        message = "Odoo MCP endpoint is not allowed"
        audit(
            session,
            scope,
            principal,
            safe_endpoint,
            operation,
            tool_name,
            "denied",
            error_code="endpoint_not_allowed",
            error_message=message,
            request_metadata=request_metadata,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message
        ) from error
    except Exception as error:
        session.rollback()
        message = "Odoo MCP operation failed"
        audit(
            session,
            scope,
            principal,
            endpoint,
            operation,
            tool_name,
            "failed",
            error_code="connector_error",
            error_message=message,
            request_metadata=request_metadata,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message) from error
    audit(
        session,
        scope,
        principal,
        endpoint,
        operation,
        tool_name,
        "succeeded",
        request_metadata=request_metadata,
    )
    return result


FactoryDependency = Annotated[OdooClientFactory, Depends(get_odoo_client_factory)]
AllowedHostsDependency = Annotated[set[str], Depends(get_allowed_odoo_hosts)]


@router.post("/test-connection", response_model=ConnectionResult)
def test_connection(
    payload: OdooCredentials,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ConnectionResult:
    result = execute(
        payload,
        "test_connection",
        None,
        lambda client: client.initialize(),
        session,
        scope,
        principal,
        factory,
        allowed_hosts,
    )
    assert isinstance(result, dict)
    server_info = result.get("serverInfo")
    return ConnectionResult(
        connected=True,
        server_info=server_info if isinstance(server_info, dict) else {},
    )


@router.post("/discover-tools", response_model=ToolsResult)
def discover_tools(
    payload: OdooCredentials,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ToolsResult:
    result = execute(
        payload,
        "discover_tools",
        None,
        lambda client: client.discover_tools(),
        session,
        scope,
        principal,
        factory,
        allowed_hosts,
    )
    assert isinstance(result, list)
    return ToolsResult(tools=result)


@router.post("/search", response_model=ToolResult)
def search_records(
    payload: SearchRequest,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ToolResult:
    arguments = {
        "model": payload.model,
        "domain": payload.domain,
        "fields": payload.fields,
        "limit": payload.limit,
        "offset": payload.offset,
    }
    result = execute(
        payload,
        "search",
        "search_records",
        lambda client: client.call_tool("search_records", arguments),
        session,
        scope,
        principal,
        factory,
        allowed_hosts,
    )
    assert isinstance(result, dict)
    return ToolResult(result=result)


@router.post("/records/{model}/{record_id}", response_model=ToolResult)
def get_record(
    model: Annotated[str, Field(pattern=MODEL_PATTERN)],
    record_id: int,
    payload: RecordRequest,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ToolResult:
    if record_id <= 0:
        raise HTTPException(status_code=422, detail="record_id must be positive")
    arguments = {"model": model, "record_id": record_id, "fields": payload.fields}
    result = execute(
        payload,
        "get",
        "get_record",
        lambda client: client.call_tool("get_record", arguments),
        session,
        scope,
        principal,
        factory,
        allowed_hosts,
    )
    assert isinstance(result, dict)
    return ToolResult(result=result)


@router.post("/map/{model}/{record_id}", response_model=MappingResultResponse)
def map_record(
    model: Annotated[str, Field(pattern=MODEL_PATTERN)],
    record_id: int,
    payload: OdooCredentials,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> MappingResultResponse:
    if record_id <= 0:
        raise HTTPException(status_code=422, detail="record_id must be positive")
    fields = MAPPING_FIELDS.get(model)
    if fields is None:
        session.rollback()
        audit(
            session,
            scope,
            principal,
            sanitized_endpoint(payload.endpoint_url, allowed_hosts),
            "map_record",
            "get_record",
            "denied",
            error_code="unsupported_mapping_model",
            error_message="Odoo model is not supported for mapping",
            request_metadata={"model": model, "record_id": record_id},
        )
        raise HTTPException(status_code=422, detail="Odoo model is not supported for mapping")

    arguments = {"model": model, "record_id": record_id, "fields": fields}
    source_uri = sanitized_endpoint(payload.endpoint_url, allowed_hosts)

    def fetch_and_map(client: Any) -> MappingResult:
        remote_result = client.call_tool("get_record", arguments)
        if not isinstance(remote_result, dict):
            raise OdooMappingError("Odoo MCP record result is invalid")
        record = extract_mcp_record(remote_result, record_id)
        dto = map_odoo_record(model, record)
        return persist_odoo_mapping(session, scope, dto, source_uri)

    result = execute(
        payload,
        "map_record",
        "get_record",
        fetch_and_map,
        session,
        scope,
        principal,
        factory,
        allowed_hosts,
        request_metadata={"model": model, "record_id": record_id},
    )
    assert isinstance(result, MappingResult)
    return MappingResultResponse(
        created=result.created,
        entity=EntityRead.model_validate(result.entity),
        source_id=result.source.id,
        external_reference_id=result.external_reference.id,
    )


@router.post("/aggregate", response_model=ToolResult)
def aggregate_records(
    payload: AggregateRequest,
    session: SessionDependency,
    scope: ScopeDependency,
    principal: WriterDependency,
    factory: FactoryDependency,
    allowed_hosts: AllowedHostsDependency,
) -> ToolResult:
    arguments = {
        "model": payload.model,
        "domain": payload.domain,
        "fields": payload.fields,
        "groupby": payload.groupby,
    }
    result = execute(
        payload,
        "aggregate",
        "aggregate_records",
        lambda client: client.call_tool("aggregate_records", arguments),
        session,
        scope,
        principal,
        factory,
        allowed_hosts,
    )
    assert isinstance(result, dict)
    return ToolResult(result=result)
