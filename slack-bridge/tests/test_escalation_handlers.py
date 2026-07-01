"""Handler tests for the Slack escalation-capture I/O endpoints (Slice A, Phase 3b).

Covers the signature gate and the happy paths of ``POST /slack/commands`` and
``POST /slack/interactivity``. The network calls (api token fetch, api intake,
Slack Web API) are factored into module-level functions in ``app.main`` so
they're substituted with stubs here — the pure shaping logic is tested
separately in ``test_escalations_helpers``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.escalations import ESCALATION_CALLBACK_ID
from app.main import create_app

SIGNING_SECRET = "A123SIGNING"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        slack_client_id="A123CLIENT",
        slack_client_secret="A123SECRET",
        slack_signing_secret=SIGNING_SECRET,
        lq_ai_backend_url="http://api.test",
        lq_ai_bridge_token="bridge-token-fixture",
        lq_ai_bridge_public_url="https://bridge.test",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _signed(body: bytes, *, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


class _StubSlackClient:
    def __init__(self) -> None:
        self.views_open_kwargs: dict | None = None
        self.posted: list[dict] = []
        self.updated: list[dict] = []

    async def views_open(self, **kwargs: object) -> dict:
        self.views_open_kwargs = kwargs
        return {"ok": True}

    async def chat_postMessage(self, **kwargs: object) -> dict:
        self.posted.append(kwargs)
        return {"ok": True, "ts": "1700000000.000200"}

    async def chat_update(self, **kwargs: object) -> dict:
        self.updated.append(kwargs)
        return {"ok": True}


# ---------------------------------------------------------------------------
# /slack/commands
# ---------------------------------------------------------------------------


def test_commands_bad_signature_returns_401(client: TestClient) -> None:
    body = urllib.parse.urlencode(
        {"team_id": "T01", "channel_id": "C99", "trigger_id": "trig", "command": "/legal"}
    ).encode()
    res = client.post(
        "/slack/commands",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=deadbeef",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert res.status_code == 401


def test_commands_opens_intake_modal(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubSlackClient()

    async def _fake_token(cfg: object, team_id: str) -> str:
        return "xoxb-fake"

    monkeypatch.setattr("app.main._fetch_bot_token", _fake_token)
    monkeypatch.setattr("app.main._make_slack_client", lambda token: stub)

    body = urllib.parse.urlencode(
        {"team_id": "T01", "channel_id": "C99", "trigger_id": "trig-123", "command": "/legal"}
    ).encode()
    res = client.post("/slack/commands", content=body, headers=_signed(body))
    assert res.status_code == 200, res.text
    assert stub.views_open_kwargs is not None
    assert stub.views_open_kwargs["trigger_id"] == "trig-123"
    assert stub.views_open_kwargs["view"]["callback_id"] == ESCALATION_CALLBACK_ID


# ---------------------------------------------------------------------------
# /slack/interactivity
# ---------------------------------------------------------------------------


def _view_submission_body() -> bytes:
    payload = {
        "type": "view_submission",
        "user": {"id": "U0REQ", "username": "dana"},
        "team": {"id": "T01"},
        "view": {
            "callback_id": ESCALATION_CALLBACK_ID,
            "private_metadata": json.dumps({"team_id": "T01", "channel_id": "C99"}),
            "state": {
                "values": {
                    "question": {"value": {"value": "Can we use this clause?"}},
                    "links": {"value": {"value": ""}},
                }
            },
        },
    }
    return ("payload=" + urllib.parse.quote(json.dumps(payload))).encode()


def test_interactivity_bad_signature_returns_401(client: TestClient) -> None:
    body = _view_submission_body()
    res = client.post(
        "/slack/interactivity",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=deadbeef",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert res.status_code == 401


def test_interactivity_files_escalation_and_confirms(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubSlackClient()
    captured: dict[str, dict] = {}

    async def _fake_token(cfg: object, team_id: str) -> str:
        return "xoxb-fake"

    async def _fake_intake(cfg: object, body: dict) -> dict:
        captured["body"] = body
        return {"id": "esc-1", "status": "new", "slack_thread_ts": body["slack_thread_ts"]}

    monkeypatch.setattr("app.main._fetch_bot_token", _fake_token)
    monkeypatch.setattr("app.main._make_slack_client", lambda token: stub)
    monkeypatch.setattr("app.main._post_escalation_intake", _fake_intake)

    body = _view_submission_body()
    res = client.post("/slack/interactivity", content=body, headers=_signed(body))
    assert res.status_code == 200, res.text

    # The api intake was called with the verified requester + the posted ts.
    sent = captured["body"]
    assert sent["question"] == "Can we use this clause?"
    assert sent["requester_slack_user_id"] == "U0REQ"
    assert sent["slack_channel_id"] == "C99"
    assert sent["slack_thread_ts"] == "1700000000.000200"

    # A confirmation was posted then updated to name the escalation id.
    assert stub.posted and stub.posted[0]["channel"] == "C99"
    assert stub.updated and "esc-1" in stub.updated[0]["text"]
