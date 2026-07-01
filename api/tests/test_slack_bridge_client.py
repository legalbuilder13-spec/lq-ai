"""Unit test for the backend → slack-bridge egress client (Slice A, Phase 4).

Pins the api → bridge contract: where it POSTs, the shared-secret bearer, and
the non-secret body. The HTTP client is stubbed — no network, no live bridge.
"""

from __future__ import annotations

import pytest

from app.clients import slack_bridge


class _StubSettings:
    lq_ai_bridge_url = "http://bridge.test"
    lq_ai_bridge_token = "tok-123"


@pytest.mark.unit
async def test_post_escalation_status_update_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(self, url: str, *, headers=None, json=None):  # type: ignore[no-untyped-def]
            captured.update(url=url, headers=headers, json=json)
            return _Resp()

    monkeypatch.setattr(slack_bridge.httpx, "AsyncClient", _Client)

    await slack_bridge.post_escalation_status_update(
        _StubSettings(),  # type: ignore[arg-type]
        team_id="T1",
        channel_id="C1",
        thread_ts="1700000000.000100",
        status="answered",
    )

    assert captured["url"] == "http://bridge.test/internal/escalations/status-post"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"  # type: ignore[index]
    assert captured["json"] == {
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "1700000000.000100",
        "status": "answered",
    }


@pytest.mark.unit
async def test_post_escalation_status_update_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(self, url: str, *, headers=None, json=None):  # type: ignore[no-untyped-def]
            captured["url"] = url
            return _Resp()

    class _SlashSettings:
        lq_ai_bridge_url = "http://bridge.test/"
        lq_ai_bridge_token = "tok"

    monkeypatch.setattr(slack_bridge.httpx, "AsyncClient", _Client)

    await slack_bridge.post_escalation_status_update(
        _SlashSettings(),  # type: ignore[arg-type]
        team_id="T1",
        channel_id="C1",
        thread_ts="1.2",
        status="closed",
    )

    # No double slash — the trailing slash on the base URL is stripped.
    assert captured["url"] == "http://bridge.test/internal/escalations/status-post"
