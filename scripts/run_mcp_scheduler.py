"""Dedicated MCP schedule dispatcher and recovery worker.

Required environment variables:
- MCP_SCHEDULER_TOKEN
- MCP_SCHEDULER_ORGANIZATION_ID
- MCP_SCHEDULER_WORKSPACE_ID

Use ``--once`` for supervisors/cron, or run continuously with a bounded poll cadence.
"""

import argparse
import json
import os
import signal
import threading
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from company_brain.api.dependencies import get_principal, require_writer
from company_brain.api.generic_mcp_integrations import (
    dispatch_due_sync_schedules,
    get_allowed_mcp_hosts,
    get_mcp_connector_factory,
    get_mcp_credential_registry,
    get_mcp_sync_session_factory,
    run_scheduler_cycle,
)
from company_brain.db.session import SessionFactory

_STOP = threading.Event()


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _poll_seconds() -> int:
    raw = os.environ.get("MCP_SCHEDULER_POLL_SECONDS", "15")
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError("MCP_SCHEDULER_POLL_SECONDS must be an integer") from error
    if not 1 <= value <= 300:
        raise RuntimeError("MCP_SCHEDULER_POLL_SECONDS must be between 1 and 300")
    return value


def _scheduler_api_url() -> str | None:
    value = os.environ.get("MCP_SCHEDULER_API_URL")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("MCP_SCHEDULER_API_URL must be an HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("MCP_SCHEDULER_API_URL requires HTTPS outside loopback")
    return value.rstrip("/")


def _run_once_via_api(
    api_url: str, token: str, organization_id: UUID, workspace_id: UUID
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": str(organization_id),
        "X-Workspace-ID": str(workspace_id),
    }
    with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as client:
        dispatched_response = client.post(
            f"{api_url}/api/v1/integrations/mcp/scheduler/dispatch-due",
            headers=headers,
        )
        dispatched_response.raise_for_status()
        recovered_response = client.post(
            f"{api_url}/api/v1/integrations/mcp/scheduler/run-cycle",
            headers=headers,
        )
        recovered_response.raise_for_status()
    dispatched = dispatched_response.json()
    recovered = recovered_response.json()
    return {
        "dispatched_count": int(dispatched["dispatched_count"]),
        "attempted_count": int(recovered["attempted_count"]),
        "terminal_count": int(recovered["terminal_count"]),
    }


def run_once() -> dict[str, object]:
    token = _required_environment("MCP_SCHEDULER_TOKEN")
    organization_id = UUID(_required_environment("MCP_SCHEDULER_ORGANIZATION_ID"))
    workspace_id = UUID(_required_environment("MCP_SCHEDULER_WORKSPACE_ID"))
    api_url = _scheduler_api_url()
    if api_url is not None:
        return _run_once_via_api(api_url, token, organization_id, workspace_id)
    credentials = get_mcp_credential_registry()
    allowed_hosts = get_allowed_mcp_hosts()
    factory = get_mcp_connector_factory()
    worker_sessions = get_mcp_sync_session_factory()
    with SessionFactory() as session:
        principal = require_writer(
            get_principal(
                session=session,
                authorization=f"Bearer {token}",
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
        )
        dispatched = dispatch_due_sync_schedules(
            session=session,
            principal=principal,
            credentials=credentials,
            allowed_hosts=allowed_hosts,
        )
        recovered = run_scheduler_cycle(
            session=session,
            principal=principal,
            factory=factory,
            sync_session_factory=worker_sessions,
            allowed_hosts=allowed_hosts,
            credentials=credentials,
        )
    return {
        "dispatched_count": dispatched.dispatched_count,
        "attempted_count": recovered.attempted_count,
        "terminal_count": recovered.terminal_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
    cadence = _poll_seconds()
    while not _STOP.is_set():
        print(json.dumps(run_once(), sort_keys=True), flush=True)
        if arguments.once:
            return
        _STOP.wait(cadence)


if __name__ == "__main__":
    main()
