"""Handler tests for the api → bridge status-post endpoint (Slice A, Phase 4).

``POST /internal/escalations/status-post`` is the reverse leg of the bridge
channel: the backend (not Slack) calls it to have the bridge post an
escalation's new status into its thread. It authenticates with the shared
``LQ_AI_BRIDGE_TOKEN`` bearer (the api cannot produce a Slack signature), then
fetches the bot token and posts. The network calls (token fetch, Slack Web API)
are the module-level stubs from ``app.main``; the status-update copy is the pure
helper in ``app.escalations``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.escalations import build_status_update_text
from app.main import create_app

BRIDGE_TOKEN = "bridge-token-fixture"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        slack_client_id="A123CLIENT",
        slack_client_secret="A123SECRET",
        slack_signing_secret="A123SIGNING",
        lq_ai_backend_url="http://api.test",
        lq_ai_bridge_token=BRIDGE_TOKEN,
        lq_ai_bridge_public_url="https://bridge.test",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


class _StubSlackClient:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.deleted: list[dict] = []

    async def chat_postMessage(self, **kwargs: object) -> dict:
        self.posted.append(kwargs)
        return {"ok": True, "ts": "1700000000.000300"}

    async def chat_delete(self, **kwargs: object) -> dict:
        self.deleted.append(kwargs)
        return {"ok": True}


def _auth(token: str = BRIDGE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_BODY = {
    "team_id": "T01",
    "channel_id": "C99",
    "thread_ts": "1700000000.000100",
    "status": "in_review",
}


def test_status_post_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubSlackClient()

    async def _fake_token(cfg: object, team_id: str) -> str:
        return "xoxb-fake"

    monkeypatch.setattr("app.main._fetch_bot_token", _fake_token)
    monkeypatch.setattr("app.main._make_slack_client", lambda token: stub)

    res = client.post("/internal/escalations/status-post", headers=_auth(), json=_BODY)
    assert res.status_code == 200, res.text
    assert stub.posted, "expected a threaded reply to be posted"
    posted = stub.posted[0]
    assert posted["channel"] == "C99"
    assert posted["thread_ts"] == "1700000000.000100"
    assert "review" in str(posted["text"]).lower()


def test_status_post_bad_bearer_returns_401(client: TestClient) -> None:
    res = client.post("/internal/escalations/status-post", headers=_auth("wrong"), json=_BODY)
    assert res.status_code == 401


def test_status_post_missing_bearer_returns_401(client: TestClient) -> None:
    res = client.post("/internal/escalations/status-post", json=_BODY)
    assert res.status_code == 401


def test_status_post_missing_fields_returns_400(client: TestClient) -> None:
    res = client.post(
        "/internal/escalations/status-post",
        headers=_auth(),
        json={"team_id": "T01"},
    )
    assert res.status_code == 400


def test_status_post_slack_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_token(cfg: object, team_id: str) -> str:
        return "xoxb-fake"

    class _BoomClient:
        async def chat_postMessage(self, **kwargs: object) -> dict:
            raise RuntimeError("slack down")

    monkeypatch.setattr("app.main._fetch_bot_token", _fake_token)
    monkeypatch.setattr("app.main._make_slack_client", lambda token: _BoomClient())

    res = client.post("/internal/escalations/status-post", headers=_auth(), json=_BODY)
    assert res.status_code == 502


def test_build_status_update_text_covers_lifecycle() -> None:
    texts = {
        status: build_status_update_text(status)
        for status in ("new", "in_review", "answered", "closed")
    }
    # Every lifecycle status yields a non-empty, distinct message.
    assert all(texts.values())
    assert len(set(texts.values())) == 4
    # An unexpected status is still reported (raw value echoed), not dropped.
    assert "weird_status" in build_status_update_text("weird_status")


# ---------------------------------------------------------------------------
# /internal/escalations/message-delete (Phase 6)
# ---------------------------------------------------------------------------


_DELETE_BODY = {
    "team_id": "T01",
    "channel_id": "C99",
    "thread_ts": "1700000000.000100",
}


def test_message_delete_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubSlackClient()

    async def _fake_token(cfg: object, team_id: str) -> str:
        return "xoxb-fake"

    monkeypatch.setattr("app.main._fetch_bot_token", _fake_token)
    monkeypatch.setattr("app.main._make_slack_client", lambda token: stub)

    res = client.post("/internal/escalations/message-delete", headers=_auth(), json=_DELETE_BODY)
    assert res.status_code == 200, res.text
    assert stub.deleted, "expected the thread-root message to be deleted"
    assert stub.deleted[0]["channel"] == "C99"
    assert stub.deleted[0]["ts"] == "1700000000.000100"


def test_message_delete_bad_bearer_returns_401(client: TestClient) -> None:
    res = client.post(
        "/internal/escalations/message-delete",
        headers=_auth("wrong"),
        json=_DELETE_BODY,
    )
    assert res.status_code == 401


def test_message_delete_missing_fields_returns_400(client: TestClient) -> None:
    res = client.post(
        "/internal/escalations/message-delete",
        headers=_auth(),
        json={"team_id": "T01"},
    )
    assert res.status_code == 400


def test_message_delete_slack_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_token(cfg: object, team_id: str) -> str:
        return "xoxb-fake"

    class _BoomClient:
        async def chat_delete(self, **kwargs: object) -> dict:
            raise RuntimeError("slack down")

    monkeypatch.setattr("app.main._fetch_bot_token", _fake_token)
    monkeypatch.setattr("app.main._make_slack_client", lambda token: _BoomClient())

    res = client.post("/internal/escalations/message-delete", headers=_auth(), json=_DELETE_BODY)
    assert res.status_code == 502
