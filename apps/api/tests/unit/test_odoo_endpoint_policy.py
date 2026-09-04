import socket

import pytest

from company_brain.api.odoo_integrations import (
    OdooEndpointPolicyError,
    pinned_endpoint,
)


def test_public_dns_resolution_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    def public(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", public)

    assert pinned_endpoint("https://odoo.example.com/mcp") == (
        "https://8.8.8.8/mcp",
        "odoo.example.com",
    )


def test_mixed_public_private_dns_resolution_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mixed(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)

    with pytest.raises(OdooEndpointPolicyError, match="non-public"):
        pinned_endpoint("https://odoo.example.com/mcp")
