"""Pydantic schemas for the Legal Escalation Capture API (Slice A).

Shared across the legal team's escalation route (read / list / status) and,
in a later phase, the slack-bridge intake endpoint. Mirrors the
``Escalation*`` components in ``docs/api/backend-openapi.yaml``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EscalationStatus = Literal["new", "in_review", "answered", "closed"]
"""The fixed v1 escalation lifecycle — matches the ``chk_escalations_status``
DB check constraint. No custom/configurable workflow states in v1."""


class EscalationResponse(BaseModel):
    """One escalation as returned to the legal team's surface."""

    id: uuid.UUID
    status: EscalationStatus
    question: str
    links: list[str] = Field(default_factory=list)
    requester_slack_user_id: str
    requester_slack_display_name: str | None
    slack_channel_id: str
    slack_thread_ts: str
    assignee_user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class EscalationStatusUpdate(BaseModel):
    """PATCH body. Supply ``status`` to move the escalation through its
    lifecycle and/or ``assignee_user_id`` to assign it to a legal user.
    Fields left unset are unchanged. ``extra="forbid"`` so a typo'd field name
    (e.g. ``staus``) is rejected with 422 rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")

    status: EscalationStatus | None = None
    assignee_user_id: uuid.UUID | None = None


_QUESTION_MAX = 10_000
_MAX_LINKS = 20


class EscalationCreate(BaseModel):
    """Wire shape the slack-bridge POSTs to create an escalation.

    The requester is the verified Slack identity (the bridge verifies the
    Slack request signature before calling); the api resolves the workspace
    from ``team_id``. Renaming a field breaks the bridge → api contract.
    """

    model_config = ConfigDict(extra="forbid")

    team_id: str = Field(..., min_length=1, description="Slack workspace id (T0...).")
    requester_slack_user_id: str = Field(
        ..., min_length=1, description="Verified Slack user id of who filed the escalation."
    )
    requester_slack_display_name: str | None = Field(
        default=None, description="Snapshotted Slack display name, for human reading."
    )
    slack_channel_id: str = Field(..., min_length=1)
    slack_thread_ts: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=_QUESTION_MAX)
    links: list[str] = Field(default_factory=list, max_length=_MAX_LINKS)


class EscalationIntakeResponse(BaseModel):
    """Minimal confirmation returned to the bridge after capture, so it can
    reference the record id and post a confirmation into the thread."""

    id: uuid.UUID
    status: EscalationStatus
    slack_thread_ts: str


class EscalationCaptureToggle(BaseModel):
    """PATCH body for the operator's deployment-wide capture on/off switch."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class EscalationCaptureStatus(BaseModel):
    """The deployment-wide escalation-capture on/off state (admin surface)."""

    enabled: bool
