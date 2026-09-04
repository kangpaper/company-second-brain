from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from unicodedata import category
from urllib.parse import parse_qsl, urlsplit


def has_disallowed_unicode(value: str) -> bool:
    return any(category(character).startswith("C") for character in value)


def validate_mcp_resource_uri(value: str) -> str:
    if (
        not value
        or len(value) > 2048
        or "#" in value
        or has_disallowed_unicode(value)
        or any(character.isspace() for character in value)
    ):
        raise ValueError("MCP resource URI is not allowed")
    try:
        parsed = urlsplit(value)
        query_keys = {
            "".join(character for character in key.casefold() if character.isalnum())
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
    except ValueError as error:
        raise ValueError("MCP resource URI is not allowed") from error
    sensitive_keys = {
        "apikey",
        "accesstoken",
        "authorization",
        "clientsecret",
        "password",
        "secret",
        "token",
    }
    if (
        not parsed.scheme
        or parsed.username is not None
        or parsed.password is not None
        or query_keys & sensitive_keys
    ):
        raise ValueError("MCP resource URI is not allowed")
    return value


def project_mcp_resource_descriptor(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("MCP resource descriptor is invalid")
    uri = item.get("uri")
    name = item.get("name")
    description = item.get("description")
    mime_type = item.get("mimeType")
    size = item.get("size")
    if not isinstance(uri, str):
        raise ValueError("MCP resource descriptor is invalid")
    validate_mcp_resource_uri(uri)
    if (
        name is not None
        and (
            not isinstance(name, str)
            or not name
            or len(name) > 500
            or has_disallowed_unicode(name)
        )
    ) or (
        description is not None
        and (
            not isinstance(description, str)
            or len(description) > 2000
            or has_disallowed_unicode(description)
        )
    ) or (
        mime_type is not None
        and (
            not isinstance(mime_type, str)
            or not mime_type
            or len(mime_type) > 255
            or has_disallowed_unicode(mime_type)
        )
    ) or (
        size is not None
        and (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > 2**63 - 1
        )
    ):
        raise ValueError("MCP resource descriptor is invalid")
    descriptor: dict[str, Any] = {"uri": uri}
    for field_name in ("name", "description", "mimeType", "size"):
        if item.get(field_name) is not None:
            descriptor[field_name] = item[field_name]
    return descriptor


@dataclass(frozen=True)
class MCPResourceContent:
    uri: str
    name: str
    mime_type: str
    text: str


@runtime_checkable
class ReadOnlyMCPAdapter(Protocol):
    """Common provider-neutral boundary for URL-based read-only MCP connectors."""

    def initialize(self) -> dict[str, Any]: ...

    def list_resources(
        self, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def read_resource(self, uri: str) -> MCPResourceContent: ...

    def close(self) -> None: ...
