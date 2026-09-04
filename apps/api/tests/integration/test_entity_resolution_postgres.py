import os
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from company_brain.domain.models import (
    Entity,
    EntityMerge,
    EntityResolutionAudit,
    EntityResolutionCase,
    EntityType,
    Organization,
    Relationship,
    User,
    Workspace,
)
from company_brain.domain.repositories import TenantScope
from company_brain.entity_resolution.merge import merge_entities, split_merge

pytestmark = pytest.mark.postgres


def postgres_engine():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return create_engine(database_url)


def resolution_scope(
    session: Session, suffix: str
) -> tuple[Organization, Workspace, User, Entity, Entity]:
    organization = Organization(
        name=f"Resolution {suffix}", slug=f"resolution-{suffix}-{uuid4().hex}"
    )
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name="Main",
        slug=f"main-{uuid4().hex}",
    )
    user = User(
        organization_id=organization.id,
        email=f"resolution-{uuid4()}@example.com",
        display_name="Resolver",
    )
    session.add_all([workspace, user])
    session.flush()
    source = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="Acme Corp",
        normalized_name="acme corp",
    )
    target = Entity(
        organization_id=organization.id,
        workspace_id=workspace.id,
        entity_type=EntityType.CUSTOMER,
        name="Acme Corporation",
        normalized_name="acme corporation",
    )
    session.add_all([source, target])
    session.flush()
    return organization, workspace, user, source, target


def test_resolution_audit_is_database_append_only() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, _, _ = resolution_scope(session, "audit")
        audit = EntityResolutionAudit(
            organization_id=organization.id,
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="dismiss",
            details={"case_id": str(uuid4())},
        )
        session.add(audit)
        session.commit()
        audit_id = audit.id

        persisted = session.get(EntityResolutionAudit, audit_id)
        assert persisted is not None
        persisted.details = {"tampered": True}
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()

        persisted = session.get(EntityResolutionAudit, audit_id)
        assert persisted is not None
        session.delete(persisted)
        with pytest.raises(DBAPIError, match="append-only"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_resolution_case_rejects_cross_workspace_selected_entity() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, _, _ = resolution_scope(session, "case-a")
        other_workspace = Workspace(
            organization_id=organization.id,
            name="Other",
            slug=f"other-{uuid4().hex}",
        )
        session.add(other_workspace)
        session.flush()
        other_entity = Entity(
            organization_id=organization.id,
            workspace_id=other_workspace.id,
            entity_type=EntityType.CUSTOMER,
            name="Other Acme",
            normalized_name="other acme",
        )
        session.add(other_entity)
        session.flush()
        session.add(
            EntityResolutionCase(
                organization_id=organization.id,
                workspace_id=workspace.id,
                requested_by_user_id=user.id,
                entity_type=EntityType.CUSTOMER,
                query_name="Acme",
                normalized_name="acme",
                candidates=[],
                status="resolved",
                selected_entity_id=other_entity.id,
                resolution_action="match",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    engine.dispose()


def test_merge_rejects_cross_workspace_entities() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, source, _ = resolution_scope(session, "merge-a")
        other_workspace = Workspace(
            organization_id=organization.id,
            name="Other",
            slug=f"other-{uuid4().hex}",
        )
        session.add(other_workspace)
        session.flush()
        other_target = Entity(
            organization_id=organization.id,
            workspace_id=other_workspace.id,
            entity_type=EntityType.CUSTOMER,
            name="Other Target",
            normalized_name="other target",
        )
        session.add(other_target)
        session.flush()
        session.add(
            EntityMerge(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_entity_id=source.id,
                target_entity_id=other_target.id,
                merged_by_user_id=user.id,
                snapshot={},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    engine.dispose()


def test_entity_cannot_participate_in_two_active_merges() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, source, target = resolution_scope(session, "active-unique")
        third = Entity(
            organization_id=organization.id,
            workspace_id=workspace.id,
            entity_type=EntityType.CUSTOMER,
            name="Third Acme",
            normalized_name="third acme",
        )
        session.add(third)
        session.flush()
        session.add(
            EntityMerge(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_entity_id=source.id,
                target_entity_id=target.id,
                merged_by_user_id=user.id,
                snapshot={},
            )
        )
        session.flush()
        session.add(
            EntityMerge(
                organization_id=organization.id,
                workspace_id=workspace.id,
                source_entity_id=third.id,
                target_entity_id=source.id,
                merged_by_user_id=user.id,
                snapshot={},
            )
        )
        with pytest.raises(DBAPIError, match="active merge"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_active_merge_journal_is_immutable_and_cannot_be_deleted() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, source, target = resolution_scope(
            session, "journal-immutable"
        )
        merge = EntityMerge(
            organization_id=organization.id,
            workspace_id=workspace.id,
            source_entity_id=source.id,
            target_entity_id=target.id,
            merged_by_user_id=user.id,
            snapshot={"external_reference_ids": []},
        )
        session.add(merge)
        session.commit()
        merge_id = merge.id
        user_id = user.id

        persisted = session.get(EntityMerge, merge_id)
        assert persisted is not None
        persisted.snapshot = {"external_reference_ids": [str(uuid4())]}
        with pytest.raises(DBAPIError, match="journal is immutable"):
            session.flush()
        session.rollback()

        persisted = session.get(EntityMerge, merge_id)
        assert persisted is not None
        session.delete(persisted)
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            session.flush()
        session.rollback()

        persisted = session.get(EntityMerge, merge_id)
        assert persisted is not None
        persisted.status = "split"
        persisted.split_by_user_id = user_id
        session.commit()
        persisted.status = "active"
        persisted.split_by_user_id = None
        with pytest.raises(DBAPIError, match="invalid entity merge journal transition"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_relationship_database_rejects_self_loops_and_duplicate_typed_edges() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, _, source, _ = resolution_scope(session, "relationship-integrity")
        session.add(
            Relationship(
                organization_id=organization.id,
                workspace_id=workspace.id,
                from_entity_id=source.id,
                to_entity_id=source.id,
                relationship_type="RELATED_TO",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with Session(engine) as session:
        organization, workspace, _, source, target = resolution_scope(
            session, "relationship-duplicate"
        )
        values = {
            "organization_id": organization.id,
            "workspace_id": workspace.id,
            "from_entity_id": source.id,
            "to_entity_id": target.id,
            "relationship_type": "RELATED_TO",
        }
        session.add(Relationship(**values))
        session.flush()
        session.add(Relationship(**values))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    engine.dispose()


def test_database_rejects_new_relationship_to_merged_tombstone() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, _, source, target = resolution_scope(session, "merged-owner")
        source.lifecycle_status = "merged"
        session.commit()
        session.add(
            Relationship(
                organization_id=organization.id,
                workspace_id=workspace.id,
                from_entity_id=source.id,
                to_entity_id=target.id,
                relationship_type="RELATED_TO",
            )
        )
        with pytest.raises(DBAPIError, match="active entity"):
            session.flush()
        session.rollback()
    engine.dispose()


def test_postgres_merge_split_restores_source_target_relationship() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, user, source, target = resolution_scope(
            session, "split-round-trip"
        )
        relationship = Relationship(
            organization_id=organization.id,
            workspace_id=workspace.id,
            from_entity_id=source.id,
            to_entity_id=target.id,
            relationship_type="POSSIBLE_DUPLICATE",
        )
        session.add(relationship)
        session.commit()
        relationship_id = relationship.id
        scope = TenantScope(organization.id, workspace.id)

        merged = merge_entities(session, scope, user.id, source.id, target.id)
        session.commit()
        split_merge(session, scope, user.id, merged.merge_id)
        session.commit()

        restored = session.get(Relationship, relationship_id)
        assert restored is not None
        assert restored.from_entity_id == source.id
        assert restored.to_entity_id == target.id
    engine.dispose()


def test_relationship_metadata_update_does_not_invert_merge_lock_order() -> None:
    engine = postgres_engine()
    with Session(engine) as session:
        organization, workspace, _, source, target = resolution_scope(
            session, "relationship-lock-order"
        )
        relationship = Relationship(
            organization_id=organization.id,
            workspace_id=workspace.id,
            from_entity_id=source.id,
            to_entity_id=target.id,
            relationship_type="RELATED_TO",
        )
        session.add(relationship)
        session.commit()
        source_id, target_id, relationship_id = source.id, target.id, relationship.id

    connection_a = engine.connect()
    transaction_a = connection_a.begin()
    connection_a.execute(
        text("SELECT id FROM entities WHERE id IN (:source_id, :target_id) FOR UPDATE"),
        {"source_id": source_id, "target_id": target_id},
    )

    started = threading.Event()
    finished = threading.Event()
    outcome: list[BaseException] = []
    backend_pid: list[int] = []

    def update_relationship() -> None:
        try:
            with engine.begin() as connection_b:
                backend_pid.append(
                    connection_b.execute(text("SELECT pg_backend_pid()")).scalar_one()
                )
                started.set()
                connection_b.execute(
                    text("UPDATE relationships SET confidence = 0.75 WHERE id = :id"),
                    {"id": relationship_id},
                )
        except BaseException as error:
            outcome.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=update_relationship)
    worker.start()
    assert started.wait(timeout=2)

    deadline = time.monotonic() + 3
    while not finished.is_set() and time.monotonic() < deadline:
        with engine.connect() as observer:
            waiting = observer.execute(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": backend_pid[0]},
            ).scalar_one_or_none()
        if waiting:
            break
        time.sleep(0.01)

    connection_a.execute(
        text("SELECT id FROM relationships WHERE id = :id FOR UPDATE"),
        {"id": relationship_id},
    )
    transaction_a.commit()
    worker.join(timeout=3)
    connection_a.close()

    assert finished.is_set()
    assert outcome == []
    engine.dispose()
