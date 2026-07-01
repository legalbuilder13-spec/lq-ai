"""Integration tests for the Legal Escalation Capture read/list/status API (Slice A, Phase 2).

Covers the legal team's surface over the escalation queue:

* GET list — active escalations, newest first, soft-deleted excluded, optional status filter.
* GET /{id} — returns an escalation; 404 for unknown OR soft-deleted.
* PATCH /{id} — moves status through the fixed lifecycle (audited as
  ``escalation.status_changed`` with from/to only, no question content);
  rejects invalid status; read-only "viewer" role is forbidden; no-op PATCH
  writes no audit row.
* Auth — unauthenticated callers get 401.

The queue is team-shared (single legal team per deployment), so there is no
per-user owner filter — visibility is the deployment's authenticated users.
Escalations are seeded directly via the model (the Slack-driven create path
lands in Phase 3).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.main import app
from app.models import AuditLog, Escalation, SlackWorkspace, User
from app.security import create_access_token, hash_password


def _override_get_db(db_session: AsyncSession):
    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    return _override


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = _override_get_db(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def stub_slack_notify(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Phase 4/6: a real status change or operator delete signals the slack-bridge
    over the network.

    The notify path is gated on the bridge being configured (token set), so we
    set the token, then stub BOTH egress functions for every test in this module
    so no real HTTP call is made. Yields the recorder list so a test can assert
    the signal (or simply ignore it).
    """

    monkeypatch.setenv("LQ_AI_BRIDGE_TOKEN", "test-bridge-token")
    get_settings.cache_clear()

    calls: list[dict] = []

    async def _record(settings: object, **kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("app.api.escalations.post_escalation_status_update", _record)
    monkeypatch.setattr("app.api.escalations.delete_escalation_message", _record)
    yield calls
    get_settings.cache_clear()


async def _make_user(db_session: AsyncSession, *, role: str = "member", suffix: str = "") -> User:
    user = User(
        email=f"esc-{suffix or uuid.uuid4().hex[:8]}@example.com",
        display_name=f"Esc User {suffix}".strip(),
        hashed_password=hash_password("correct-horse-battery-staple"),
        is_admin=False,
        mfa_enabled=False,
        must_change_password=False,
        role=role,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def member(db_session: AsyncSession) -> User:
    return await _make_user(db_session, role="member", suffix="m")


@pytest_asyncio.fixture
async def viewer(db_session: AsyncSession) -> User:
    return await _make_user(db_session, role="viewer", suffix="v")


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = User(
        email=f"esc-admin-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Esc Admin",
        hashed_password=hash_password("correct-horse-battery-staple"),
        is_admin=True,
        mfa_enabled=False,
        must_change_password=False,
        role="admin",
    )
    db_session.add(user)
    await db_session.flush()
    return user


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(user.id, user.email, is_admin=user.is_admin)
    return {"Authorization": f"Bearer {token}"}


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


async def _make_escalation(
    db_session: AsyncSession,
    ws: SlackWorkspace,
    *,
    status: str = "new",
    question: str = "Can we use this clause?",
    created_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> Escalation:
    esc = Escalation(
        slack_workspace_id=ws.id,
        requester_slack_user_id="U0REQ",
        requester_slack_display_name="Dana PM",
        slack_channel_id="C0CHAN",
        slack_thread_ts="1700000000.000100",
        question=question,
        links=["https://example.com/policy"],
        status=status,
        deleted_at=deleted_at,
    )
    if created_at is not None:
        esc.created_at = created_at
    db_session.add(esc)
    await db_session.flush()
    return esc


# ---------------------------------------------------------------------------
# GET / — list
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_returns_empty_when_none(client: AsyncClient, member: User) -> None:
    resp = await client.get("/api/v1/escalations", headers=_bearer(member))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.integration
async def test_list_newest_first_excludes_soft_deleted(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    base = datetime.now(UTC)
    await _make_escalation(db_session, ws, question="older", created_at=base - timedelta(minutes=5))
    await _make_escalation(db_session, ws, question="newer", created_at=base)
    await _make_escalation(db_session, ws, question="gone", created_at=base, deleted_at=base)

    resp = await client.get("/api/v1/escalations", headers=_bearer(member))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    questions = [row["question"] for row in body]
    assert questions == ["newer", "older"]  # newest first, soft-deleted excluded


@pytest.mark.integration
async def test_list_filters_by_status(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    await _make_escalation(db_session, ws, status="new")
    await _make_escalation(db_session, ws, status="in_review")

    resp = await client.get("/api/v1/escalations?status=in_review", headers=_bearer(member))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "in_review"


@pytest.mark.integration
async def test_list_rejects_unknown_status_filter(client: AsyncClient, member: User) -> None:
    resp = await client.get("/api/v1/escalations?status=bogus", headers=_bearer(member))
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_list_without_bearer_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/escalations")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /{id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_returns_escalation(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws)

    resp = await client.get(f"/api/v1/escalations/{esc.id}", headers=_bearer(member))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(esc.id)
    assert body["status"] == "new"
    assert body["requester_slack_user_id"] == "U0REQ"


@pytest.mark.integration
async def test_get_unknown_returns_404(client: AsyncClient, member: User) -> None:
    resp = await client.get(f"/api/v1/escalations/{uuid.uuid4()}", headers=_bearer(member))
    assert resp.status_code == 404


@pytest.mark.integration
async def test_get_soft_deleted_returns_404(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, deleted_at=datetime.now(UTC))

    resp = await client.get(f"/api/v1/escalations/{esc.id}", headers=_bearer(member))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_patch_changes_status_and_audits(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "in_review"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_review"

    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "escalation.status_changed",
                    AuditLog.resource_id == str(esc.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    details = audits[0].details or {}
    assert details.get("from") == "new"
    assert details.get("to") == "in_review"
    # P3: no question content in the audit row.
    assert "question" not in details


@pytest.mark.integration
async def test_patch_rejects_invalid_status(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws)

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "bogus"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_patch_viewer_role_forbidden(
    client: AsyncClient, db_session: AsyncSession, viewer: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws)

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(viewer),
        json={"status": "in_review"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_patch_no_change_skips_audit_row(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "new"},
    )
    assert resp.status_code == 200, resp.text

    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "escalation.status_changed",
                    AuditLog.resource_id == str(esc.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert audits == []


@pytest.mark.integration
async def test_patch_unknown_returns_404(client: AsyncClient, member: User) -> None:
    resp = await client.patch(
        f"/api/v1/escalations/{uuid.uuid4()}",
        headers=_bearer(member),
        json={"status": "in_review"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id} — Slack status post-back (Phase 4)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_patch_status_change_signals_slack(
    client: AsyncClient,
    db_session: AsyncSession,
    member: User,
    stub_slack_notify: list[dict],
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "in_review"},
    )
    assert resp.status_code == 200, resp.text

    # Exactly one signal, carrying the workspace team id + the thread coords +
    # the new status — and no secret (the bridge resolves the bot token itself).
    assert len(stub_slack_notify) == 1
    signal = stub_slack_notify[0]
    assert signal["team_id"] == ws.team_id
    assert signal["channel_id"] == "C0CHAN"
    assert signal["thread_ts"] == "1700000000.000100"
    assert signal["status"] == "in_review"
    assert "bot_token" not in signal


@pytest.mark.integration
async def test_patch_assignee_only_does_not_signal_slack(
    client: AsyncClient,
    db_session: AsyncSession,
    member: User,
    stub_slack_notify: list[dict],
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"assignee_user_id": str(member.id)},
    )
    assert resp.status_code == 200, resp.text
    # An assignee-only change is not a status transition — no Slack post.
    assert stub_slack_notify == []


@pytest.mark.integration
async def test_patch_noop_does_not_signal_slack(
    client: AsyncClient,
    db_session: AsyncSession,
    member: User,
    stub_slack_notify: list[dict],
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "new"},
    )
    assert resp.status_code == 200, resp.text
    assert stub_slack_notify == []


@pytest.mark.integration
async def test_patch_status_change_survives_slack_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    member: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    async def _boom(settings: object, **kwargs: object) -> None:
        raise RuntimeError("bridge unreachable")

    monkeypatch.setattr("app.api.escalations.post_escalation_status_update", _boom)

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "in_review"},
    )
    # Best-effort: the status change is committed and returned even though the
    # Slack post-back failed.
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_review"


# ---------------------------------------------------------------------------
# DELETE /{id} — operator deletion-on-request (Phase 6)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_redacts_soft_deletes_and_audits(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deletions: list[dict] = []

    async def _record(settings: object, **kwargs: object) -> None:
        deletions.append(dict(kwargs))

    monkeypatch.setattr("app.api.escalations.delete_escalation_message", _record)

    ws = await _make_workspace(db_session)
    esc = await _make_escalation(
        db_session, ws, status="in_review", question="secret privileged question"
    )

    resp = await client.delete(f"/api/v1/escalations/{esc.id}", headers=_bearer(admin))
    assert resp.status_code == 204, resp.text

    # Content redacted + soft-deleted.
    await db_session.refresh(esc)
    assert esc.deleted_at is not None
    assert esc.question == "[redacted]"
    assert esc.links is None
    assert esc.requester_slack_display_name is None

    # One escalation.deleted audit row, content-free (P3).
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "escalation.deleted",
                    AuditLog.resource_id == str(esc.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].user_id == admin.id
    assert "question" not in (audits[0].details or {})

    # Bridge was signalled to remove the Slack message.
    assert len(deletions) == 1
    assert deletions[0]["team_id"] == ws.team_id
    assert deletions[0]["thread_ts"] == "1700000000.000100"

    # Now invisible to the read surface.
    get_resp = await client.get(f"/api/v1/escalations/{esc.id}", headers=_bearer(admin))
    assert get_resp.status_code == 404


@pytest.mark.integration
async def test_delete_survives_bridge_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(settings: object, **kwargs: object) -> None:
        raise RuntimeError("bridge unreachable")

    monkeypatch.setattr("app.api.escalations.delete_escalation_message", _boom)

    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws)

    resp = await client.delete(f"/api/v1/escalations/{esc.id}", headers=_bearer(admin))
    # The deletion is committed even though the Slack-side removal failed.
    assert resp.status_code == 204, resp.text
    await db_session.refresh(esc)
    assert esc.deleted_at is not None


@pytest.mark.integration
async def test_delete_non_admin_forbidden(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws)

    resp = await client.delete(f"/api/v1/escalations/{esc.id}", headers=_bearer(member))
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
async def test_delete_unknown_returns_404(client: AsyncClient, admin: User) -> None:
    resp = await client.delete(f"/api/v1/escalations/{uuid.uuid4()}", headers=_bearer(admin))
    assert resp.status_code == 404


@pytest.mark.integration
async def test_delete_twice_second_returns_404(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    """A second deletion-on-request for the same escalation 404s (already gone)."""
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws)

    first = await client.delete(f"/api/v1/escalations/{esc.id}", headers=_bearer(admin))
    assert first.status_code == 204, first.text
    second = await client.delete(f"/api/v1/escalations/{esc.id}", headers=_bearer(admin))
    assert second.status_code == 404, second.text


# ---------------------------------------------------------------------------
# PATCH / DELETE — additional edge cases (Phase 4-6 hardening)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_patch_rejects_unknown_field(
    client: AsyncClient, db_session: AsyncSession, member: User
) -> None:
    """A typo'd field name is rejected (422), not silently accepted as a no-op."""
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"staus": "closed"},  # deliberate typo
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_patch_status_and_assignee_emits_one_signal(
    client: AsyncClient,
    db_session: AsyncSession,
    member: User,
    stub_slack_notify: list[dict],
) -> None:
    """A combined status + assignee change posts exactly one Slack status note."""
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "in_review", "assignee_user_id": str(member.id)},
    )
    assert resp.status_code == 200, resp.text
    assert len(stub_slack_notify) == 1
    assert stub_slack_notify[0]["status"] == "in_review"


@pytest.mark.integration
async def test_patch_status_skips_signal_when_workspace_soft_deleted(
    client: AsyncClient,
    db_session: AsyncSession,
    member: User,
    stub_slack_notify: list[dict],
) -> None:
    """If the workspace is soft-deleted, the status change still succeeds but no
    Slack post is attempted — fail-safe, never post into a disconnected team."""
    ws = await _make_workspace(db_session)
    esc = await _make_escalation(db_session, ws, status="new")
    ws.deleted_at = datetime.now(UTC)
    await db_session.flush()

    resp = await client.patch(
        f"/api/v1/escalations/{esc.id}",
        headers=_bearer(member),
        json={"status": "in_review"},
    )
    assert resp.status_code == 200, resp.text
    assert stub_slack_notify == []
