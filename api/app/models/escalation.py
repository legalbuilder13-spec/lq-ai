"""Escalation ORM models — Legal Escalation Capture (Slice A).

An Escalation is one legal question filed from Slack by a non-legal team
member (support, product, engineering, trust & safety) and captured into
lq-ai for the legal team to track and (in a later phase) answer. This slice
is capture only: no AI, no routing, no answer.

Per the locked design decisions for this feature:

* The *requester* is the verified Slack identity that filed the escalation
  (workspace + Slack user id + display name) — NOT an lq-ai user account.
  Per-user Slack↔lq-ai binding is separate, unbuilt work; an escalation does
  not require the requester to have an lq-ai login.
* The *owning legal team* is the single legal team per deployment; queue
  visibility is enforced at the route layer (see api/app/api/escalations.py),
  not encoded here. ``assignee_user_id`` records the lq-ai legal user who
  picks the escalation up, if any.
* ``project_id`` optionally scopes the escalation to a matter; when the
  matter is privileged, audit rows for this escalation inherit the privilege
  marking via the standard ``audit_action`` project-privilege resolution.

The status lifecycle is fixed for v1: ``new`` → ``in_review`` → ``answered``
→ ``closed`` (no custom/configurable workflow states). See migration
``0064_escalations.py`` for the authoritative DDL and docs/db-schema.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Escalation(Base):
    """One legal question captured from Slack into lq-ai.

    ``deleted_at`` is the soft-delete column — operator deletion-on-request
    sets it (and redacts content); NULL means active. ``links`` holds the
    related URLs the requester supplied, as a JSON array of strings.
    """

    __tablename__ = "escalations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'in_review', 'answered', 'closed')",
            name="chk_escalations_status",
        ),
        CheckConstraint(
            "char_length(question) > 0",
            name="chk_escalations_question_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    slack_workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "slack_workspaces.id",
            ondelete="RESTRICT",
            name="fk_escalations_slack_workspace_id",
        ),
        nullable=False,
    )
    requester_slack_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    requester_slack_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    slack_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    slack_thread_ts: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    links: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'new'"), default="new"
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_escalations_assignee_user_id"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL", name="fk_escalations_project_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Escalation id={self.id} status={self.status!r} "
            f"workspace={self.slack_workspace_id} thread_ts={self.slack_thread_ts!r} "
            f"deleted={self.deleted_at is not None}>"
        )


class EscalationFile(Base):
    """Many-to-many join: escalation ↔ file (attachments).

    Attachments reuse lq-ai's existing file storage (the ``files`` table and
    ingestion pipeline) so a later phase can feed them to the citation
    engine. Both ends ``ON DELETE CASCADE`` — dropping an escalation removes
    its attachment links; dropping a file removes the join rows referencing
    it. The composite ``(escalation_id, file_id)`` is the primary key.
    """

    __tablename__ = "escalation_files"
    __table_args__ = (PrimaryKeyConstraint("escalation_id", "file_id", name="pk_escalation_files"),)

    escalation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("escalations.id", ondelete="CASCADE", name="fk_escalation_files_escalation_id"),
        nullable=False,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE", name="fk_escalation_files_file_id"),
        nullable=False,
    )
    attached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    def __repr__(self) -> str:
        return f"<EscalationFile escalation_id={self.escalation_id} file_id={self.file_id}>"


class EscalationConfig(Base):
    """Deployment-wide on/off switch for Legal Escalation Capture (Phase 5).

    A single row keyed ``'singleton'`` holds the operator's enable/disable
    switch — there is one legal team per deployment, so one flag. Fail-safe:
    a missing row reads as disabled (see ``app.escalation_config``), so a fresh
    deployment never captures escalations until an operator explicitly turns it
    on. The on/off action is recorded in ``audit_log``; this table only holds
    the current state. The ``id = 'singleton'`` check keeps it a true singleton.
    """

    __tablename__ = "escalation_config"
    __table_args__ = (CheckConstraint("id = 'singleton'", name="chk_escalation_config_singleton"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("'singleton'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    def __repr__(self) -> str:
        return f"<EscalationConfig id={self.id!r} enabled={self.enabled}>"
