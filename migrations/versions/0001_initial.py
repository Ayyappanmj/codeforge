"""initial schema: executions, execution_attempts, outbox_events

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("stdin", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("completed_at", TS),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("stdout", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr", sa.Text(), nullable=False, server_default=""),
        sa.Column("output_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("execution_duration_ms", sa.Integer()),
        sa.Column("error_type", sa.String(32)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enqueue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','TIMED_OUT','CANCELLED')",
            name="ck_executions_status"),
    )
    op.create_index("ix_executions_status_created", "executions", ["status", "created_at"])
    op.create_index("ix_executions_language_created", "executions", ["language", "created_at"])
    op.create_index("ix_executions_created_at", "executions", ["created_at"])

    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("execution_id", sa.Uuid(),
                  sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("outcome", sa.String(16)),
        sa.Column("error_type", sa.String(32)),
        sa.Column("duration_ms", sa.Integer()),
    )
    op.create_index("ix_attempts_execution", "execution_attempts", ["execution_id", "attempt_number"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("execution_id", sa.Uuid(),
                  sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("published_at", TS),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("last_error", sa.String(300)),
    )
    op.create_index("ix_outbox_pending", "outbox_events", ["published_at", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("execution_attempts")
    op.drop_table("executions")
