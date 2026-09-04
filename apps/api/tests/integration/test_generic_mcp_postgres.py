import os
import threading
import time
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from company_brain.api import generic_mcp_integrations as mcp_api
from company_brain.api.dependencies import Principal
from company_brain.api.generic_mcp_integrations import (
    MCPSyncRunRead,
    _claim_sync_coordinator,
    _claim_sync_item,
    _complete_sync_item,
    _finalize_sync_run,
    _lock_resource_checkpoint,
    _perform_resource_intake,
    discover_saved_connection_resources,
    dispatch_due_sync_schedules,
    execute_sync_run,
)
from company_brain.domain.models import (
    IngestionRun,
    MCPConnection,
    MCPDiscoveredResource,
    MCPResourceCheckpoint,
    MCPScheduleTick,
    MCPSyncItem,
    MCPSyncRun,
    MCPSyncSchedule,
    MCPSyncScheduleResource,
    Organization,
    Source,
    User,
    Workspace,
    utc_now,
)
from company_brain.domain.repositories import TenantScope
from company_brain.ingestion.service import IntakeInput, stage_intake
from company_brain.integrations.mcp.adapter import MCPResourceContent

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def _seed_due_schedule(
    session: Session,
) -> tuple[Principal, MCPSyncSchedule]:
    suffix = uuid4().hex
    organization = Organization(name="MCP Schedule DB", slug=f"mcp-schedule-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
        settings={},
    )
    user = User(
        organization_id=organization.id,
        email=f"scheduler-{suffix}@example.com",
        display_name="Scheduler",
    )
    session.add_all([workspace, user])
    session.flush()
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="mcp_instance",
        uri="https://example.com/mcp",
        metadata_={"source_system": "mcp"},
    )
    session.add(source)
    session.flush()
    connection = MCPConnection(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        created_by_user_id=user.id,
        name="Scheduled knowledge",
        credential_key="knowledge-prod",
        enabled=True,
    )
    session.add(connection)
    session.flush()
    resource_uri = f"kb://scheduled/{suffix}"
    resource_hash = sha256(resource_uri.encode()).hexdigest()
    database_now = session.scalar(select(func.clock_timestamp()))
    assert database_now is not None
    session.add(
        MCPDiscoveredResource(
            organization_id=organization.id,
            workspace_id=workspace.id,
            connection_id=connection.id,
            source_id=source.id,
            resource_uri=resource_uri,
            resource_uri_hash=resource_hash,
            name="Scheduled resource",
            available=True,
            first_seen_at=database_now,
            last_seen_at=database_now,
            last_seen_cycle_id=uuid4(),
        )
    )
    schedule = MCPSyncSchedule(
        organization_id=organization.id,
        workspace_id=workspace.id,
        connection_id=connection.id,
        source_id=source.id,
        created_by_user_id=user.id,
        name="Every five minutes",
        interval_seconds=300,
        enabled=True,
        next_due_at=database_now - timedelta(seconds=1),
    )
    session.add(schedule)
    session.flush()
    session.add(
        MCPSyncScheduleResource(
            organization_id=organization.id,
            workspace_id=workspace.id,
            schedule_id=schedule.id,
            connection_id=connection.id,
            source_id=source.id,
            ordinal=0,
            resource_uri=resource_uri,
            resource_uri_hash=resource_hash,
        )
    )
    session.commit()
    principal = Principal(
        user_id=user.id,
        role="editor",
        scope=TenantScope(
            organization_id=organization.id,
            workspace_id=workspace.id,
        ),
    )
    return principal, schedule


def _seed_leased_sync_item(
    session: Session,
    *,
    run_lease_expires_at: datetime,
    item_status: str,
    item_attempt_count: int,
    item_lease_expires_at: datetime | None,
    item_count: int = 1,
) -> tuple[Principal, MCPSyncRun, MCPSyncItem]:
    suffix = uuid4().hex
    organization = Organization(name="MCP Lease DB", slug=f"mcp-lease-{suffix}")
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{suffix}",
        settings={},
    )
    user = User(
        organization_id=organization.id,
        email=f"owner-{suffix}@example.com",
        display_name="Owner",
    )
    session.add_all([workspace, user])
    session.flush()
    source = Source(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_type="mcp_instance",
        uri="https://example.com/mcp",
        metadata_={"source_system": "mcp"},
    )
    session.add(source)
    session.flush()
    connection = MCPConnection(
        organization_id=organization.id,
        workspace_id=workspace.id,
        source_id=source.id,
        created_by_user_id=user.id,
        name="Lease sync",
        credential_key="knowledge-prod",
        enabled=True,
    )
    session.add(connection)
    session.flush()
    run_owner = uuid4()
    sync_run = MCPSyncRun(
        organization_id=organization.id,
        workspace_id=workspace.id,
        connection_id=connection.id,
        source_id=source.id,
        created_by_user_id=user.id,
        status="running",
        requested_count=item_count,
        completed_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        max_concurrency=4,
        max_attempts=3,
        lease_owner=run_owner,
        lease_expires_at=run_lease_expires_at,
        started_at=run_lease_expires_at - timedelta(minutes=5),
    )
    session.add(sync_run)
    session.flush()
    items = [
        MCPSyncItem(
            organization_id=organization.id,
            workspace_id=workspace.id,
            sync_run_id=sync_run.id,
            connection_id=connection.id,
            source_id=source.id,
            ordinal=ordinal,
            resource_uri=f"kb://lease/{suffix}/{ordinal}",
            resource_uri_hash=sha256(f"kb://lease/{suffix}/{ordinal}".encode()).hexdigest(),
            status=item_status,
            attempt_count=item_attempt_count,
            max_attempts=3,
            lease_owner=uuid4() if item_status == "running" else None,
            lease_expires_at=item_lease_expires_at,
            started_at=(
                run_lease_expires_at - timedelta(minutes=5) if item_status == "running" else None
            ),
        )
        for ordinal in range(item_count)
    ]
    session.add_all(items)
    session.commit()
    principal = Principal(
        user_id=user.id,
        role="editor",
        scope=TenantScope(
            organization_id=organization.id,
            workspace_id=workspace.id,
        ),
    )
    return principal, sync_run, items[0]


def test_expired_third_sync_attempt_becomes_failed_without_attempt_four() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, item = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now + timedelta(minutes=4),
            item_status="running",
            item_attempt_count=3,
            item_lease_expires_at=database_now - timedelta(seconds=1),
        )
        claimed_item, item_owner = _claim_sync_item(
            session,
            principal,
            sync_run.id,
            item.id,
            sync_run.lease_owner,
        )
        assert claimed_item is not None
        assert item_owner is None
        assert claimed_item.status == "failed"
        assert claimed_item.attempt_count == 3
        assert claimed_item.error_code == "lease_expired_after_max_attempts"
    engine.dispose()


def test_expired_coordinator_does_not_reclaim_active_item_work() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, _ = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now - timedelta(seconds=1),
            item_status="running",
            item_attempt_count=1,
            item_lease_expires_at=database_now + timedelta(minutes=1),
        )
        original_owner = sync_run.lease_owner
        with pytest.raises(HTTPException) as conflict:
            _claim_sync_coordinator(session, principal, sync_run.id)
        assert conflict.value.status_code == 409
        session.refresh(sync_run)
        assert sync_run.lease_owner == original_owner
        assert sync_run.lease_expires_at < database_now
    engine.dispose()


def test_sync_coordinator_lease_uses_database_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, _ = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now + timedelta(minutes=2),
            item_status="queued",
            item_attempt_count=0,
            item_lease_expires_at=None,
        )
        original_owner = sync_run.lease_owner
        monkeypatch.setattr(
            mcp_api,
            "utc_now",
            lambda: database_now + timedelta(hours=1),
        )
        with pytest.raises(HTTPException) as conflict:
            _claim_sync_coordinator(session, principal, sync_run.id)
        assert conflict.value.status_code == 409
        session.refresh(sync_run)
        assert sync_run.lease_owner == original_owner
    engine.dispose()


def test_sync_run_with_any_failed_item_finalizes_failed() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, _ = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now + timedelta(minutes=4),
            item_status="running",
            item_attempt_count=1,
            item_lease_expires_at=database_now + timedelta(minutes=1),
            item_count=2,
        )
        source = session.get(Source, sync_run.source_id)
        assert source is not None
        ingestion_run = stage_intake(
            session,
            principal.scope,
            IntakeInput(
                source_type="mcp_instance",
                uri="kb://lease/success",
                filename="Success.md",
                media_type="text/markdown",
            ),
            b"# Success\n\nPersisted content.",
            source=source,
        )
        items = list(
            session.scalars(
                select(MCPSyncItem)
                .where(MCPSyncItem.sync_run_id == sync_run.id)
                .order_by(MCPSyncItem.ordinal)
            )
        )
        items[0].status = "changed"
        items[0].lease_owner = None
        items[0].lease_expires_at = None
        items[0].ingestion_run_id = ingestion_run.id
        items[0].content_hash = ingestion_run.content_hash
        items[0].ingestion_status = ingestion_run.status
        items[0].finished_at = database_now
        items[1].status = "failed"
        items[1].lease_owner = None
        items[1].lease_expires_at = None
        items[1].error_code = "connector_error"
        items[1].error_message = "MCP sync item failed"
        items[1].finished_at = database_now
        session.commit()

        finalized = _finalize_sync_run(
            session,
            principal,
            sync_run.id,
            sync_run.lease_owner,
        )
        assert finalized.status == "failed"
        assert finalized.completed_count == 2
        assert finalized.changed_count == 1
        assert finalized.failed_count == 1
    engine.dispose()


def test_expired_item_owner_cannot_complete_work() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, item = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now + timedelta(minutes=4),
            item_status="running",
            item_attempt_count=1,
            item_lease_expires_at=database_now - timedelta(seconds=1),
        )
        expired_owner = item.lease_owner
        assert expired_owner is not None

        completed = _complete_sync_item(
            session,
            principal,
            sync_run.id,
            item.id,
            expired_owner,
            result=None,
            outcome="failed",
            error_code="connector_error",
        )

        assert completed is False
        session.expire_all()
        persisted_item = session.get(MCPSyncItem, item.id)
        assert persisted_item is not None
        assert persisted_item.status == "running"
        assert persisted_item.lease_owner == expired_owner
        assert persisted_item.error_code is None
    engine.dispose()


@pytest.mark.parametrize("loss_mode", ["expired_item", "source_uri"])
def test_stale_item_authority_cannot_commit_network_intake_state(
    monkeypatch: pytest.MonkeyPatch,
    loss_mode: str,
) -> None:
    engine = postgres_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as setup:
        database_now = setup.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, item = _seed_leased_sync_item(
            setup,
            run_lease_expires_at=database_now + timedelta(minutes=4),
            item_status="queued",
            item_attempt_count=0,
            item_lease_expires_at=None,
        )
        coordinator_owner = sync_run.lease_owner
        assert coordinator_owner is not None
        sync_run_id = sync_run.id
        item_id = item.id
        source_id = sync_run.source_id

    entered_read = threading.Event()
    release_read = threading.Event()
    errors: list[BaseException] = []

    class BlockingConnector:
        def initialize(self) -> dict[str, object]:
            return {"capabilities": {"resources": {}}}

        def read_resource(self, uri: str) -> MCPResourceContent:
            entered_read.set()
            assert release_read.wait(timeout=10)
            return MCPResourceContent(
                uri=uri,
                name="Expired lease",
                mime_type="text/markdown",
                text="# Must not commit",
            )

        def close(self) -> None:
            return None

    if loss_mode == "expired_item":
        monkeypatch.setattr(mcp_api, "_SYNC_ITEM_LEASE", timedelta(milliseconds=300))

    def process_item() -> None:
        try:
            mcp_api._process_sync_item(
                session_factory=session_factory,
                principal=principal,
                sync_run_id=sync_run_id,
                item_id=item_id,
                coordinator_owner=coordinator_owner,
                factory=lambda _endpoint, _token: BlockingConnector(),
                allowed_hosts={"example.com"},
                credentials={"knowledge-prod": SecretStr("server-owned-secret")},
            )
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=process_item, daemon=True)
    worker.start()
    assert entered_read.wait(timeout=5), errors
    with session_factory() as observer:
        if loss_mode == "expired_item":
            persisted_item = observer.get(MCPSyncItem, item_id)
            assert persisted_item is not None
            lease_expires_at = persisted_item.lease_expires_at
            assert lease_expires_at is not None
            while True:
                now = observer.scalar(select(func.clock_timestamp()))
                assert now is not None
                if now > lease_expires_at:
                    break
                time.sleep(0.01)
        else:
            source = observer.get(Source, source_id)
            assert source is not None
            source.uri = "https://revoked.invalid/mcp"
            observer.commit()
    release_read.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []

    with session_factory() as verify:
        scope = principal.scope
        assert (
            verify.scalar(
                select(func.count(IngestionRun.id)).where(
                    IngestionRun.organization_id == scope.organization_id,
                    IngestionRun.workspace_id == scope.workspace_id,
                )
            )
            == 0
        )
        assert (
            verify.scalar(
                select(func.count(MCPResourceCheckpoint.id)).where(
                    MCPResourceCheckpoint.organization_id == scope.organization_id,
                    MCPResourceCheckpoint.workspace_id == scope.workspace_id,
                )
            )
            == 0
        )
    engine.dispose()


def test_expired_coordinator_owner_cannot_claim_item() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, item = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now - timedelta(seconds=1),
            item_status="queued",
            item_attempt_count=0,
            item_lease_expires_at=None,
        )
        expired_owner = sync_run.lease_owner
        assert expired_owner is not None

        with pytest.raises(RuntimeError, match="coordinator lease was lost"):
            _claim_sync_item(
                session,
                principal,
                sync_run.id,
                item.id,
                expired_owner,
            )
        session.rollback()
        session.expire_all()
        persisted_item = session.get(MCPSyncItem, item.id)
        assert persisted_item is not None
        assert persisted_item.status == "queued"
        assert persisted_item.attempt_count == 0
        assert persisted_item.lease_owner is None
    engine.dispose()


def test_coordinator_expiring_while_waiting_for_item_lock_cannot_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = postgres_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as seed_session:
        database_now = seed_session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        lease_expires_at = database_now + timedelta(milliseconds=500)
        principal, sync_run, item = _seed_leased_sync_item(
            seed_session,
            run_lease_expires_at=lease_expires_at,
            item_status="queued",
            item_attempt_count=0,
            item_lease_expires_at=None,
        )
        coordinator_owner = sync_run.lease_owner
        assert coordinator_owner is not None

    first_clock_read = threading.Event()
    original_lease_now = mcp_api._lease_now

    def observed_lease_now(session: Session) -> datetime:
        now = original_lease_now(session)
        first_clock_read.set()
        return now

    monkeypatch.setattr(mcp_api, "_lease_now", observed_lease_now)
    errors: list[str] = []

    def claim_item() -> None:
        try:
            with session_factory() as claim_session:
                _claim_sync_item(
                    claim_session,
                    principal,
                    sync_run.id,
                    item.id,
                    coordinator_owner,
                )
        except RuntimeError as error:
            errors.append(str(error))

    with session_factory() as blocker_session:
        blocker_session.scalar(
            select(MCPSyncItem).where(MCPSyncItem.id == item.id).with_for_update()
        )
        claimant = threading.Thread(target=claim_item)
        claimant.start()
        assert first_clock_read.wait(timeout=5)
        while True:
            current_database_time = blocker_session.scalar(select(func.clock_timestamp()))
            assert current_database_time is not None
            if current_database_time > lease_expires_at:
                break
            time.sleep(0.01)
        blocker_session.commit()
        claimant.join(timeout=5)
        assert not claimant.is_alive()

    assert errors == ["MCP sync coordinator lease was lost"]
    with session_factory() as verify_session:
        persisted_item = verify_session.get(MCPSyncItem, item.id)
        assert persisted_item is not None
        assert persisted_item.status == "queued"
        assert persisted_item.attempt_count == 0
        assert persisted_item.lease_owner is None
    engine.dispose()


def test_expired_coordinator_owner_cannot_finalize_run() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        database_now = session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        principal, sync_run, item = _seed_leased_sync_item(
            session,
            run_lease_expires_at=database_now - timedelta(seconds=1),
            item_status="running",
            item_attempt_count=1,
            item_lease_expires_at=database_now - timedelta(seconds=1),
        )
        expired_owner = sync_run.lease_owner
        assert expired_owner is not None
        item.status = "failed"
        item.lease_owner = None
        item.lease_expires_at = None
        item.error_code = "connector_error"
        item.error_message = "MCP sync item failed"
        item.finished_at = database_now
        session.commit()

        with pytest.raises(RuntimeError, match="coordinator lease was lost"):
            _finalize_sync_run(
                session,
                principal,
                sync_run.id,
                expired_owner,
            )
        session.rollback()
        session.expire_all()
        persisted_run = session.get(MCPSyncRun, sync_run.id)
        assert persisted_run is not None
        assert persisted_run.status == "running"
        assert persisted_run.completed_count == 0
        assert persisted_run.finished_at is None
    engine.dispose()


def test_coordinator_expiring_while_waiting_for_item_locks_cannot_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = postgres_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as seed_session:
        database_now = seed_session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        lease_expires_at = database_now + timedelta(milliseconds=500)
        principal, sync_run, item = _seed_leased_sync_item(
            seed_session,
            run_lease_expires_at=lease_expires_at,
            item_status="running",
            item_attempt_count=1,
            item_lease_expires_at=lease_expires_at,
        )
        coordinator_owner = sync_run.lease_owner
        assert coordinator_owner is not None
        item.status = "failed"
        item.lease_owner = None
        item.lease_expires_at = None
        item.error_code = "connector_failure"
        item.error_message = "Connector request failed"
        item.finished_at = database_now
        seed_session.commit()

    first_clock_read = threading.Event()
    original_lease_now = mcp_api._lease_now

    def observed_lease_now(session: Session) -> datetime:
        now = original_lease_now(session)
        first_clock_read.set()
        return now

    monkeypatch.setattr(mcp_api, "_lease_now", observed_lease_now)
    errors: list[str] = []

    def finalize_run() -> None:
        try:
            with session_factory() as finalize_session:
                _finalize_sync_run(
                    finalize_session,
                    principal,
                    sync_run.id,
                    coordinator_owner,
                )
        except RuntimeError as error:
            errors.append(str(error))

    with session_factory() as blocker_session:
        blocker_session.scalar(
            select(MCPSyncItem).where(MCPSyncItem.id == item.id).with_for_update()
        )
        finalizer = threading.Thread(target=finalize_run)
        finalizer.start()
        assert first_clock_read.wait(timeout=5)
        while True:
            current_database_time = blocker_session.scalar(select(func.clock_timestamp()))
            assert current_database_time is not None
            if current_database_time > lease_expires_at:
                break
            time.sleep(0.01)
        blocker_session.commit()
        finalizer.join(timeout=5)
        assert not finalizer.is_alive()

    assert errors == ["MCP sync coordinator lease was lost"]
    with session_factory() as verify_session:
        persisted_run = verify_session.get(MCPSyncRun, sync_run.id)
        assert persisted_run is not None
        assert persisted_run.status == "running"
        assert persisted_run.completed_count == 0
        assert persisted_run.failed_count == 0
        assert persisted_run.finished_at is None
    engine.dispose()


def test_mcp_instance_identity_is_unique_within_tenant_workspace() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        suffix = uuid4().hex
        organization = Organization(name="MCP DB", slug=f"mcp-db-{suffix}")
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
        source_kwargs = {
            "organization_id": organization.id,
            "workspace_id": workspace.id,
            "source_type": "mcp_instance",
            "uri": "https://knowledge.example.com/mcp",
            "metadata_": {"source_system": "mcp"},
        }
        session.add(Source(**source_kwargs))
        session.commit()

        try:
            session.add(Source(**source_kwargs))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.rollback()
            for source in session.query(Source).filter_by(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_type="mcp_instance",
                uri=source_kwargs["uri"],
            ):
                session.delete(source)
            session.commit()


def test_mcp_connection_credential_key_format_is_database_enforced() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        suffix = uuid4().hex
        organization = Organization(name="MCP Connection DB", slug=f"mcp-conn-{suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        session.add_all([workspace, user])
        session.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        session.add(source)
        session.commit()

        try:
            session.add(
                MCPConnection(
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    source_id=source.id,
                    created_by_user_id=user.id,
                    name="Invalid key",
                    credential_key="Bearer plaintext token",
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.query(Source).filter_by(id=source.id).delete()
            session.query(User).filter_by(id=user.id).delete()
            session.query(Workspace).filter_by(id=workspace.id).delete()
            session.query(Organization).filter_by(id=organization.id).delete()
            session.commit()
    engine.dispose()


def test_mcp_connection_rejects_non_mcp_source() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        suffix = uuid4().hex
        organization = Organization(name="MCP Source DB", slug=f"mcp-source-{suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        session.add_all([workspace, user])
        session.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="manual_upload",
            uri=f"upload://{suffix}",
            metadata_={"source_system": "upload"},
        )
        session.add(source)
        session.commit()

        try:
            session.add(
                MCPConnection(
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    source_id=source.id,
                    created_by_user_id=user.id,
                    name="Wrong source",
                    credential_key="knowledge-prod",
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.query(Source).filter_by(id=source.id).delete()
            session.query(User).filter_by(id=user.id).delete()
            session.query(Workspace).filter_by(id=workspace.id).delete()
            session.query(Organization).filter_by(id=organization.id).delete()
            session.commit()
    engine.dispose()


def test_mcp_connection_prevents_source_type_mutation() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        suffix = uuid4().hex
        organization = Organization(name="MCP Durable DB", slug=f"mcp-durable-{suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        session.add_all([workspace, user])
        session.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        session.add(source)
        session.flush()
        connection = MCPConnection(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            created_by_user_id=user.id,
            name="Durable source type",
            credential_key="knowledge-prod",
            enabled=True,
        )
        session.add(connection)
        session.commit()

        try:
            source.source_type = "manual_upload"
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        finally:
            session.query(MCPConnection).filter_by(id=connection.id).delete()
            session.query(Source).filter_by(id=source.id).delete()
            session.query(User).filter_by(id=user.id).delete()
            session.query(Workspace).filter_by(id=workspace.id).delete()
            session.query(Organization).filter_by(id=organization.id).delete()
            session.commit()
    engine.dispose()


def test_mcp_connection_source_type_invariant_is_concurrency_safe() -> None:
    engine = postgres_engine()
    suffix = uuid4().hex
    with Session(engine) as session:
        organization = Organization(name="MCP Race DB", slug=f"mcp-race-{suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        session.add_all([workspace, user])
        session.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://race-{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        session.add(source)
        session.commit()
        organization_id = organization.id
        workspace_id = workspace.id
        user_id = user.id
        source_id = source.id

    worker_ready = threading.Event()
    worker_done = threading.Event()
    worker_result: dict[str, object] = {}

    def insert_connection() -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                worker_result["pid"] = connection.scalar(text("SELECT pg_backend_pid()"))
                worker_ready.set()
                connection.execute(
                    text(
                        "INSERT INTO mcp_connections "
                        "(id, organization_id, workspace_id, source_id, "
                        "created_by_user_id, name, credential_key, enabled, "
                        "created_at, updated_at) "
                        "VALUES (:id, :organization_id, :workspace_id, :source_id, "
                        ":user_id, :name, 'knowledge-prod', true, now(), now())"
                    ),
                    {
                        "id": uuid4(),
                        "organization_id": organization_id,
                        "workspace_id": workspace_id,
                        "source_id": source_id,
                        "user_id": user_id,
                        "name": f"Race {suffix}",
                    },
                )
                transaction.commit()
                worker_result["outcome"] = "committed"
            except DBAPIError as error:
                transaction.rollback()
                worker_result["outcome"] = "rejected"
                worker_result["sqlstate"] = error.orig.sqlstate
            finally:
                worker_ready.set()
                worker_done.set()

    parent_connection = engine.connect()
    parent_transaction = parent_connection.begin()
    worker = threading.Thread(target=insert_connection, daemon=True)
    try:
        parent_connection.execute(
            text(
                "UPDATE sources SET source_type = 'manual_upload' "
                "WHERE organization_id = :organization_id "
                "AND workspace_id = :workspace_id AND id = :source_id"
            ),
            {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "source_id": source_id,
            },
        )
        worker.start()
        assert worker_ready.wait(timeout=5)

        observed_lock_wait = False
        deadline = time.monotonic() + 5
        with engine.connect() as observer:
            while not worker_done.is_set() and time.monotonic() < deadline:
                wait_event_type = observer.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": worker_result["pid"]},
                )
                if wait_event_type == "Lock":
                    observed_lock_wait = True
                    break
                time.sleep(0.01)

        assert worker_done.is_set() or observed_lock_wait
        parent_transaction.commit()
        assert worker_done.wait(timeout=5)

        with engine.connect() as verifier:
            mismatch_count = verifier.scalar(
                text(
                    "SELECT count(*) FROM mcp_connections AS c "
                    "JOIN sources AS s ON s.organization_id = c.organization_id "
                    "AND s.workspace_id = c.workspace_id AND s.id = c.source_id "
                    "WHERE c.source_id = :source_id "
                    "AND s.source_type <> 'mcp_instance'"
                ),
                {"source_id": source_id},
            )
        assert mismatch_count == 0, worker_result
        assert observed_lock_wait, worker_result
        assert worker_result["outcome"] == "rejected"
        assert worker_result["sqlstate"] == "23514"
    finally:
        if parent_transaction.is_active:
            parent_transaction.rollback()
        parent_connection.close()
        worker_done.wait(timeout=5)
        worker.join(timeout=5)
        with Session(engine) as cleanup:
            cleanup.query(MCPConnection).filter_by(source_id=source_id).delete()
            cleanup.query(Source).filter_by(id=source_id).delete()
            cleanup.query(User).filter_by(id=user_id).delete()
            cleanup.query(Workspace).filter_by(id=workspace_id).delete()
            cleanup.query(Organization).filter_by(id=organization_id).delete()
            cleanup.commit()
    engine.dispose()


def test_mcp_resource_checkpoint_target_is_database_enforced() -> None:
    engine = postgres_engine()
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection)
    try:
        suffix = uuid4().hex
        organization = Organization(name="MCP Checkpoint DB", slug=f"mcp-checkpoint-{suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        session.add_all([workspace, user])
        session.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://checkpoint-{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        other_source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://checkpoint-other-{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        session.add_all([source, other_source])
        session.flush()
        saved_connection = MCPConnection(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            created_by_user_id=user.id,
            name="Checkpoint connection",
            credential_key="knowledge-prod",
            enabled=True,
        )
        session.add(saved_connection)
        session.flush()
        scope = TenantScope(
            organization_id=organization.id,
            workspace_id=workspace.id,
        )
        resource_uri = "kb://policies/payment"
        run = stage_intake(
            session,
            scope,
            IntakeInput(
                source_type="mcp_instance",
                uri=resource_uri,
                filename="Payment Policy.md",
                media_type="text/markdown",
            ),
            b"# Payment Policy\n\nInvoices are due in 30 days.",
            source=source,
        )
        checkpoint = MCPResourceCheckpoint(
            organization_id=organization.id,
            workspace_id=workspace.id,
            connection_id=saved_connection.id,
            source_id=source.id,
            resource_uri=resource_uri,
            resource_uri_hash="a" * 64,
            content_hash=run.content_hash,
            ingestion_run_id=run.id,
            ingestion_status=run.status,
            last_changed_at=utc_now(),
        )
        session.add(checkpoint)
        session.flush()

        def assert_rejected(statement: str, parameters: dict[str, object]) -> None:
            savepoint = connection.begin_nested()
            try:
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement), parameters)
            finally:
                savepoint.rollback()

        assert_rejected(
            "UPDATE mcp_resource_checkpoints SET content_hash = :content_hash "
            "WHERE id = :checkpoint_id",
            {"content_hash": "b" * 64, "checkpoint_id": checkpoint.id},
        )
        assert_rejected(
            "UPDATE ingestion_runs SET content_hash = :content_hash WHERE id = :run_id",
            {"content_hash": "b" * 64, "run_id": run.id},
        )
        assert_rejected(
            "UPDATE mcp_connections SET source_id = :source_id WHERE id = :connection_id",
            {"source_id": other_source.id, "connection_id": saved_connection.id},
        )
        assert_rejected(
            "UPDATE mcp_resource_checkpoints "
            "SET resource_uri = 'kb://policies/other', resource_uri_hash = :uri_hash "
            "WHERE id = :checkpoint_id",
            {"uri_hash": "b" * 64, "checkpoint_id": checkpoint.id},
        )
        assert_rejected(
            "UPDATE mcp_resource_checkpoints SET resource_uri_hash = 'invalid' "
            "WHERE id = :checkpoint_id",
            {"checkpoint_id": checkpoint.id},
        )

        valid_count = connection.scalar(
            text(
                "SELECT count(*) FROM mcp_resource_checkpoints AS checkpoint "
                "JOIN mcp_connections AS saved ON "
                "saved.organization_id = checkpoint.organization_id "
                "AND saved.workspace_id = checkpoint.workspace_id "
                "AND saved.id = checkpoint.connection_id "
                "AND saved.source_id = checkpoint.source_id "
                "JOIN ingestion_runs AS run ON "
                "run.organization_id = checkpoint.organization_id "
                "AND run.workspace_id = checkpoint.workspace_id "
                "AND run.source_id = checkpoint.source_id "
                "AND run.id = checkpoint.ingestion_run_id "
                "AND run.content_hash = checkpoint.content_hash "
                "AND run.status = checkpoint.ingestion_status "
                "WHERE checkpoint.id = :checkpoint_id"
            ),
            {"checkpoint_id": checkpoint.id},
        )
        assert valid_count == 1
    finally:
        session.close()
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


def test_mcp_resource_checkpoint_creation_is_concurrency_safe() -> None:
    engine = postgres_engine()
    suffix = uuid4().hex
    with Session(engine) as setup:
        organization = Organization(name="MCP Checkpoint Race", slug=f"checkpoint-race-{suffix}")
        setup.add(organization)
        setup.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        setup.add_all([workspace, user])
        setup.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://checkpoint-race-{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        setup.add(source)
        setup.flush()
        saved_connection = MCPConnection(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            created_by_user_id=user.id,
            name="Checkpoint race",
            credential_key="knowledge-prod",
            enabled=True,
        )
        setup.add(saved_connection)
        setup.commit()
        organization_id = organization.id
        workspace_id = workspace.id
        user_id = user.id
        source_id = source.id
        connection_id = saved_connection.id

    principal = Principal(
        user_id=user_id,
        role="editor",
        scope=TenantScope(
            organization_id=organization_id,
            workspace_id=workspace_id,
        ),
    )
    resource_uri = "kb://policies/payment"
    worker_ready = threading.Event()
    worker_done = threading.Event()
    worker_result: dict[str, object] = {}

    def create_same_checkpoint() -> None:
        with Session(engine) as worker_session:
            try:
                worker_session.execute(text("SET LOCAL statement_timeout = '5s'"))
                worker_result["pid"] = worker_session.scalar(text("SELECT pg_backend_pid()"))
                worker_ready.set()
                worker_connection = worker_session.get(MCPConnection, connection_id)
                assert worker_connection is not None
                worker_checkpoint = _lock_resource_checkpoint(
                    worker_session,
                    principal,
                    worker_connection,
                    resource_uri,
                )
                worker_result["checkpoint_id"] = worker_checkpoint.id
                worker_session.commit()
                worker_result["outcome"] = "committed"
            except DBAPIError as error:
                worker_session.rollback()
                worker_result["outcome"] = "rejected"
                worker_result["sqlstate"] = error.orig.sqlstate
            finally:
                worker_ready.set()
                worker_done.set()

    owner_session = Session(engine)
    worker = threading.Thread(target=create_same_checkpoint, daemon=True)
    try:
        owner_connection = owner_session.get(MCPConnection, connection_id)
        assert owner_connection is not None
        owner_checkpoint = _lock_resource_checkpoint(
            owner_session,
            principal,
            owner_connection,
            resource_uri,
        )
        worker.start()
        assert worker_ready.wait(timeout=5)

        observed_advisory_wait = False
        deadline = time.monotonic() + 5
        with engine.connect() as observer:
            while not worker_done.is_set() and time.monotonic() < deadline:
                wait_state = observer.execute(
                    text(
                        "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_result["pid"]},
                ).one_or_none()
                if wait_state == ("Lock", "advisory"):
                    observed_advisory_wait = True
                    break
                time.sleep(0.01)

        assert observed_advisory_wait, worker_result
        owner_session.commit()
        assert worker_done.wait(timeout=5)
        worker.join(timeout=5)
        assert worker_result["outcome"] == "committed"
        assert worker_result["checkpoint_id"] == owner_checkpoint.id

        with engine.connect() as verifier:
            checkpoint_count = verifier.scalar(
                text(
                    "SELECT count(*) FROM mcp_resource_checkpoints "
                    "WHERE organization_id = :organization_id "
                    "AND workspace_id = :workspace_id "
                    "AND connection_id = :connection_id"
                ),
                {
                    "organization_id": organization_id,
                    "workspace_id": workspace_id,
                    "connection_id": connection_id,
                },
            )
        assert checkpoint_count == 1
    finally:
        owner_session.rollback()
        owner_session.close()
        worker_done.wait(timeout=5)
        worker.join(timeout=5)
        with Session(engine) as cleanup:
            cleanup.query(MCPResourceCheckpoint).filter_by(connection_id=connection_id).delete()
            cleanup.query(MCPConnection).filter_by(id=connection_id).delete()
            cleanup.query(Source).filter_by(id=source_id).delete()
            cleanup.query(User).filter_by(id=user_id).delete()
            cleanup.query(Workspace).filter_by(id=workspace_id).delete()
            cleanup.query(Organization).filter_by(id=organization_id).delete()
            cleanup.commit()
        engine.dispose()


def test_mcp_resource_observation_is_serialized_with_checkpoint_update() -> None:
    engine = postgres_engine()
    suffix = uuid4().hex
    endpoint = "https://example.com/mcp"
    resource_uri = f"kb://policies/payment-{suffix}"
    old_text = "# Payment Policy\n\nInvoices are due in 30 days."
    new_text = "# Payment Policy\n\nInvoices are due in 45 days."
    with Session(engine) as setup:
        organization = Organization(name="MCP Snapshot Race", slug=f"snapshot-race-{suffix}")
        setup.add(organization)
        setup.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        setup.add_all([workspace, user])
        setup.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=endpoint,
            metadata_={"source_system": "mcp"},
        )
        setup.add(source)
        setup.flush()
        saved_connection = MCPConnection(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            created_by_user_id=user.id,
            name="Snapshot race",
            credential_key="knowledge-prod",
            enabled=True,
        )
        setup.add(saved_connection)
        setup.commit()
        organization_id = organization.id
        workspace_id = workspace.id
        user_id = user.id
        source_id = source.id
        connection_id = saved_connection.id

    principal = Principal(
        user_id=user_id,
        role="editor",
        scope=TenantScope(
            organization_id=organization_id,
            workspace_id=workspace_id,
        ),
    )
    old_read_started = threading.Event()
    newer_request_done = threading.Event()
    outcomes: dict[str, object] = {}

    class SnapshotConnector:
        def __init__(self, snapshot: str) -> None:
            self.snapshot = snapshot

        def initialize(self) -> None:
            return

        def read_resource(self, uri: str) -> MCPResourceContent:
            assert uri == resource_uri
            if self.snapshot == old_text:
                old_read_started.set()
                newer_request_done.wait(timeout=1)
            return MCPResourceContent(
                uri=resource_uri,
                name="Payment Policy.md",
                mime_type="text/markdown",
                text=self.snapshot,
            )

        def close(self) -> None:
            return

    def ingest(snapshot: str, outcome_key: str) -> None:
        with Session(engine) as worker_session:
            try:
                worker_session.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection = worker_session.get(MCPConnection, connection_id)
                assert connection is not None
                result = _perform_resource_intake(
                    endpoint_url=endpoint,
                    access_token=SecretStr("server-owned-secret"),
                    resource_uri=resource_uri,
                    operation="intake_saved_resource",
                    session=worker_session,
                    principal=principal,
                    factory=lambda _endpoint, _token: SnapshotConnector(snapshot),
                    allowed_hosts={"example.com"},
                    checkpoint_connection=connection,
                )
                outcomes[outcome_key] = result.id
            except BaseException as error:
                outcomes[outcome_key] = error
            finally:
                if outcome_key == "new":
                    newer_request_done.set()

    old_worker = threading.Thread(
        target=ingest,
        args=(old_text, "old"),
        name="old-snapshot",
        daemon=True,
    )
    new_worker = threading.Thread(
        target=ingest,
        args=(new_text, "new"),
        name="new-snapshot",
        daemon=True,
    )
    old_worker.start()
    assert old_read_started.wait(timeout=5)
    new_worker.start()
    old_worker.join(timeout=10)
    new_worker.join(timeout=10)
    assert not old_worker.is_alive()
    assert not new_worker.is_alive()
    assert not isinstance(outcomes.get("old"), BaseException), outcomes
    assert not isinstance(outcomes.get("new"), BaseException), outcomes

    ingest(new_text, "replay")
    assert not isinstance(outcomes.get("replay"), BaseException), outcomes
    with Session(engine) as verifier:
        run_count = verifier.scalar(
            text(
                "SELECT count(*) FROM ingestion_runs "
                "WHERE organization_id = :organization_id "
                "AND workspace_id = :workspace_id AND source_id = :source_id"
            ),
            {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "source_id": source_id,
            },
        )
        asset_count = verifier.scalar(
            text(
                "SELECT count(*) FROM source_assets "
                "WHERE organization_id = :organization_id "
                "AND workspace_id = :workspace_id AND source_id = :source_id"
            ),
            {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "source_id": source_id,
            },
        )
        new_hash_count = verifier.scalar(
            text(
                "SELECT count(*) FROM ingestion_runs "
                "WHERE organization_id = :organization_id "
                "AND workspace_id = :workspace_id AND source_id = :source_id "
                "AND content_hash = :content_hash"
            ),
            {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "source_id": source_id,
                "content_hash": sha256(new_text.encode()).hexdigest(),
            },
        )
        checkpoint = verifier.scalar(
            select(MCPResourceCheckpoint).where(
                MCPResourceCheckpoint.connection_id == connection_id,
                MCPResourceCheckpoint.resource_uri == resource_uri,
            )
        )
    assert run_count == 2
    assert asset_count == 2
    assert new_hash_count == 1
    assert checkpoint is not None
    assert checkpoint.content_hash == sha256(new_text.encode()).hexdigest()
    engine.dispose()


def test_sync_run_database_guards_identity_policy_and_terminal_aggregate() -> None:
    engine = postgres_engine()
    suffix = uuid4().hex
    resource_uri = f"kb://guard/{suffix}"
    with Session(engine) as session:
        organization = Organization(name="MCP Sync Guard", slug=f"sync-guard-{suffix}")
        session.add(organization)
        session.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"guard-{suffix}@example.com",
            display_name="Guard",
        )
        session.add_all([workspace, user])
        session.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=f"https://{suffix}.example.com/mcp",
            metadata_={"source_system": "mcp"},
        )
        session.add(source)
        session.flush()
        connection = MCPConnection(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            created_by_user_id=user.id,
            name="Guarded sync",
            credential_key="knowledge-prod",
            enabled=True,
        )
        session.add(connection)
        session.flush()
        sync_run = MCPSyncRun(
            organization_id=organization.id,
            workspace_id=workspace.id,
            connection_id=connection.id,
            source_id=source.id,
            created_by_user_id=user.id,
            status="queued",
            requested_count=1,
            completed_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=0,
            max_concurrency=4,
            max_attempts=3,
        )
        session.add(sync_run)
        session.commit()
        organization_id = organization.id
        workspace_id = workspace.id
        source_id = source.id
        connection_id = connection.id
        sync_run_id = sync_run.id

        mismatched_hash = MCPSyncItem(
            organization_id=organization_id,
            workspace_id=workspace_id,
            sync_run_id=sync_run_id,
            connection_id=connection_id,
            source_id=source_id,
            ordinal=0,
            resource_uri=resource_uri,
            resource_uri_hash="0" * 64,
            status="queued",
            attempt_count=0,
            max_attempts=3,
        )
        session.add(mismatched_hash)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        wrong_policy = MCPSyncItem(
            organization_id=organization_id,
            workspace_id=workspace_id,
            sync_run_id=sync_run_id,
            connection_id=connection_id,
            source_id=source_id,
            ordinal=0,
            resource_uri=resource_uri,
            resource_uri_hash=sha256(resource_uri.encode("utf-8")).hexdigest(),
            status="queued",
            attempt_count=0,
            max_attempts=2,
        )
        session.add(wrong_policy)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        item = MCPSyncItem(
            organization_id=organization_id,
            workspace_id=workspace_id,
            sync_run_id=sync_run_id,
            connection_id=connection_id,
            source_id=source_id,
            ordinal=0,
            resource_uri=resource_uri,
            resource_uri_hash=sha256(resource_uri.encode("utf-8")).hexdigest(),
            status="queued",
            attempt_count=0,
            max_attempts=3,
        )
        session.add(item)
        session.commit()
        item_id = item.id

        retarget_uri = f"kb://guard/{suffix}/retargeted"
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE mcp_sync_items SET resource_uri = :resource_uri, "
                    "resource_uri_hash = :resource_uri_hash WHERE id = :item_id"
                ),
                {
                    "resource_uri": retarget_uri,
                    "resource_uri_hash": sha256(retarget_uri.encode("utf-8")).hexdigest(),
                    "item_id": item_id,
                },
            )
            session.commit()
        session.rollback()

        now = utc_now()
        run_owner = uuid4()
        item_owner = uuid4()
        session.execute(
            text(
                "UPDATE mcp_sync_runs SET status = 'running', "
                "lease_owner = :owner, lease_expires_at = :expires_at, "
                "started_at = :started_at WHERE id = :run_id"
            ),
            {
                "owner": run_owner,
                "expires_at": now + timedelta(minutes=5),
                "started_at": now,
                "run_id": sync_run_id,
            },
        )
        session.execute(
            text(
                "UPDATE mcp_sync_items SET status = 'running', attempt_count = 1, "
                "lease_owner = :owner, lease_expires_at = :expires_at, "
                "started_at = :started_at WHERE id = :item_id"
            ),
            {
                "owner": item_owner,
                "expires_at": now + timedelta(minutes=1),
                "started_at": now,
                "item_id": item_id,
            },
        )
        session.commit()
        session.execute(
            text(
                "UPDATE mcp_sync_items SET status = 'failed', lease_owner = NULL, "
                "lease_expires_at = NULL, finished_at = :finished_at, "
                "error_code = 'connector_error', "
                "error_message = 'MCP sync item failed' WHERE id = :item_id"
            ),
            {"finished_at": utc_now(), "item_id": item_id},
        )
        session.commit()

        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE mcp_sync_runs SET status = 'succeeded', "
                    "completed_count = 1, changed_count = 1, "
                    "lease_owner = NULL, lease_expires_at = NULL, "
                    "finished_at = :finished_at WHERE id = :run_id"
                ),
                {"finished_at": utc_now(), "run_id": sync_run_id},
            )
            session.commit()
        session.rollback()

        session.execute(
            text(
                "UPDATE mcp_sync_runs SET status = 'failed', completed_count = 1, "
                "failed_count = 1, lease_owner = NULL, lease_expires_at = NULL, "
                "finished_at = :finished_at WHERE id = :run_id"
            ),
            {"finished_at": utc_now(), "run_id": sync_run_id},
        )
        session.commit()
        terminal_run = session.get(MCPSyncRun, sync_run_id)
        assert terminal_run is not None
        assert terminal_run.status == "failed"
        assert terminal_run.failed_count == 1

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(DBAPIError):
                connection.execute(
                    text("DELETE FROM mcp_sync_items WHERE sync_run_id = :run_id"),
                    {"run_id": sync_run_id},
                )
                connection.execute(
                    text("DELETE FROM mcp_sync_runs WHERE id = :run_id"),
                    {"run_id": sync_run_id},
                )
        finally:
            transaction.rollback()

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(DBAPIError):
                connection.execute(text("TRUNCATE TABLE mcp_sync_items, mcp_sync_runs"))
        finally:
            transaction.rollback()
    engine.dispose()


def test_sync_run_enforces_coordinator_lease_and_four_worker_cap() -> None:
    engine = postgres_engine()
    suffix = uuid4().hex
    endpoint = "https://example.com/mcp"
    resource_uris = [f"kb://bounded/{suffix}/{index}" for index in range(8)]
    with Session(engine) as setup:
        organization = Organization(name="MCP Sync DB", slug=f"mcp-sync-{suffix}")
        setup.add(organization)
        setup.flush()
        workspace = Workspace(
            organization_id=organization.id,
            name="Main",
            slug=f"main-{suffix}",
            settings={},
        )
        user = User(
            organization_id=organization.id,
            email=f"owner-{suffix}@example.com",
            display_name="Owner",
        )
        setup.add_all([workspace, user])
        setup.flush()
        source = Source(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_type="mcp_instance",
            uri=endpoint,
            metadata_={"source_system": "mcp"},
        )
        setup.add(source)
        setup.flush()
        connection = MCPConnection(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_id=source.id,
            created_by_user_id=user.id,
            name="Bounded sync",
            credential_key="knowledge-prod",
            enabled=True,
        )
        setup.add(connection)
        setup.flush()
        stale_lease_time = utc_now()
        sync_run = MCPSyncRun(
            organization_id=organization.id,
            workspace_id=workspace.id,
            connection_id=connection.id,
            source_id=source.id,
            created_by_user_id=user.id,
            status="running",
            requested_count=len(resource_uris),
            completed_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=0,
            max_concurrency=4,
            max_attempts=3,
            lease_owner=uuid4(),
            lease_expires_at=stale_lease_time - timedelta(minutes=1),
            started_at=stale_lease_time - timedelta(minutes=2),
        )
        setup.add(sync_run)
        setup.flush()
        setup.add_all(
            [
                MCPSyncItem(
                    organization_id=organization.id,
                    workspace_id=workspace.id,
                    sync_run_id=sync_run.id,
                    connection_id=connection.id,
                    source_id=source.id,
                    ordinal=ordinal,
                    resource_uri=uri,
                    resource_uri_hash=sha256(uri.encode("utf-8")).hexdigest(),
                    status="running",
                    attempt_count=1,
                    max_attempts=3,
                    lease_owner=uuid4(),
                    lease_expires_at=stale_lease_time - timedelta(minutes=1),
                    started_at=stale_lease_time - timedelta(minutes=2),
                )
                for ordinal, uri in enumerate(resource_uris)
            ]
        )
        setup.commit()
        organization_id = organization.id
        workspace_id = workspace.id
        user_id = user.id
        sync_run_id = sync_run.id

    principal = Principal(
        user_id=user_id,
        role="editor",
        scope=TenantScope(
            organization_id=organization_id,
            workspace_id=workspace_id,
        ),
    )
    active_lock = threading.Lock()
    release_reads = threading.Event()
    four_active = threading.Event()
    active_reads = 0
    max_active_reads = 0

    class BoundedConnector:
        def initialize(self) -> None:
            return

        def read_resource(self, uri: str) -> MCPResourceContent:
            nonlocal active_reads, max_active_reads
            with active_lock:
                active_reads += 1
                max_active_reads = max(max_active_reads, active_reads)
                if active_reads == 4:
                    four_active.set()
            try:
                assert release_reads.wait(timeout=10)
                return MCPResourceContent(
                    uri=uri,
                    name=f"Resource {uri.rsplit('/', 1)[-1]}.md",
                    mime_type="text/markdown",
                    text=f"# Resource\n\n{uri}",
                )
            finally:
                with active_lock:
                    active_reads -= 1

        def close(self) -> None:
            return

    worker_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    outcome: dict[str, object] = {}

    def execute_first() -> None:
        with Session(engine) as coordinator_session:
            try:
                outcome["result"] = execute_sync_run(
                    sync_run_id=sync_run_id,
                    session=coordinator_session,
                    principal=principal,
                    factory=lambda _endpoint, _token: BoundedConnector(),
                    sync_session_factory=worker_sessions,
                    allowed_hosts={"example.com"},
                    credentials={"knowledge-prod": SecretStr("server-owned-secret")},
                )
            except BaseException as error:
                outcome["error"] = error

    first = threading.Thread(target=execute_first, name="sync-coordinator", daemon=True)
    first.start()
    assert four_active.wait(timeout=10)
    with (
        Session(engine) as competing_session,
        pytest.raises(HTTPException) as conflict,
    ):
        execute_sync_run(
            sync_run_id=sync_run_id,
            session=competing_session,
            principal=principal,
            factory=lambda _endpoint, _token: BoundedConnector(),
            sync_session_factory=worker_sessions,
            allowed_hosts={"example.com"},
            credentials={"knowledge-prod": SecretStr("server-owned-secret")},
        )
    assert conflict.value.status_code == 409
    release_reads.set()
    first.join(timeout=30)
    assert not first.is_alive()
    assert "error" not in outcome, outcome
    result = outcome["result"]
    assert isinstance(result, MCPSyncRunRead)
    assert result.status == "succeeded"
    assert result.completed_count == 8
    assert result.changed_count == 8
    assert max_active_reads == 4

    with Session(engine) as verifier:
        items = list(
            verifier.scalars(
                select(MCPSyncItem)
                .where(MCPSyncItem.sync_run_id == sync_run_id)
                .order_by(MCPSyncItem.ordinal)
            )
        )
        persisted_run = verifier.get(MCPSyncRun, sync_run_id)
        canonical_count = verifier.scalar(
            text(
                "SELECT count(*) FROM documents "
                "WHERE organization_id = :organization_id "
                "AND workspace_id = :workspace_id"
            ),
            {
                "organization_id": organization_id,
                "workspace_id": workspace_id,
            },
        )
    assert persisted_run is not None
    assert persisted_run.lease_owner is None
    assert persisted_run.lease_expires_at is None
    assert [item.status for item in items] == ["changed"] * 8
    assert [item.attempt_count for item in items] == [2] * 8
    assert all(item.lease_owner is None for item in items)
    assert canonical_count == 0
    engine.dispose()


def test_due_schedule_dispatch_is_single_claim_under_concurrency() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as setup:
        principal, schedule = _seed_due_schedule(setup)
        schedule_id = schedule.id

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def dispatch() -> None:
        try:
            with Session(engine, expire_on_commit=False) as worker:
                barrier.wait(timeout=5)
                result = dispatch_due_sync_schedules(
                    session=worker,
                    principal=principal,
                    credentials={"knowledge-prod": SecretStr("server-owned-secret")},
                    allowed_hosts={"example.com"},
                )
                results.append(result.dispatched_count)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=dispatch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(results) == [0, 1]
    with Session(engine) as verify:
        assert (
            verify.scalar(
                select(func.count(MCPScheduleTick.id)).where(
                    MCPScheduleTick.schedule_id == schedule_id
                )
            )
            == 1
        )
        assert (
            verify.scalar(
                select(func.count(MCPSyncRun.id))
                .join(
                    MCPScheduleTick,
                    MCPScheduleTick.sync_run_id == MCPSyncRun.id,
                )
                .where(MCPScheduleTick.schedule_id == schedule_id)
            )
            == 1
        )
    engine.dispose()


def test_discovery_claim_prevents_second_connector_construction() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as setup:
        principal, schedule = _seed_due_schedule(setup)
        connection_id = schedule.connection_id

    entered_network = threading.Event()
    release_network = threading.Event()
    factory_calls = 0
    factory_lock = threading.Lock()
    results: list[str] = []
    errors: list[BaseException] = []

    class BlockingConnector:
        def list_resources(self, cursor: str | None = None):
            del cursor
            entered_network.set()
            assert release_network.wait(timeout=10)
            return [], None

        def close(self) -> None:
            return None

    def factory(_: str, __: str):
        nonlocal factory_calls
        with factory_lock:
            factory_calls += 1
        return BlockingConnector()

    def discover(name: str) -> None:
        try:
            with Session(engine, expire_on_commit=False) as worker:
                discover_saved_connection_resources(
                    connection_id=connection_id,
                    session=worker,
                    principal=principal,
                    factory=factory,
                    allowed_hosts={"example.com"},
                    credentials={"knowledge-prod": SecretStr("server-owned-secret")},
                    response=Response(),
                )
                results.append(name)
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=discover, args=("first",))
    first.start()
    assert entered_network.wait(timeout=5)
    second = threading.Thread(target=discover, args=("second",))
    second.start()
    second.join(timeout=5)
    release_network.set()
    first.join(timeout=10)

    assert results == ["first"]
    assert len(errors) == 1
    assert isinstance(errors[0], HTTPException)
    assert errors[0].status_code == 409
    assert factory_calls == 1
    with Session(engine) as verify:
        connection = verify.get(MCPConnection, connection_id)
        assert connection is not None
        assert connection.discovery_lease_owner is None
        assert connection.discovery_lease_expires_at is None
    engine.dispose()


def test_discovery_revalidates_authority_after_network_lock_wait() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as setup:
        principal, schedule = _seed_due_schedule(setup)
        connection_id = schedule.connection_id
        original_resource_count = setup.scalar(
            select(func.count(MCPDiscoveredResource.id)).where(
                MCPDiscoveredResource.connection_id == connection_id
            )
        )

    entered_network = threading.Event()
    release_network = threading.Event()
    errors: list[BaseException] = []

    class BlockingConnector:
        def list_resources(self, cursor: str | None = None):
            del cursor
            entered_network.set()
            assert release_network.wait(timeout=10)
            return [{"uri": "kb://must-not-commit"}], None

        def close(self) -> None:
            return None

    def discover() -> None:
        try:
            with Session(engine, expire_on_commit=False) as worker:
                discover_saved_connection_resources(
                    connection_id=connection_id,
                    session=worker,
                    principal=principal,
                    factory=lambda _endpoint, _token: BlockingConnector(),
                    allowed_hosts={"example.com"},
                    credentials={"knowledge-prod": SecretStr("server-owned-secret")},
                    response=Response(),
                )
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=discover)
    worker.start()
    assert entered_network.wait(timeout=5)
    with Session(engine) as revoker:
        connection = revoker.get(MCPConnection, connection_id)
        assert connection is not None
        connection.enabled = False
        revoker.commit()
    release_network.set()
    worker.join(timeout=10)

    assert len(errors) == 1
    assert isinstance(errors[0], HTTPException)
    assert errors[0].status_code == 409
    with Session(engine) as verify:
        connection = verify.get(MCPConnection, connection_id)
        assert connection is not None
        assert connection.discovery_lease_owner is None
        assert connection.discovery_lease_expires_at is None
        assert (
            verify.scalar(
                select(func.count(MCPDiscoveredResource.id)).where(
                    MCPDiscoveredResource.connection_id == connection_id
                )
            )
            == original_resource_count
        )
        assert (
            verify.scalar(
                select(func.count(MCPDiscoveredResource.id)).where(
                    MCPDiscoveredResource.connection_id == connection_id,
                    MCPDiscoveredResource.resource_uri == "kb://must-not-commit",
                )
            )
            == 0
        )
    engine.dispose()


def test_scoped_connection_update_lock_also_locks_endpoint_source() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as setup:
        principal, schedule = _seed_due_schedule(setup)
        connection_id = schedule.connection_id
        source_id = schedule.source_id

    worker_ready = threading.Event()
    worker_done = threading.Event()
    worker_result: dict[str, object] = {}

    def mutate_source() -> None:
        try:
            with engine.connect() as connection:
                worker_result["pid"] = connection.scalar(text("SELECT pg_backend_pid()"))
                worker_ready.set()
                connection.execute(
                    text("UPDATE sources SET uri = :uri WHERE id = :source_id"),
                    {"uri": "https://revoked.example/mcp", "source_id": source_id},
                )
                connection.commit()
                worker_result["outcome"] = "committed"
        except BaseException as error:
            worker_result["error"] = error
        finally:
            worker_done.set()

    holder = Session(engine, expire_on_commit=False)
    worker = threading.Thread(target=mutate_source, daemon=True)
    try:
        mcp_api._scoped_connection(holder, principal, connection_id, for_update=True)
        worker.start()
        assert worker_ready.wait(timeout=5)

        observed_lock_wait = False
        deadline = time.monotonic() + 5
        with engine.connect() as observer:
            while not worker_done.is_set() and time.monotonic() < deadline:
                if observer.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": worker_result["pid"]},
                ) == "Lock":
                    observed_lock_wait = True
                    break
                time.sleep(0.01)

        assert observed_lock_wait, worker_result
        assert not worker_done.is_set()
        holder.commit()
        assert worker_done.wait(timeout=5)
        assert worker_result == {"pid": worker_result["pid"], "outcome": "committed"}
    finally:
        holder.rollback()
        holder.close()
        worker_done.wait(timeout=5)
        worker.join(timeout=5)
        engine.dispose()


def test_b4_database_guards_catalog_schedule_and_tick_history() -> None:
    engine = postgres_engine()
    with Session(engine, expire_on_commit=False) as session:
        principal, schedule = _seed_due_schedule(session)
        dispatch = dispatch_due_sync_schedules(
            session=session,
            principal=principal,
            credentials={"knowledge-prod": SecretStr("server-owned-secret")},
            allowed_hosts={"example.com"},
        )
        assert dispatch.dispatched_count == 1
        tick_id = session.scalar(
            select(MCPScheduleTick.id).where(MCPScheduleTick.schedule_id == schedule.id)
        )
        resource = session.scalar(
            select(MCPDiscoveredResource).where(
                MCPDiscoveredResource.connection_id == schedule.connection_id
            )
        )
        assert tick_id is not None
        assert resource is not None

        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE mcp_discovered_resources SET resource_uri = :uri, "
                    "resource_uri_hash = :hash WHERE id = :id"
                ),
                {
                    "uri": "kb://retargeted",
                    "hash": sha256(b"kb://retargeted").hexdigest(),
                    "id": resource.id,
                },
            )
            session.commit()
        session.rollback()

        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE mcp_schedule_ticks SET trigger = 'manual' WHERE id = :id"),
                {"id": tick_id},
            )
            session.commit()
        session.rollback()

        with pytest.raises(DBAPIError):
            session.execute(
                text("DELETE FROM mcp_schedule_ticks WHERE id = :id"),
                {"id": tick_id},
            )
            session.commit()
        session.rollback()

        bad_resource = MCPDiscoveredResource(
            organization_id=schedule.organization_id,
            workspace_id=schedule.workspace_id,
            connection_id=schedule.connection_id,
            source_id=schedule.source_id,
            resource_uri="kb://bad-hash",
            resource_uri_hash="0" * 64,
            available=True,
            first_seen_at=utc_now(),
            last_seen_at=utc_now(),
            last_seen_cycle_id=uuid4(),
        )
        session.add(bad_resource)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    engine.dispose()


def test_integration_audit_truncate_is_rejected() -> None:
    engine = postgres_engine()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(DBAPIError):
                connection.execute(text("TRUNCATE TABLE integration_audits"))
        finally:
            transaction.rollback()
    engine.dispose()
