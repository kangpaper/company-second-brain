"""add reasoning run audit

Revision ID: 9d4e7b8c1f20
Revises: a73340202820
Create Date: 2026-08-13 20:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d4e7b8c1f20"
down_revision: str | Sequence[str] | None = "a73340202820"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reasoning_runs",
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("citation_ids", sa.JSON(), nullable=False),
        sa.Column("uncertainty", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(context_hash) = 64",
            name="ck_reasoning_run_context_hash_length",
        ),
        sa.CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(model)) > 0 "
            "AND length(trim(prompt_version)) > 0",
            name="ck_reasoning_run_provider_identity",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["users.organization_id", "users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "customer_id"],
            ["entities.organization_id", "entities.workspace_id", "entities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "workspace_id", "id"),
    )
    for column in (
        "actor_user_id",
        "customer_id",
        "context_hash",
        "organization_id",
        "workspace_id",
        "status",
    ):
        op.create_index(f"ix_reasoning_runs_{column}", "reasoning_runs", [column])
    op.create_table(
        "reasoning_run_citations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("reasoning_run_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "reasoning_run_id"],
            [
                "reasoning_runs.organization_id",
                "reasoning_runs.workspace_id",
                "reasoning_runs.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "evidence_id"],
            ["evidence.organization_id", "evidence.workspace_id", "evidence.id"],
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "workspace_id", "reasoning_run_id", "evidence_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "reasoning_run_id",
            "ordinal",
            name="uq_reasoning_run_citation_ordinal",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION validate_reasoning_run_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            citation_value text;
        BEGIN
            PERFORM 1
            FROM memberships
            WHERE memberships.organization_id = NEW.organization_id
              AND memberships.workspace_id = NEW.workspace_id
              AND memberships.user_id = NEW.actor_user_id
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'reasoning run actor lacks workspace membership';
            END IF;
            PERFORM 1
            FROM entities
            WHERE entities.organization_id = NEW.organization_id
              AND entities.workspace_id = NEW.workspace_id
              AND entities.id = NEW.customer_id
              AND entities.entity_type = 'customer'
              AND entities.lifecycle_status = 'active'
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'reasoning run target must be an active customer';
            END IF;
            IF NEW.context_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'invalid reasoning run context hash';
            END IF;
            IF length(btrim(NEW.provider, E' \t\n\r')) = 0
               OR length(btrim(NEW.model, E' \t\n\r')) = 0
               OR length(btrim(NEW.prompt_version, E' \t\n\r')) = 0 THEN
                RAISE EXCEPTION 'invalid reasoning run provider identity';
            END IF;
            IF NEW.status = 'failed' THEN
                IF NEW.citation_ids::jsonb <> '[]'::jsonb THEN
                    RAISE EXCEPTION 'failed reasoning run citations must be empty';
                END IF;
                IF length(btrim(NEW.error_code, E' \t\n\r')) = 0
                   OR length(btrim(NEW.error_message, E' \t\n\r')) = 0 THEN
                    RAISE EXCEPTION 'invalid reasoning run failure details';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.status <> 'succeeded' THEN
                RAISE EXCEPTION 'invalid reasoning run status';
            END IF;
            IF length(btrim(NEW.answer, E' \t\n\r')) = 0
               OR length(btrim(NEW.uncertainty, E' \t\n\r')) = 0 THEN
                RAISE EXCEPTION 'invalid grounded answer';
            END IF;
            IF jsonb_typeof(NEW.citation_ids::jsonb) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'reasoning run citations must be an array';
            END IF;
            IF jsonb_array_length(NEW.citation_ids::jsonb) NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION 'reasoning run citation count out of bounds';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.citation_ids::jsonb) AS item
                WHERE jsonb_typeof(item) IS DISTINCT FROM 'string'
            ) THEN
                RAISE EXCEPTION 'reasoning run citations must be UUID strings';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(NEW.citation_ids::jsonb) AS item(value)
                GROUP BY value
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'reasoning run citations must be unique';
            END IF;
            BEGIN
                FOR citation_value IN
                    SELECT value
                    FROM jsonb_array_elements_text(NEW.citation_ids::jsonb) AS item(value)
                LOOP
                    PERFORM 1
                    FROM evidence
                    WHERE evidence.organization_id = NEW.organization_id
                      AND evidence.workspace_id = NEW.workspace_id
                      AND evidence.id = citation_value::uuid
                    FOR KEY SHARE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'reasoning run citation is outside tenant evidence';
                    END IF;
                END LOOP;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'reasoning run citations must be UUID strings';
            END;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_runs_validate_insert
        BEFORE INSERT ON reasoning_runs
        FOR EACH ROW EXECUTE FUNCTION validate_reasoning_run_insert();
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_reasoning_run_citation_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM reasoning_runs
                WHERE reasoning_runs.organization_id = NEW.organization_id
                  AND reasoning_runs.workspace_id = NEW.workspace_id
                  AND reasoning_runs.id = NEW.reasoning_run_id
                  AND reasoning_runs.status = 'succeeded'
                  AND reasoning_runs.citation_ids::jsonb
                      ->> (NEW.ordinal - 1) = NEW.evidence_id::text
            ) THEN
                RAISE EXCEPTION 'reasoning run citation association does not match audit JSON';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_run_citations_validate_insert
        BEFORE INSERT ON reasoning_run_citations
        FOR EACH ROW EXECUTE FUNCTION validate_reasoning_run_citation_insert();
        """
    )
    op.execute(
        """
        CREATE FUNCTION materialize_reasoning_run_citations()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'succeeded' THEN
                INSERT INTO reasoning_run_citations (
                    organization_id,
                    workspace_id,
                    reasoning_run_id,
                    evidence_id,
                    ordinal
                )
                SELECT
                    NEW.organization_id,
                    NEW.workspace_id,
                    NEW.id,
                    item.value::uuid,
                    item.ordinality::integer
                FROM jsonb_array_elements_text(NEW.citation_ids::jsonb)
                    WITH ORDINALITY AS item(value, ordinality);
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_runs_materialize_citations
        AFTER INSERT ON reasoning_runs
        FOR EACH ROW EXECUTE FUNCTION materialize_reasoning_run_citations();
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_reasoning_run_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'reasoning_runs are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_runs_append_only
        BEFORE UPDATE OR DELETE ON reasoning_runs
        FOR EACH ROW EXECUTE FUNCTION reject_reasoning_run_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_runs_block_truncate
        BEFORE TRUNCATE ON reasoning_runs
        FOR EACH STATEMENT EXECUTE FUNCTION reject_reasoning_run_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_run_citations_append_only
        BEFORE UPDATE OR DELETE ON reasoning_run_citations
        FOR EACH ROW EXECUTE FUNCTION reject_reasoning_run_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER reasoning_run_citations_block_truncate
        BEFORE TRUNCATE ON reasoning_run_citations
        FOR EACH STATEMENT EXECUTE FUNCTION reject_reasoning_run_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS reasoning_run_citations_block_truncate "
        "ON reasoning_run_citations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS reasoning_run_citations_append_only "
        "ON reasoning_run_citations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS reasoning_runs_materialize_citations ON reasoning_runs"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS reasoning_run_citations_validate_insert "
        "ON reasoning_run_citations"
    )
    op.execute("DROP FUNCTION IF EXISTS materialize_reasoning_run_citations()")
    op.execute("DROP FUNCTION IF EXISTS validate_reasoning_run_citation_insert()")
    op.execute("DROP TRIGGER IF EXISTS reasoning_runs_block_truncate ON reasoning_runs")
    op.execute("DROP TRIGGER IF EXISTS reasoning_runs_append_only ON reasoning_runs")
    op.execute("DROP FUNCTION IF EXISTS reject_reasoning_run_mutation()")
    op.execute("DROP TRIGGER IF EXISTS reasoning_runs_validate_insert ON reasoning_runs")
    op.execute("DROP FUNCTION IF EXISTS validate_reasoning_run_insert()")
    op.drop_table("reasoning_run_citations")
    for column in (
        "status",
        "workspace_id",
        "organization_id",
        "context_hash",
        "customer_id",
        "actor_user_id",
    ):
        op.drop_index(f"ix_reasoning_runs_{column}", table_name="reasoning_runs")
    op.drop_table("reasoning_runs")
