"""Export-bundle test for escalations (Slice A, Phase 6).

The user-data export must include escalations the user is the *assignee* of —
the only lq-ai-user link, since the requester is a Slack identity. Other users'
escalations, unassigned escalations, and operator-deleted (soft-deleted)
escalations must not appear in this user's bundle.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Escalation, SlackWorkspace, User
from app.security import hash_password
from app.workers.user_export import build_export_zip_for_test


async def _make_user(db_session: AsyncSession, *, suffix: str) -> User:
    user = User(
        email=f"exp-{suffix}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=f"Export User {suffix}",
        hashed_password=hash_password("correct-horse-battery-staple"),
        is_admin=False,
        mfa_enabled=False,
        must_change_password=False,
        role="member",
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_workspace(db_session: AsyncSession) -> SlackWorkspace:
    ws = SlackWorkspace(
        team_id=f"T{uuid.uuid4().hex[:8]}",
        team_name="Acme Legal",
        bot_token_encrypted=b"ciphertext",
        bot_user_id="U0BOT",
        installer_slack_user_id="U0INSTALL",
        scope="commands,chat:write",
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


def _escalation(
    ws: SlackWorkspace,
    *,
    assignee: uuid.UUID | None,
    question: str,
    deleted: bool = False,
) -> Escalation:
    return Escalation(
        slack_workspace_id=ws.id,
        requester_slack_user_id="U0REQ",
        requester_slack_display_name="Dana PM",
        slack_channel_id="C0CHAN",
        slack_thread_ts=f"17000000{uuid.uuid4().hex[:6]}.000100",
        question=question,
        status="in_review",
        assignee_user_id=assignee,
        deleted_at=datetime.now(UTC) if deleted else None,
    )


@pytest.mark.integration
async def test_export_includes_only_my_active_assigned_escalations(
    db_session: AsyncSession,
) -> None:
    me = await _make_user(db_session, suffix="me")
    other = await _make_user(db_session, suffix="other")
    ws = await _make_workspace(db_session)

    mine = _escalation(ws, assignee=me.id, question="my assigned question")
    theirs = _escalation(ws, assignee=other.id, question="someone else's")
    unassigned = _escalation(ws, assignee=None, question="nobody's yet")
    mine_deleted = _escalation(ws, assignee=me.id, question="[redacted]", deleted=True)
    db_session.add_all([mine, theirs, unassigned, mine_deleted])
    await db_session.flush()

    data = await build_export_zip_for_test(db_session, me)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "escalations.json" in zf.namelist()
        rows = json.loads(zf.read("escalations.json"))

    ids = {r["id"] for r in rows}
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids  # other user's — excluded
    assert str(unassigned.id) not in ids  # unassigned — excluded
    assert str(mine_deleted.id) not in ids  # soft-deleted — excluded

    only = next(r for r in rows if r["id"] == str(mine.id))
    assert only["question"] == "my assigned question"
    assert only["assignee_user_id"] == str(me.id)
