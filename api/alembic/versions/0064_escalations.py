"""escalations + escalation_files — Legal Escalation Capture (Slice A)

Captures a legal question filed from Slack into a tracked escalation record
owned by the legal team. Capture only — no AI, no routing, no answer.

Design notes baked into this DDL:

* ``slack_workspace_id`` -> ``slack_workspaces.id`` is ``ON DELETE RESTRICT``:
  a workspace with live escalations cannot be hard-deleted out from under
  them (workspaces are soft-deleted in normal operation).
* ``assignee_user_id`` -> ``users.id`` is ``ON DELETE SET NULL`` so deleting
  the legal user who picked an escalation up anonymizes the assignment but
  preserves the record (the audit trail of who-did-what survives in
  ``audit_log`` independently).
* ``project_id`` -> ``projects.id`` is ``ON DELETE SET NULL``: the escalation
  outlives an optional matter link; when the matter is privileged, audit
  rows inherit the privilege marking via ``audit_action``'s project lookup.
* The requester is the verified Slack identity (``requester_slack_user_id`` /
  ``requester_slack_display_name``), not an lq-ai user — requesters need no
  lq-ai account.
* ``status`` is constrained to the fixed v1 lifecycle; no custom states.

Revision ID: 0064
Revises: 0063
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "escalations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "slack_workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "slack_workspaces.id",
                ondelete="RESTRICT",
                name="fk_escalations_slack_workspace_id",
            ),
            nullable=False,
        ),
        sa.Column("requester_slack_user_id", sa.Text(), nullable=False),
        sa.Column("requester_slack_display_name", sa.Text(), nullable=True),
        sa.Column("slack_channel_id", sa.Text(), nullable=False),
        sa.Column("slack_thread_ts", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("links", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'new'")),
        sa.Column(
            "assignee_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_escalations_assignee_user_id"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL", name="fk_escalations_project_id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('new', 'in_review', 'answered', 'closed')",
            name="chk_escalations_status",
        ),
        sa.CheckConstraint(
            "char_length(question) > 0",
            name="chk_escalations_question_nonempty",
        ),
    )
    op.create_index("ix_escalations_status", "escalations", ["status"])
    op.create_index("ix_escalations_slack_workspace_id", "escalations", ["slack_workspace_id"])
    op.create_index("ix_escalations_slack_thread_ts", "escalations", ["slack_thread_ts"])
    op.create_index(
        "ix_escalations_assignee_user_id",
        "escalations",
        ["assignee_user_id"],
        postgresql_where=sa.text("assignee_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_escalations_project_id",
        "escalations",
        ["project_id"],
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "ix_escalations_active_created_at",
        "escalations",
        [sa.text("created_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "escalation_files",
        sa.Column(
            "escalation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "escalations.id", ondelete="CASCADE", name="fk_escalation_files_escalation_id"
            ),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE", name="fk_escalation_files_file_id"),
            nullable=False,
        ),
        sa.Column(
            "attached_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("escalation_id", "file_id", name="pk_escalation_files"),
    )
    op.create_index("ix_escalation_files_file_id", "escalation_files", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_escalation_files_file_id", table_name="escalation_files")
    op.drop_table("escalation_files")
    op.drop_index("ix_escalations_active_created_at", table_name="escalations")
    op.drop_index("ix_escalations_project_id", table_name="escalations")
    op.drop_index("ix_escalations_assignee_user_id", table_name="escalations")
    op.drop_index("ix_escalations_slack_thread_ts", table_name="escalations")
    op.drop_index("ix_escalations_slack_workspace_id", table_name="escalations")
    op.drop_index("ix_escalations_status", table_name="escalations")
    op.drop_table("escalations")
