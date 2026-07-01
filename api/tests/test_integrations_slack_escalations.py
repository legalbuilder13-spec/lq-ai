"""Integration tests for the slack-bridge escalation intake endpoint (Slice A, Phase 3a).

Covers ``POST /api/v1/integrations/slack/escalations`` — the bridge-auth
surface the slack-bridge calls after it has verified a Slack submission:

* Auth — valid bridge bearer → 201; missing/wrong bearer → 401.
* Creates exactly one escalation bound to the resolved workspace, with the
  verified Slack requester identity, status ``new``.
* Writes an ``escalation.created`` audit row carrying ids/counts only — never
  the question text (invariant P3) — with a null actor (the requester is a
  Slack identity, not an lq-ai user).
* Unknown or soft-deleted workspace → 404.
* Empty question → 422 (schema validation).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.main import app
from app.models import AuditLog, Escalation, SlackWorkspace
from app.security.encryption import generate_master_key

BRIDGE_TOKEN = "bridge-token-fixture-value"


def _override_get_db(db_session: AsyncSession):
    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    return _override


@pytest_asyncio.fixture
async def configured_settings(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    master_key = generate_master_key()
    monkeypatch.setenv("LQ_AI_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("LQ_AI_BRIDGE_MASTER_KEY", master_key)
    get_settings.cache_clear()
    yield master_key
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    configured_settings: str,
) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = _override_get_db(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture(autouse=True)
async def _enable_capture(db_session: AsyncSession) -> None:
    """Phase 5 added a deployment-wide capture switch that defaults OFF.

    These Phase 3 intake tests predate it and assume capture is on, so enable
    it for every test in this module. The disabled path is covered explicitly
    by ``test_create_escalation_disabled_refused`` below.
    """

    from app.escalation_config import set_escalation_enabled

    await set_escalation_enabled(db_session, enabled=True)
    await db_session.flush()


async def _seed_workspace(
    db_session: AsyncSession, *, team_id: str = "T01234567", deleted: bool = False
) -> SlackWorkspace:
    ws = SlackWorkspace(
        team_id=team_id,
        team_name="Acme Legal",
        bot_token_encrypted=b"ciphertext",
        bot_user_id="U0BOT",
        installer_slack_user_id="U0INSTALL",
        scope="commands,chat:write",
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


def _escalation_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "team_id": "T01234567",
        "requester_slack_user_id": "U0REQ",
        "requester_slack_display_name": "Dana PM",
        "slack_channel_id": "C0CHAN",
        "slack_thread_ts": "1700000000.000100",
        "question": "Can we use this indemnity clause in the MSA?",
        "links": ["https://example.com/policy"],
    }
    base.update(overrides)
    return base


@pytest.mark.integration
async def test_create_escalation_valid_bearer_201(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_workspace(db_session)

    res = await client.post(
        "/api/v1/integrations/slack/escalations",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
        json=_escalation_body(),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "new"
    assert body["slack_thread_ts"] == "1700000000.000100"
    new_id = body["id"]

    persisted = (
        await db_session.execute(select(Escalation).where(Escalation.id == new_id))
    ).scalar_one()
    assert persisted.requester_slack_user_id == "U0REQ"
    assert persisted.question == "Can we use this indemnity clause in the MSA?"
    assert persisted.status == "new"

    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "escalation.created",
                    AuditLog.resource_id == str(new_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].user_id is None  # filed from Slack, no lq-ai actor
    details = audits[0].details or {}
    assert details.get("status") == "new"
    assert "question" not in details  # P3: no content in the audit row


@pytest.mark.integration
async def test_create_escalation_without_bearer_401(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_workspace(db_session)
    res = await client.post("/api/v1/integrations/slack/escalations", json=_escalation_body())
    assert res.status_code == 401


@pytest.mark.integration
async def test_create_escalation_wrong_bearer_401(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_workspace(db_session)
    res = await client.post(
        "/api/v1/integrations/slack/escalations",
        headers={"Authorization": "Bearer not-the-real-token"},
        json=_escalation_body(),
    )
    assert res.status_code == 401


@pytest.mark.integration
async def test_create_escalation_unknown_team_404(client: AsyncClient) -> None:
    res = await client.post(
        "/api/v1/integrations/slack/escalations",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
        json=_escalation_body(team_id="T-not-connected"),
    )
    assert res.status_code == 404, res.text


@pytest.mark.integration
async def test_create_escalation_soft_deleted_workspace_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_workspace(db_session, team_id="T-DELETED", deleted=True)
    res = await client.post(
        "/api/v1/integrations/slack/escalations",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
        json=_escalation_body(team_id="T-DELETED"),
    )
    assert res.status_code == 404, res.text


@pytest.mark.integration
async def test_create_escalation_empty_question_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_workspace(db_session)
    res = await client.post(
        "/api/v1/integrations/slack/escalations",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
        json=_escalation_body(question=""),
    )
    assert res.status_code == 422, res.text


@pytest.mark.integration
async def test_create_escalation_disabled_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Phase 5: when the operator has capture switched off, intake fails closed
    with 403 and creates nothing — even with a valid bearer and workspace."""

    from app.escalation_config import set_escalation_enabled

    await _seed_workspace(db_session)
    # Override the autouse enable: capture OFF for this test.
    await set_escalation_enabled(db_session, enabled=False)
    await db_session.flush()

    res = await client.post(
        "/api/v1/integrations/slack/escalations",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
        json=_escalation_body(),
    )
    assert res.status_code == 403, res.text

    rows = (await db_session.execute(select(Escalation))).scalars().all()
    assert rows == []
