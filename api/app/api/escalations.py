"""Legal Escalation Capture — read / list / status API (Slice A, Phase 2).

Escalations are filed from Slack by non-legal team members and captured by
the slack-bridge intake endpoint (Phase 3); this module is the legal team's
surface for reading the queue and moving each escalation through its fixed
lifecycle (``new`` → ``in_review`` → ``answered`` → ``closed``).

Access model — single legal team per deployment, so every authenticated user
of the deployment is the legal team:

* reads (``GET``) take :data:`ActiveUser`;
* status changes (``PATCH``) take :data:`MutatingUser`, so a read-only
  ``viewer`` role cannot mutate the queue.

Requesters are Slack identities with no lq-ai login, so the queue is
invisible to them by construction. Operator enable/disable and
deletion-on-request (``AdminUser``) land in later phases. The queue is
team-shared, not per-user-owned — there is no owner filter, only the
soft-delete filter (``deleted_at IS NULL``). Each status change writes an
``escalation.status_changed`` audit row (ids + from/to status only, never the
question content) committed in the same transaction as the change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status as http_status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ActiveUser, AdminUser, MutatingUser
from app.audit import audit_action
from app.clients.slack_bridge import (
    delete_escalation_message,
    post_escalation_status_update,
)
from app.config import Settings, get_settings
from app.db.session import get_db
from app.models.escalation import Escalation
from app.models.slack_workspace import SlackWorkspace
from app.schemas.escalation import (
    EscalationResponse,
    EscalationStatus,
    EscalationStatusUpdate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/escalations", tags=["escalations"])


def _to_response(esc: Escalation) -> EscalationResponse:
    return EscalationResponse(
        id=esc.id,
        # The DB check-constraint (chk_escalations_status) guarantees the
        # stored value is one of the four lifecycle states.
        status=cast(EscalationStatus, esc.status),
        question=esc.question,
        links=list(esc.links or []),
        requester_slack_user_id=esc.requester_slack_user_id,
        requester_slack_display_name=esc.requester_slack_display_name,
        slack_channel_id=esc.slack_channel_id,
        slack_thread_ts=esc.slack_thread_ts,
        assignee_user_id=esc.assignee_user_id,
        project_id=esc.project_id,
        created_at=esc.created_at,
        updated_at=esc.updated_at,
    )


async def _load_active(db: AsyncSession, *, escalation_id: uuid.UUID) -> Escalation:
    """Fetch a non-deleted escalation by id; 404 if missing or soft-deleted.

    The queue is team-shared (single legal team per deployment), so there is
    no per-user owner filter — only the soft-delete filter.
    """

    stmt = select(Escalation).where(
        Escalation.id == escalation_id,
        Escalation.deleted_at.is_(None),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="escalation not found",
        )
    return row


async def create_escalation(
    db: AsyncSession,
    *,
    slack_workspace_id: uuid.UUID,
    requester_slack_user_id: str,
    requester_slack_display_name: str | None,
    slack_channel_id: str,
    slack_thread_ts: str,
    question: str,
    links: list[str] | None = None,
    project_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> Escalation:
    """Create one escalation record and its ``escalation.created`` audit row.

    Called by the slack-bridge intake endpoint after the bridge has verified
    the Slack request signature and the requester identity. Flushes but does
    NOT commit — the caller owns the commit boundary so the escalation row and
    its audit row ride a single transaction (invariant P5).

    The actor is ``None`` because the requester is a Slack identity, not an
    lq-ai user. The audit ``details`` carry ids and counts only — never the
    question content (invariant P3).
    """

    escalation = Escalation(
        slack_workspace_id=slack_workspace_id,
        requester_slack_user_id=requester_slack_user_id,
        requester_slack_display_name=requester_slack_display_name,
        slack_channel_id=slack_channel_id,
        slack_thread_ts=slack_thread_ts,
        question=question,
        links=list(links) if links else None,
        project_id=project_id,
    )
    db.add(escalation)
    await db.flush()

    await audit_action(
        db,
        user_id=None,
        action="escalation.created",
        resource_type="escalation",
        resource_id=str(escalation.id),
        project_id=project_id,
        request=request,
        details={
            "status": "new",
            "slack_workspace_id": str(slack_workspace_id),
            "requester_slack_user_id": requester_slack_user_id,
            "link_count": len(links or []),
        },
    )
    return escalation


@router.get(
    "",
    response_model=list[EscalationResponse],
    summary="List active escalations (newest first)",
)
async def list_escalations(
    user: ActiveUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[
        EscalationStatus | None,
        Query(description="Filter by lifecycle status"),
    ] = None,
) -> list[EscalationResponse]:
    """GET /api/v1/escalations — the deployment's active escalation queue.

    Sorted ``created_at DESC`` (newest first); the partial
    ``ix_escalations_active_created_at`` index covers the active set.
    """

    stmt = select(Escalation).where(Escalation.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Escalation.status == status)
    stmt = stmt.order_by(Escalation.created_at.desc(), Escalation.id.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(r) for r in rows]


@router.get(
    "/{escalation_id}",
    response_model=EscalationResponse,
    summary="Fetch a single escalation",
    responses={404: {"description": "Escalation not found"}},
)
async def get_escalation(
    escalation_id: uuid.UUID,
    user: ActiveUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EscalationResponse:
    """GET /api/v1/escalations/{id} — 404 if unknown or soft-deleted."""

    esc = await _load_active(db, escalation_id=escalation_id)
    return _to_response(esc)


def _bridge_configured(settings: Settings) -> bool:
    """The bridge channel is usable only when both its secret and URL are set.

    The shared token is empty by default (M3-D1), so on a deployment that does
    not run the ``slack`` profile this is False and the notify helpers no-op
    silently — no doomed outbound call on every status change / delete.
    """

    return bool(settings.lq_ai_bridge_token and settings.lq_ai_bridge_url)


async def _active_workspace_team_id(db: AsyncSession, esc: Escalation) -> str | None:
    """Resolve the Slack team id for an escalation's *active* workspace, or None.

    Returns None (and logs) when the workspace has been soft-deleted, so the
    notify helpers fail safe rather than post into a disconnected workspace.
    """

    workspace = (
        await db.execute(
            select(SlackWorkspace).where(
                SlackWorkspace.id == esc.slack_workspace_id,
                SlackWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if workspace is None:
        log.warning("escalation.bridge_notify.workspace_missing escalation_id=%s", esc.id)
        return None
    return workspace.team_id


async def _notify_slack_status_change(
    db: AsyncSession, settings: Settings, esc: Escalation
) -> None:
    """Signal the slack-bridge to post the new status into the escalation thread.

    Invariant P1: the backend never calls Slack — it signals the bridge over the
    trusted internal channel (one dedicated, audited egress client; ADR 0022),
    and the bridge fetches the bot token and posts. Only non-secret identifiers
    leave the backend. The caller invokes this best-effort: a failure is logged
    but never undoes the status change, which is already committed.
    """

    if not _bridge_configured(settings):
        return
    team_id = await _active_workspace_team_id(db, esc)
    if team_id is None:
        return
    await post_escalation_status_update(
        settings,
        team_id=team_id,
        channel_id=esc.slack_channel_id,
        thread_ts=esc.slack_thread_ts,
        status=esc.status,
    )


async def _notify_slack_deletion(db: AsyncSession, settings: Settings, esc: Escalation) -> None:
    """Signal the slack-bridge to remove the escalation's Slack message.

    Same egress posture as the status post-back (ADR 0022). Best-effort — the
    lq-ai-side deletion is already committed. Note this removes only the
    thread-root message the bridge posted at capture; any status-update replies
    remain, which is acceptable because they name only a status, never content.
    """

    if not _bridge_configured(settings):
        return
    team_id = await _active_workspace_team_id(db, esc)
    if team_id is None:
        return
    await delete_escalation_message(
        settings,
        team_id=team_id,
        channel_id=esc.slack_channel_id,
        thread_ts=esc.slack_thread_ts,
    )


@router.patch(
    "/{escalation_id}",
    response_model=EscalationResponse,
    summary="Move an escalation's status or assign it (non-viewer roles only)",
    responses={404: {"description": "Escalation not found"}},
)
async def update_escalation(
    escalation_id: uuid.UUID,
    payload: EscalationStatusUpdate,
    request: Request,
    user: MutatingUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscalationResponse:
    """PATCH /api/v1/escalations/{id} — change status and/or assignee.

    A no-op PATCH (no actual change) returns the current state without an
    audit row, mirroring the saved-prompts convention. Any real change writes
    an ``escalation.status_changed`` audit row carrying only the from/to
    status (and an ``assigned`` flag) — never the question content.
    """

    esc = await _load_active(db, escalation_id=escalation_id)

    changed: dict[str, object] = {}
    if payload.status is not None and payload.status != esc.status:
        changed["from"] = esc.status
        changed["to"] = payload.status
        esc.status = payload.status
    if payload.assignee_user_id is not None and payload.assignee_user_id != esc.assignee_user_id:
        esc.assignee_user_id = payload.assignee_user_id
        changed["assigned"] = True

    if not changed:
        return _to_response(esc)

    await audit_action(
        db,
        user_id=user.id,
        action="escalation.status_changed",
        resource_type="escalation",
        resource_id=str(esc.id),
        project_id=esc.project_id,
        request=request,
        details=changed,
    )
    await db.commit()
    await db.refresh(esc)

    # Phase 4: a real status transition posts a note back into the Slack thread,
    # via the bridge. Gated on an actual status move ("to" in changed), not an
    # assignee-only change. Best-effort — the change is already durable, so a
    # bridge failure is logged, never raised.
    if "to" in changed:
        try:
            await _notify_slack_status_change(db, settings, esc)
        except Exception:
            log.warning(
                "escalation.status_post.failed escalation_id=%s",
                esc.id,
                exc_info=True,
            )
    return _to_response(esc)


@router.delete(
    "/{escalation_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Operator deletion-on-request: redact an escalation (admin only)",
    responses={404: {"description": "Escalation not found"}},
)
async def delete_escalation(
    escalation_id: uuid.UUID,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """DELETE /api/v1/escalations/{id} — operator deletion-on-request.

    Soft-deletes the record (so it leaves the queue) AND redacts its content —
    the question text, links, and requester display name — so the privileged
    content is gone from lq-ai, while the row and its audit trail survive. The
    soft-delete makes it 404 to the read/list/status surface (they filter
    ``deleted_at IS NULL``).

    Writes an ``escalation.deleted`` audit row (actor = the operator; ids only,
    never content — P3); on a later user deletion the audit actor is anonymized
    automatically by the ``audit_log.user_id`` ``ON DELETE SET NULL`` FK, so the
    record of the deletion survives without naming the deleter. Best-effort
    (the record is already durable): signals the bridge to remove the Slack-side
    message — invariant P1, the api never calls Slack.
    """

    esc = await _load_active(db, escalation_id=escalation_id)

    # Redact the privileged content; deliberately keep the low-sensitivity Slack
    # routing ids (channel/thread) so the best-effort bridge message-delete below
    # can still find the message to remove.
    esc.deleted_at = datetime.now(UTC)
    esc.question = "[redacted]"  # CHECK requires non-empty; content is gone
    esc.links = None
    esc.requester_slack_display_name = None
    esc.updated_at = datetime.now(UTC)

    await audit_action(
        db,
        user_id=admin.id,
        action="escalation.deleted",
        resource_type="escalation",
        resource_id=str(esc.id),
        project_id=esc.project_id,
        request=request,
        details={"redacted": True},
    )
    await db.commit()

    try:
        await _notify_slack_deletion(db, settings, esc)
    except Exception:
        log.warning(
            "escalation.delete_post.failed escalation_id=%s",
            esc.id,
            exc_info=True,
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
