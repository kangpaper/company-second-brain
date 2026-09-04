from __future__ import annotations

import copy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    desc,
    event,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, Session, mapped_column

from company_brain.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class EntityType(StrEnum):
    ORGANIZATION = "organization"
    PERSON = "person"
    EMPLOYEE = "employee"
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    PRODUCT = "product"
    ORDER = "order"
    INVOICE = "invoice"
    OPPORTUNITY = "opportunity"
    PROJECT = "project"
    TICKET = "ticket"
    MEETING = "meeting"
    EMAIL = "email"
    DOCUMENT = "document"
    DECISION = "decision"
    EVENT = "event"
    TASK = "task"


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    DECISION = "decision"
    CONVERSATION = "conversation"
    BUSINESS = "business"


def enum_values(enum_class: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_class]


def workspace_scope_constraint() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "workspace_id"],
        ["workspaces.organization_id", "workspaces.id"],
    )


def entity_scope_constraint(column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "workspace_id", column],
        ["entities.organization_id", "entities.workspace_id", "entities.id"],
    )


class IdMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Organization(IdMixin, TimestampMixin, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(100))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "slug"),
    )


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(255))
    api_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "email"),
    )


class Membership(IdMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)
    user_id: Mapped[UUID] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "user_id"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class TenantRecordMixin:
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    workspace_id: Mapped[UUID] = mapped_column(index=True)


class Entity(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "entities"
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, values_callable=enum_values), index=True
    )
    name: Mapped[str] = mapped_column(String(500), index=True)
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="active")
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        workspace_scope_constraint(),
    )


class EntityRevision(TenantRecordMixin, Base):
    __tablename__ = "entity_revisions"
    revision_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(index=True)

    @property
    def id(self) -> UUID:
        return self.entity_id

    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, values_callable=enum_values), index=True
    )
    name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    lifecycle_status: Mapped[str] = mapped_column(String(32))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    operation: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "revision_id"),
        CheckConstraint(
            "operation IN ('insert', 'update', 'delete')",
            name="ck_entity_revision_operation",
        ),
        Index(
            "ix_entity_revisions_tenant_entity_effective",
            "organization_id",
            "workspace_id",
            "entity_id",
            "effective_at",
            "revision_id",
        ),
        workspace_scope_constraint(),
    )


class ExternalReference(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "external_references"
    entity_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    source_system: Mapped[str] = mapped_column(String(100))
    source_model: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    raw_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_id",
            "source_model",
            "external_id",
            name="uq_external_reference_workspace_source",
        ),
        workspace_scope_constraint(),
        entity_scope_constraint("entity_id"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id"],
            ["sources.organization_id", "sources.workspace_id", "sources.id"],
        ),
    )


class Relationship(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "relationships"
    from_entity_id: Mapped[UUID] = mapped_column(index=True)
    to_entity_id: Mapped[UUID] = mapped_column(index=True)
    relationship_type: Mapped[str] = mapped_column(String(100), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "from_entity_id",
            "to_entity_id",
            "relationship_type",
            name="uq_relationship_typed_edge",
        ),
        CheckConstraint("from_entity_id <> to_entity_id", name="ck_relationship_no_self_loop"),
        Index(
            "ix_relationships_tenant_from_type",
            "organization_id",
            "workspace_id",
            "from_entity_id",
            "relationship_type",
        ),
        Index(
            "ix_relationships_tenant_to_type",
            "organization_id",
            "workspace_id",
            "to_entity_id",
            "relationship_type",
        ),
        workspace_scope_constraint(),
        entity_scope_constraint("from_entity_id"),
        entity_scope_constraint("to_entity_id"),
    )


class Event(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "events"
    subject_entity_id: Mapped[UUID | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        Index(
            "ix_events_tenant_occurred_id",
            "organization_id",
            "workspace_id",
            occurred_at.desc(),
            desc("id"),
        ),
        workspace_scope_constraint(),
        entity_scope_constraint("subject_entity_id"),
    )


class Source(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "sources"
    source_type: Mapped[str] = mapped_column(String(100), index=True)
    uri: Mapped[str] = mapped_column(String(2048))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        Index(
            "uq_sources_odoo_instance_uri",
            "organization_id",
            "workspace_id",
            "source_type",
            "uri",
            unique=True,
            postgresql_where=text("source_type = 'odoo_instance'"),
            sqlite_where=text("source_type = 'odoo_instance'"),
        ),
        Index(
            "uq_sources_mcp_instance_uri",
            "organization_id",
            "workspace_id",
            "source_type",
            "uri",
            unique=True,
            postgresql_where=text("source_type = 'mcp_instance'"),
            sqlite_where=text("source_type = 'mcp_instance'"),
        ),
        workspace_scope_constraint(),
    )


class MCPConnection(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_connections"
    source_id: Mapped[UUID] = mapped_column(index=True)
    created_by_user_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200))
    credential_key: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    discovery_cursor: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    discovery_cycle_id: Mapped[UUID | None] = mapped_column(nullable=True)
    discovery_lease_owner: Mapped[UUID | None] = mapped_column(nullable=True)
    discovery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "id",
            "source_id",
            name="uq_mcp_connections_checkpoint_source",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_id",
            name="uq_mcp_connections_tenant_source",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "name",
            name="uq_mcp_connections_tenant_name",
        ),
        CheckConstraint(
            "length(credential_key) BETWEEN 1 AND 64",
            name="ck_mcp_connection_credential_key_length",
        ),
        CheckConstraint(
            "(discovery_cursor IS NULL AND discovery_cycle_id IS NULL) OR "
            "(discovery_cursor IS NOT NULL AND discovery_cycle_id IS NOT NULL)",
            name="ck_mcp_connection_discovery_cursor_cycle",
        ),
        CheckConstraint(
            "(discovery_lease_owner IS NULL AND discovery_lease_expires_at IS NULL) OR "
            "(discovery_lease_owner IS NOT NULL AND discovery_lease_expires_at IS NOT NULL)",
            name="ck_mcp_connection_discovery_lease",
        ),
        CheckConstraint(
            "credential_key ~ '^[a-z][a-z0-9-]{0,63}$'",
            name="ck_mcp_connection_credential_key_format",
        ).ddl_if(dialect="postgresql"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id"],
            ["sources.organization_id", "sources.workspace_id", "sources.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class MCPDiscoveredResource(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_discovered_resources"
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    resource_uri: Mapped[str] = mapped_column(String(2048))
    resource_uri_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_cycle_id: Mapped[UUID] = mapped_column(index=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "connection_id",
            "resource_uri_hash",
            name="uq_mcp_discovered_resources_identity",
        ),
        CheckConstraint(
            "resource_uri_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_discovered_resource_uri_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "resource_uri_hash = encode(sha256(convert_to(resource_uri, 'UTF8')), 'hex')",
            name="ck_mcp_discovered_resource_uri_hash_matches",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "size IS NULL OR size >= 0",
            name="ck_mcp_discovered_resource_size",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "connection_id", "source_id"],
            [
                "mcp_connections.organization_id",
                "mcp_connections.workspace_id",
                "mcp_connections.id",
                "mcp_connections.source_id",
            ],
        ),
    )


class MCPResourceCheckpoint(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_resource_checkpoints"
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    resource_uri: Mapped[str] = mapped_column(String(2048))
    resource_uri_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingestion_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    ingestion_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "connection_id",
            "resource_uri_hash",
            name="uq_mcp_resource_checkpoints_identity",
        ),
        CheckConstraint(
            "resource_uri_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_resource_checkpoint_uri_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(content_hash IS NULL AND ingestion_run_id IS NULL "
            "AND ingestion_status IS NULL AND last_changed_at IS NULL) OR "
            "(content_hash IS NOT NULL AND ingestion_run_id IS NOT NULL "
            "AND ingestion_status = 'succeeded' AND last_changed_at IS NOT NULL)",
            name="ck_mcp_resource_checkpoint_target",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "connection_id", "source_id"],
            [
                "mcp_connections.organization_id",
                "mcp_connections.workspace_id",
                "mcp_connections.id",
                "mcp_connections.source_id",
            ],
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "source_id",
                "ingestion_run_id",
                "content_hash",
                "ingestion_status",
            ],
            [
                "ingestion_runs.organization_id",
                "ingestion_runs.workspace_id",
                "ingestion_runs.source_id",
                "ingestion_runs.id",
                "ingestion_runs.content_hash",
                "ingestion_runs.status",
            ],
        ),
    )


class MCPSyncSchedule(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_sync_schedules"
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    created_by_user_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200))
    interval_seconds: Mapped[int]
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "id",
            "connection_id",
            "source_id",
            name="uq_mcp_sync_schedules_resource_scope",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "connection_id",
            "name",
            name="uq_mcp_sync_schedules_connection_name",
        ),
        CheckConstraint(
            "interval_seconds BETWEEN 300 AND 86400",
            name="ck_mcp_sync_schedule_interval",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "connection_id", "source_id"],
            [
                "mcp_connections.organization_id",
                "mcp_connections.workspace_id",
                "mcp_connections.id",
                "mcp_connections.source_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class MCPSyncScheduleResource(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_sync_schedule_resources"
    schedule_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    ordinal: Mapped[int]
    resource_uri: Mapped[str] = mapped_column(String(2048))
    resource_uri_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "schedule_id", "ordinal"),
        UniqueConstraint("organization_id", "workspace_id", "schedule_id", "resource_uri_hash"),
        CheckConstraint("ordinal BETWEEN 0 AND 15", name="ck_mcp_schedule_resource_ordinal"),
        CheckConstraint(
            "resource_uri_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_schedule_resource_uri_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "resource_uri_hash = encode(sha256(convert_to(resource_uri, 'UTF8')), 'hex')",
            name="ck_mcp_schedule_resource_uri_hash_matches",
        ).ddl_if(dialect="postgresql"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "schedule_id",
                "connection_id",
                "source_id",
            ],
            [
                "mcp_sync_schedules.organization_id",
                "mcp_sync_schedules.workspace_id",
                "mcp_sync_schedules.id",
                "mcp_sync_schedules.connection_id",
                "mcp_sync_schedules.source_id",
            ],
        ),
    )


class MCPSyncRun(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_sync_runs"
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    created_by_user_id: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="queued", server_default="queued", index=True
    )
    requested_count: Mapped[int]
    completed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    changed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    unchanged_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    max_concurrency: Mapped[int] = mapped_column(default=4, server_default="4")
    max_attempts: Mapped[int] = mapped_column(default=3, server_default="3")
    lease_owner: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "id",
            "connection_id",
            "source_id",
            "max_attempts",
            name="uq_mcp_sync_runs_item_scope_policy",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_mcp_sync_run_status",
        ),
        CheckConstraint(
            "requested_count BETWEEN 1 AND 16",
            name="ck_mcp_sync_run_requested_count",
        ),
        CheckConstraint(
            "max_concurrency BETWEEN 1 AND 4",
            name="ck_mcp_sync_run_max_concurrency",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 3",
            name="ck_mcp_sync_run_max_attempts",
        ),
        CheckConstraint(
            "completed_count >= 0 AND changed_count >= 0 "
            "AND unchanged_count >= 0 AND failed_count >= 0 "
            "AND completed_count = changed_count + unchanged_count + failed_count "
            "AND completed_count <= requested_count",
            name="ck_mcp_sync_run_counts",
        ),
        CheckConstraint(
            "(status = 'queued' AND completed_count = 0 "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND completed_count = requested_count "
            "AND failed_count = 0 AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND completed_count = requested_count "
            "AND failed_count BETWEEN 1 AND requested_count "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL)",
            name="ck_mcp_sync_run_lifecycle",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "connection_id", "source_id"],
            [
                "mcp_connections.organization_id",
                "mcp_connections.workspace_id",
                "mcp_connections.id",
                "mcp_connections.source_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class MCPScheduleTick(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_schedule_ticks"
    schedule_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    sync_run_id: Mapped[UUID] = mapped_column(index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    trigger: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "schedule_id", "scheduled_for"),
        UniqueConstraint("organization_id", "workspace_id", "sync_run_id"),
        CheckConstraint("trigger IN ('interval', 'manual')", name="ck_mcp_schedule_tick_trigger"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "schedule_id",
                "connection_id",
                "source_id",
            ],
            [
                "mcp_sync_schedules.organization_id",
                "mcp_sync_schedules.workspace_id",
                "mcp_sync_schedules.id",
                "mcp_sync_schedules.connection_id",
                "mcp_sync_schedules.source_id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "sync_run_id"],
            [
                "mcp_sync_runs.organization_id",
                "mcp_sync_runs.workspace_id",
                "mcp_sync_runs.id",
            ],
        ),
    )


class MCPSyncItem(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "mcp_sync_items"
    sync_run_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    ordinal: Mapped[int]
    resource_uri: Mapped[str] = mapped_column(String(2048))
    resource_uri_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), default="queued", server_default="queued", index=True
    )
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(default=3, server_default="3")
    lease_owner: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingestion_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingestion_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "sync_run_id",
            "ordinal",
            name="uq_mcp_sync_items_ordinal",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "sync_run_id",
            "resource_uri_hash",
            name="uq_mcp_sync_items_resource",
        ),
        Index(
            "ix_mcp_sync_items_tenant_run_status_ordinal",
            "organization_id",
            "workspace_id",
            "sync_run_id",
            "status",
            "ordinal",
        ),
        CheckConstraint("ordinal BETWEEN 0 AND 15", name="ck_mcp_sync_item_ordinal"),
        CheckConstraint(
            "resource_uri_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_sync_item_uri_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "resource_uri_hash = encode(sha256(convert_to(resource_uri, 'UTF8')), 'hex')",
            name="ck_mcp_sync_item_uri_hash_matches",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "status IN ('queued', 'running', 'changed', 'unchanged', 'failed')",
            name="ck_mcp_sync_item_status",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 3 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_mcp_sync_item_attempts",
        ),
        CheckConstraint(
            "(status = 'queued' AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND finished_at IS NULL AND ingestion_run_id IS NULL AND content_hash IS NULL "
            "AND ingestion_status IS NULL AND error_code IS NULL AND error_message IS NULL) OR "
            "(status = 'running' AND attempt_count >= 1 AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND ingestion_run_id IS NULL AND content_hash IS NULL "
            "AND ingestion_status IS NULL AND error_code IS NULL AND error_message IS NULL) OR "
            "(status IN ('changed', 'unchanged') AND attempt_count >= 1 "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND ingestion_run_id IS NOT NULL AND content_hash IS NOT NULL "
            "AND ingestion_status = 'succeeded' AND error_code IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND attempt_count >= 1 "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL "
            "AND ingestion_run_id IS NULL AND content_hash IS NULL "
            "AND ingestion_status IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="ck_mcp_sync_item_lifecycle",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "sync_run_id",
                "connection_id",
                "source_id",
                "max_attempts",
            ],
            [
                "mcp_sync_runs.organization_id",
                "mcp_sync_runs.workspace_id",
                "mcp_sync_runs.id",
                "mcp_sync_runs.connection_id",
                "mcp_sync_runs.source_id",
                "mcp_sync_runs.max_attempts",
            ],
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "source_id",
                "ingestion_run_id",
                "content_hash",
                "ingestion_status",
            ],
            [
                "ingestion_runs.organization_id",
                "ingestion_runs.workspace_id",
                "ingestion_runs.source_id",
                "ingestion_runs.id",
                "ingestion_runs.content_hash",
                "ingestion_runs.status",
            ],
        ),
    )


class SourceAsset(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "source_assets"
    source_id: Mapped[UUID] = mapped_column(index=True)
    filename: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int]
    content: Mapped[bytes] = mapped_column(LargeBinary)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint("organization_id", "workspace_id", "source_id", "id"),
        CheckConstraint("byte_size >= 0", name="ck_source_asset_byte_size"),
        CheckConstraint("length(content) = byte_size", name="ck_source_asset_content_size"),
        CheckConstraint("length(content_hash) = 64", name="ck_source_asset_hash_length"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id"],
            ["sources.organization_id", "sources.workspace_id", "sources.id"],
        ),
    )


class IngestionRun(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "ingestion_runs"
    source_id: Mapped[UUID] = mapped_column(index=True)
    source_asset_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int]
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_count: Mapped[int] = mapped_column(default=0)
    document_type: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    classification_confidence: Mapped[float | None] = mapped_column(nullable=True)
    classification_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", index=True
    )
    reviewed_by: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    document_version_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint("organization_id", "workspace_id", "source_id", "id"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_id",
            "id",
            "content_hash",
            "status",
            name="uq_ingestion_runs_mcp_checkpoint_target",
        ),
        CheckConstraint("status IN ('succeeded', 'failed')", name="ck_ingestion_run_status"),
        CheckConstraint("byte_size >= 0", name="ck_ingestion_run_byte_size"),
        CheckConstraint("candidate_count >= 0", name="ck_ingestion_run_candidate_count"),
        CheckConstraint(
            "classification_confidence IS NULL OR "
            "(classification_confidence >= 0 AND classification_confidence <= 1)",
            name="ck_ingestion_run_classification_confidence",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'promoted', 'rejected')",
            name="ck_ingestion_run_review_status",
        ),
        CheckConstraint(
            "(review_status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(review_status IN ('promoted', 'rejected') AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_ingestion_run_review_audit",
        ),
        CheckConstraint(
            "review_status != 'rejected' OR "
            "(review_reason IS NOT NULL AND length(review_reason) > 0)",
            name="ck_ingestion_run_rejection_reason",
        ),
        CheckConstraint(
            "(review_status = 'promoted' AND document_id IS NOT NULL) OR "
            "(review_status IN ('pending', 'rejected') AND document_id IS NULL)",
            name="ck_ingestion_run_review_document",
        ),
        CheckConstraint(
            "(document_id IS NULL AND document_version_id IS NULL) OR "
            "(document_id IS NOT NULL AND document_version_id IS NOT NULL)",
            name="ck_ingestion_run_promotion_pair",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND extracted_text IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL) OR "
            "(status = 'failed' AND extracted_text IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND candidate_count = 0)",
            name="ck_ingestion_run_state_consistency",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id"],
            ["sources.organization_id", "sources.workspace_id", "sources.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id", "source_asset_id"],
            [
                "source_assets.organization_id",
                "source_assets.workspace_id",
                "source_assets.source_id",
                "source_assets.id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "reviewed_by"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "document_id"],
            ["documents.organization_id", "documents.workspace_id", "documents.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "document_id", "document_version_id"],
            [
                "document_versions.organization_id",
                "document_versions.workspace_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
        ),
    )


class ExtractionCandidate(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "extraction_candidates"
    ingestion_run_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    candidate_index: Mapped[int]
    candidate_type: Mapped[str] = mapped_column(String(100), index=True)
    locator: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "ingestion_run_id", "candidate_index"),
        CheckConstraint("candidate_index >= 0", name="ck_extraction_candidate_index_nonnegative"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_extraction_candidate_status",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "source_id",
                "ingestion_run_id",
            ],
            [
                "ingestion_runs.organization_id",
                "ingestion_runs.workspace_id",
                "ingestion_runs.source_id",
                "ingestion_runs.id",
            ],
        ),
    )


class EntityMerge(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "entity_merges"
    source_entity_id: Mapped[UUID] = mapped_column(index=True)
    target_entity_id: Mapped[UUID] = mapped_column(index=True)
    merged_by_user_id: Mapped[UUID]
    split_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        CheckConstraint(
            "source_entity_id <> target_entity_id", name="ck_entity_merge_distinct_entities"
        ),
        CheckConstraint(
            "(status = 'active' AND split_by_user_id IS NULL) OR "
            "(status = 'split' AND split_by_user_id IS NOT NULL)",
            name="ck_entity_merge_state",
        ),
        workspace_scope_constraint(),
        entity_scope_constraint("source_entity_id"),
        entity_scope_constraint("target_entity_id"),
        ForeignKeyConstraint(
            ["organization_id", "merged_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "split_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class EntityResolutionAudit(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "entity_resolution_audits"
    actor_user_id: Mapped[UUID]
    action: Mapped[str] = mapped_column(String(32), index=True)
    merge_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint(
            "action IN ('match', 'dismiss', 'merge', 'split')",
            name="ck_entity_resolution_audit_action",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "merge_id"],
            ["entity_merges.organization_id", "entity_merges.workspace_id", "entity_merges.id"],
        ),
    )


class EntityResolutionCase(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "entity_resolution_cases"
    requested_by_user_id: Mapped[UUID]
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, values_callable=enum_values), index=True
    )
    query_name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    identifiers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    selected_entity_id: Mapped[UUID | None] = mapped_column(nullable=True)
    resolution_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="ck_entity_resolution_case_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND selected_entity_id IS NULL "
            "AND resolution_action IS NULL) OR "
            "(status = 'resolved' AND selected_entity_id IS NOT NULL "
            "AND resolution_action IN ('match', 'merge')) OR "
            "(status = 'dismissed' AND selected_entity_id IS NULL "
            "AND resolution_action = 'dismiss')",
            name="ck_entity_resolution_case_state",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "requested_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
        entity_scope_constraint("selected_entity_id"),
    )


class IntegrationAudit(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "integration_audits"
    actor_user_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(String(50), index=True)
    endpoint: Mapped[str] = mapped_column(String(2048))
    operation: Mapped[str] = mapped_column(String(100), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'denied')",
            name="ck_integration_audit_outcome",
        ),
        CheckConstraint(
            "(outcome = 'succeeded' AND error_code IS NULL AND error_message IS NULL) OR "
            "(outcome IN ('failed', 'denied') AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="ck_integration_audit_state_consistency",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class ActionProposal(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "action_proposals"
    requested_by_user_id: Mapped[UUID] = mapped_column(index=True)
    connector: Mapped[str] = mapped_column(String(50), index=True)
    operation: Mapped[str] = mapped_column(String(50), index=True)
    target: Mapped[dict[str, Any]] = mapped_column(JSON)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    approved_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        CheckConstraint("connector = 'odoo'", name="ck_action_proposal_connector"),
        CheckConstraint(
            "operation IN ('update_record', 'delete_record')",
            name="ck_action_proposal_operation",
        ),
        CheckConstraint(
            "risk_level IN ('standard', 'elevated')",
            name="ck_action_proposal_risk_level",
        ),
        CheckConstraint(
            "(operation = 'delete_record' AND risk_level = 'elevated') OR "
            "(operation = 'update_record' AND risk_level = 'standard')",
            name="ck_action_proposal_operation_risk",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'executed', 'failed')",
            name="ck_action_proposal_status",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "requested_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "approved_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "executed_by_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class ActionAudit(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "action_audits"
    proposal_id: Mapped[UUID] = mapped_column(index=True)
    actor_user_id: Mapped[UUID] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "proposal_id",
            "event_type",
            name="uq_action_audit_proposal_event",
        ),
        CheckConstraint(
            "event_type IN ('proposed', 'approved', 'execution_succeeded', 'execution_failed')",
            name="ck_action_audit_event_type",
        ),
        CheckConstraint(
            "outcome IN ('succeeded', 'failed')",
            name="ck_action_audit_outcome",
        ),
        CheckConstraint(
            "(outcome = 'succeeded' AND error_code IS NULL AND error_message IS NULL) OR "
            "(outcome = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="ck_action_audit_state_consistency",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "proposal_id"],
            [
                "action_proposals.organization_id",
                "action_proposals.workspace_id",
                "action_proposals.id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class Evidence(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "evidence"
    source_id: Mapped[UUID] = mapped_column(index=True)
    evidence_type: Mapped[str] = mapped_column(String(100), index=True)
    pointer: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_id"],
            ["sources.organization_id", "sources.workspace_id", "sources.id"],
        ),
    )


class EvidenceLink(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "evidence_links"
    evidence_id: Mapped[UUID] = mapped_column(index=True)
    entity_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    relationship_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    event_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    document_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    memory_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN entity_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN relationship_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN event_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN document_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN memory_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_evidence_link_exactly_one_target",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "evidence_id"],
            ["evidence.organization_id", "evidence.workspace_id", "evidence.id"],
        ),
        entity_scope_constraint("entity_id"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "relationship_id"],
            [
                "relationships.organization_id",
                "relationships.workspace_id",
                "relationships.id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "event_id"],
            ["events.organization_id", "events.workspace_id", "events.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "document_id"],
            ["documents.organization_id", "documents.workspace_id", "documents.id"],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "memory_id"],
            ["memories.organization_id", "memories.workspace_id", "memories.id"],
        ),
    )


class Document(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "documents"
    title: Mapped[str] = mapped_column(String(500))
    path: Mapped[str] = mapped_column(String(2048))
    content: Mapped[str] = mapped_column(Text, default="")
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint("workspace_id", "path"),
        workspace_scope_constraint(),
    )


class DocumentVersion(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "document_versions"
    document_id: Mapped[UUID] = mapped_column(index=True)
    version_number: Mapped[int]
    markdown: Mapped[str] = mapped_column(Text)
    plain_text: Mapped[str] = mapped_column(Text, default="")
    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint("organization_id", "workspace_id", "document_id", "id"),
        UniqueConstraint("organization_id", "workspace_id", "document_id", "version_number"),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "document_id"],
            ["documents.organization_id", "documents.workspace_id", "documents.id"],
        ),
    )


class DocumentLink(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "document_links"
    source_document_id: Mapped[UUID] = mapped_column(index=True)
    source_version_id: Mapped[UUID] = mapped_column(index=True)
    target_document_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    raw_target: Mapped[str] = mapped_column(String(500))
    normalized_target: Mapped[str] = mapped_column(String(500), index=True)
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "workspace_id", "source_version_id", "normalized_target"
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "source_document_id"],
            ["documents.organization_id", "documents.workspace_id", "documents.id"],
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "workspace_id",
                "source_document_id",
                "source_version_id",
            ],
            [
                "document_versions.organization_id",
                "document_versions.workspace_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "target_document_id"],
            ["documents.organization_id", "documents.workspace_id", "documents.id"],
        ),
    )


class DocumentChunk(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "document_chunks"
    document_id: Mapped[UUID] = mapped_column(index=True)
    version_id: Mapped[UUID] = mapped_column(index=True)
    chunk_index: Mapped[int]
    heading_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text)
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint("organization_id", "workspace_id", "version_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="ck_document_chunk_index_nonnegative"),
        CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_document_chunk_offsets",
        ),
        workspace_scope_constraint(),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "document_id", "version_id"],
            [
                "document_versions.organization_id",
                "document_versions.workspace_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
        ),
    )


class Canvas(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "canvases"
    title: Mapped[str] = mapped_column(String(500))
    path: Mapped[str] = mapped_column(String(2048))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        UniqueConstraint("workspace_id", "path"),
        workspace_scope_constraint(),
    )


class ReasoningRun(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "reasoning_runs"
    actor_user_id: Mapped[UUID] = mapped_column(index=True)
    customer_id: Mapped[UUID] = mapped_column(index=True)
    context_hash: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), index=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        CheckConstraint(
            "length(context_hash) = 64",
            name="ck_reasoning_run_context_hash_length",
        ),
        CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(model)) > 0 "
            "AND length(trim(prompt_version)) > 0",
            name="ck_reasoning_run_provider_identity",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND answer IS NOT NULL "
            "AND length(trim(answer)) > 0 AND length(answer) <= 20000 "
            "AND uncertainty IS NOT NULL AND length(trim(uncertainty)) > 0 "
            "AND length(uncertainty) <= 2000 "
            "AND error_code IS NULL AND error_message IS NULL) OR "
            "(status = 'failed' AND answer IS NULL AND uncertainty IS NULL "
            "AND error_code IS NOT NULL AND length(trim(error_code)) > 0 "
            "AND error_message IS NOT NULL AND length(trim(error_message)) > 0)",
            name="ck_reasoning_run_state",
        ),
        workspace_scope_constraint(),
        entity_scope_constraint("customer_id"),
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["users.organization_id", "users.id"],
        ),
    )


class ReasoningRunCitation(Base):
    __tablename__ = "reasoning_run_citations"
    organization_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    reasoning_run_id: Mapped[UUID] = mapped_column(primary_key=True)
    evidence_id: Mapped[UUID] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "reasoning_run_id",
            "ordinal",
            name="uq_reasoning_run_citation_ordinal",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "reasoning_run_id"],
            [
                "reasoning_runs.organization_id",
                "reasoning_runs.workspace_id",
                "reasoning_runs.id",
            ],
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "evidence_id"],
            ["evidence.organization_id", "evidence.workspace_id", "evidence.id"],
        ),
    )


class Memory(IdMixin, TimestampMixin, TenantRecordMixin, Base):
    __tablename__ = "memories"
    subject_entity_id: Mapped[UUID | None] = mapped_column(nullable=True)
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType, values_callable=enum_values), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    structured_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    review_status: Mapped[str] = mapped_column(String(32), default="draft")
    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_id", "id"),
        workspace_scope_constraint(),
        entity_scope_constraint("subject_entity_id"),
    )


def _entity_revision(entity: Entity, operation: str, effective_at: datetime) -> EntityRevision:
    return EntityRevision(
        organization_id=entity.organization_id,
        workspace_id=entity.workspace_id,
        entity_id=entity.id,
        entity_type=entity.entity_type,
        name=entity.name,
        normalized_name=entity.normalized_name,
        aliases=copy.deepcopy([] if entity.aliases is None else entity.aliases),
        metadata_=copy.deepcopy({} if entity.metadata_ is None else entity.metadata_),
        lifecycle_status=entity.lifecycle_status,
        effective_at=effective_at,
        operation=operation,
    )


@event.listens_for(Session, "before_flush")
def capture_sqlite_entity_revisions(
    session: Session, _flush_context: object, _instances: object
) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        return
    now = utc_now()
    for entity in tuple(session.new):
        if isinstance(entity, Entity):
            if entity.id is None:
                entity.id = uuid4()
            if entity.created_at is None:
                entity.created_at = now
            if entity.lifecycle_status is None:
                entity.lifecycle_status = "active"
            session.add(_entity_revision(entity, "insert", entity.created_at))
    for entity in tuple(session.dirty):
        if isinstance(entity, Entity) and session.is_modified(entity, include_collections=False):
            state = sa_inspect(entity)
            organization_history = state.attrs.organization_id.history
            workspace_history = state.attrs.workspace_id.history
            if organization_history.has_changes() or workspace_history.has_changes():
                old_organization_id = (
                    organization_history.deleted[0]
                    if organization_history.deleted
                    else entity.organization_id
                )
                old_workspace_id = (
                    workspace_history.deleted[0]
                    if workspace_history.deleted
                    else entity.workspace_id
                )
                old_scope_tombstone = _entity_revision(entity, "delete", now)
                old_scope_tombstone.organization_id = old_organization_id
                old_scope_tombstone.workspace_id = old_workspace_id
                session.add(old_scope_tombstone)
            session.add(_entity_revision(entity, "update", now))
    for entity in tuple(session.deleted):
        if isinstance(entity, Entity):
            session.add(_entity_revision(entity, "delete", now))
