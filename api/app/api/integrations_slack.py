"""Bridge persistence surface for Slack workspace records — M3-D1.

The slack-bridge service runs the OAuth dance with Slack then POSTs the
resulting workspace tuple here. The bridge authenticates with a shared
bearer token (``LQ_AI_BRIDGE_TOKEN``) — NOT a user JWT, because the
bridge is a service-to-service caller with no user context.

The router is mounted WITHOUT the ``_active`` user gate (parallels
:mod:`app.api.internal` which is also a service-to-service surface).
Auth happens per-handler via :func:`require_bridge_auth`.

Decision M3-D1-2 (re-install semantics): the POST endpoint upserts on
``team_id``. Slack rotates bot tokens on re-install, so an existing
row's ``bot_token_encrypted`` + ``installer_slack_user_id`` + ``scope``
are replaced; a soft-deleted row (``deleted_at IS NOT NULL``) is
revived.

Decision M3-D1-1 (encryption key): the bot token is encrypted under
:envvar:`LQ_AI_BRIDGE_MASTER_KEY` (NOT the gateway's master key) before
persistence.

Decision M3-D1-3 (token storage): ``LQ_AI_BRIDGE_TOKEN`` lives in the
api's :class:`Settings` only — the gateway has no Slack-bridge role so
its secret surface stays minimal.
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_bridge_auth
from app.api.escalations import create_escalation
from app.config import Settings, get_settings
from app.db.session import get_db
from app.escalation_config import get_escalation_enabled
from app.models.slack_workspace import SlackWorkspace
from app.schemas.escalation import (
    EscalationCreate,
    EscalationIntakeResponse,
    EscalationStatus,
)
from app.schemas.slack_workspace import (
    SlackBotTokenResponse,
    SlackWorkspaceCreate,
    SlackWorkspaceResponse,
)
from app.security.encryption import BridgeTokenEncryptor

log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/slack", tags=["integrations-slack"])


@router.post(
    "/workspaces",
    response_model=SlackWorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_bridge_auth)],
)
async def upsert_slack_workspace(
    body: SlackWorkspaceCreate,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SlackWorkspaceResponse:
    """Persist (or upsert) the workspace record from the bridge.

    Upserts on ``team_id``. On conflict:
      * ``team_name`` is refreshed from the request body (operator may
        have renamed the workspace in Slack).
      * ``bot_token_encrypted`` is replaced (Slack rotates tokens on
        re-install).
      * ``bot_user_id`` is refreshed (Slack may have issued a new bot
        user across the re-install).
      * ``installer_slack_user_id`` is refreshed (a different operator
        may be reinstalling).
      * ``scope`` is replaced (consent may have changed).
      * ``deleted_at`` is set back to NULL (re-install revives soft-
        deleted rows).
      * ``installed_at`` stays at the original install time; the
        re-install does not reset the audit timestamp. Operators can
        infer re-install activity from the bot-token ciphertext
        changing without the install timestamp moving.
    """

    encryptor = BridgeTokenEncryptor(master_key=settings.lq_ai_bridge_master_key or None)
    bot_token_encrypted = encryptor.encrypt(body.bot_token)

    existing = (
        await db.execute(select(SlackWorkspace).where(SlackWorkspace.team_id == body.team_id))
    ).scalar_one_or_none()

    if existing is None:
        workspace = SlackWorkspace(
            team_id=body.team_id,
            team_name=body.team_name,
            bot_token_encrypted=bot_token_encrypted,
            bot_user_id=body.bot_user_id,
            installer_slack_user_id=body.installer_slack_user_id,
            scope=body.scope,
        )
        db.add(workspace)
    else:
        existing.team_name = body.team_name
        existing.bot_token_encrypted = bot_token_encrypted
        existing.bot_user_id = body.bot_user_id
        existing.installer_slack_user_id = body.installer_slack_user_id
        existing.scope = body.scope
        existing.deleted_at = None
        workspace = existing

    await db.commit()
    await db.refresh(workspace)
    return SlackWorkspaceResponse.model_validate(workspace)


@router.post(
    "/escalations",
    response_model=EscalationIntakeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_bridge_auth)],
)
async def create_slack_escalation(
    body: EscalationCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EscalationIntakeResponse:
    """Create a tracked escalation from a verified Slack submission (Phase 3a).

    The slack-bridge has already verified the Slack request signature and the
    requester identity before calling this. We resolve the (active) workspace
    from ``team_id``, create the escalation + its audit row, and return the
    record id so the bridge can post a confirmation into the thread. Slack
    egress stays in the bridge per invariant P1 — this endpoint never calls
    Slack.
    """

    # Fail closed FIRST (before any work): the operator's deployment-wide
    # capture switch. Disabling capture refuses NEW escalations; the existing
    # queue is untouched (read/list/status still work). A missing config row
    # reads as disabled (Phase 5).
    if not await get_escalation_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="escalation capture is disabled",
        )

    workspace = (
        await db.execute(
            select(SlackWorkspace).where(
                SlackWorkspace.team_id == body.team_id,
                SlackWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="slack workspace not connected",
        )

    escalation = await create_escalation(
        db,
        slack_workspace_id=workspace.id,
        requester_slack_user_id=body.requester_slack_user_id,
        requester_slack_display_name=body.requester_slack_display_name,
        slack_channel_id=body.slack_channel_id,
        slack_thread_ts=body.slack_thread_ts,
        question=body.question,
        links=body.links,
        request=request,
    )
    await db.commit()
    await db.refresh(escalation)
    return EscalationIntakeResponse(
        id=escalation.id,
        # DB check-constraint guarantees a valid lifecycle state.
        status=cast(EscalationStatus, escalation.status),
        slack_thread_ts=escalation.slack_thread_ts,
    )


@router.get(
    "/workspaces/{team_id}/bot-token",
    response_model=SlackBotTokenResponse,
    dependencies=[Depends(require_bridge_auth)],
)
async def get_slack_bot_token(
    team_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SlackBotTokenResponse:
    """Hand the slack-bridge the decrypted bot token for a workspace (bridge-auth).

    Invariant P1: the backend never calls Slack, so the bridge performs all
    Slack egress and needs the workspace bot token at call time. The token is
    decrypted here from its at-rest ciphertext for the trusted internal
    channel only; the plaintext is never logged.
    """

    workspace = (
        await db.execute(
            select(SlackWorkspace).where(
                SlackWorkspace.team_id == team_id,
                SlackWorkspace.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="slack workspace not connected",
        )

    encryptor = BridgeTokenEncryptor(master_key=settings.lq_ai_bridge_master_key or None)
    bot_token = encryptor.decrypt(workspace.bot_token_encrypted)
    return SlackBotTokenResponse(
        team_id=workspace.team_id,
        bot_user_id=workspace.bot_user_id,
        bot_token=bot_token,
    )
