"""Integration tests for the slack-bridge bot-token hand-off endpoint (Slice A, Phase 3b).

Covers ``GET /api/v1/integrations/slack/workspaces/{team_id}/bot-token`` — the
bridge-auth surface that hands the slack-bridge the decrypted workspace bot
token so the bridge (never the backend, per invariant P1) can perform Slack
egress: opening the intake modal and posting confirmations/status updates.

* Auth — valid bridge bearer → 200; missing/wrong bearer → 401.
* Returns the decrypted token (round-trips the at-rest ciphertext).
* Unknown or soft-deleted workspace → 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.main import app
from app.models import SlackWorkspace
from app.security.encryption import BridgeTokenEncryptor, generate_master_key

BRIDGE_TOKEN = "bridge-token-fixture-value"
KNOWN_BOT_TOKEN = "xoxb-known-fixture-token"


def _override_get_db(db_session: AsyncSession):
    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    return _override


@pytest_asyncio.fixture
async def master_key(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    key = generate_master_key()
    monkeypatch.setenv("LQ_AI_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("LQ_AI_BRIDGE_MASTER_KEY", key)
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, master_key: str) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = _override_get_db(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def _seed_workspace(
    db_session: AsyncSession,
    master_key: str,
    *,
    team_id: str = "T01234567",
    deleted: bool = False,
) -> SlackWorkspace:
    ws = SlackWorkspace(
        team_id=team_id,
        team_name="Acme Legal",
        bot_token_encrypted=BridgeTokenEncryptor(master_key=master_key).encrypt(KNOWN_BOT_TOKEN),
        bot_user_id="U0BOT",
        installer_slack_user_id="U0INSTALL",
        scope="commands,chat:write",
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


@pytest.mark.integration
async def test_get_bot_token_valid_bearer_returns_decrypted(
    client: AsyncClient, db_session: AsyncSession, master_key: str
) -> None:
    await _seed_workspace(db_session, master_key)
    res = await client.get(
        "/api/v1/integrations/slack/workspaces/T01234567/bot-token",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["bot_token"] == KNOWN_BOT_TOKEN
    assert body["bot_user_id"] == "U0BOT"
    assert body["team_id"] == "T01234567"


@pytest.mark.integration
async def test_get_bot_token_without_bearer_401(
    client: AsyncClient, db_session: AsyncSession, master_key: str
) -> None:
    await _seed_workspace(db_session, master_key)
    res = await client.get("/api/v1/integrations/slack/workspaces/T01234567/bot-token")
    assert res.status_code == 401


@pytest.mark.integration
async def test_get_bot_token_wrong_bearer_401(
    client: AsyncClient, db_session: AsyncSession, master_key: str
) -> None:
    await _seed_workspace(db_session, master_key)
    res = await client.get(
        "/api/v1/integrations/slack/workspaces/T01234567/bot-token",
        headers={"Authorization": "Bearer nope"},
    )
    assert res.status_code == 401


@pytest.mark.integration
async def test_get_bot_token_unknown_team_404(client: AsyncClient) -> None:
    res = await client.get(
        "/api/v1/integrations/slack/workspaces/T-unknown/bot-token",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
    )
    assert res.status_code == 404, res.text


@pytest.mark.integration
async def test_get_bot_token_soft_deleted_404(
    client: AsyncClient, db_session: AsyncSession, master_key: str
) -> None:
    await _seed_workspace(db_session, master_key, team_id="T-DELETED", deleted=True)
    res = await client.get(
        "/api/v1/integrations/slack/workspaces/T-DELETED/bot-token",
        headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"},
    )
    assert res.status_code == 404, res.text
