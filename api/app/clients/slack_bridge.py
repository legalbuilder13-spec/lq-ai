"""Backend → slack-bridge egress client (Legal Escalation Capture, Phase 4).

Invariant P1 (ADR 0016) routes *third-party* egress through the Inference
Gateway: the backend holds no third-party credentials and never calls a
third-party endpoint. This client is different in kind — it calls the
operator's *own* slack-bridge service over the trusted internal network, never
a third party. It is a second enumerated, audited egress door (ADR 0022), and
is the only api module besides the gateway client allowed to construct an
outbound HTTP client (see ``_EGRESS_ALLOWLIST`` in
``api/tests/test_transparency_invariants.py``).

The bridge — not the backend — is the only component that talks to Slack
(invariant P1 of the escalation feature). So this client signals the bridge
with the escalation's Slack coordinates and the new status only; the bridge
fetches the workspace bot token (its existing ``/bot-token`` hand-off) and
posts the update into the thread. No Slack token ever leaves the backend here.
"""

from __future__ import annotations

import httpx

from app.config import Settings


async def post_escalation_status_update(
    settings: Settings,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    status: str,
) -> None:
    """Ask the slack-bridge to post an escalation's new status into its thread.

    Sends only non-secret identifiers + the new status over the internal,
    shared-secret channel; the bridge resolves the bot token itself. Raises on
    a non-2xx bridge response so the caller can log the (best-effort) failure —
    the status change itself is already committed and durable.
    """

    async with httpx.AsyncClient(timeout=10.0) as http:
        res = await http.post(
            f"{settings.lq_ai_bridge_url.rstrip('/')}/internal/escalations/status-post",
            headers={"Authorization": f"Bearer {settings.lq_ai_bridge_token}"},
            json={
                "team_id": team_id,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "status": status,
            },
        )
    res.raise_for_status()


async def delete_escalation_message(
    settings: Settings,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    """Ask the slack-bridge to remove an escalation's Slack message.

    Used by operator deletion-on-request (Phase 6). Same first-party internal
    egress as the status post-back (ADR 0022): sends only identifiers; the
    bridge resolves the bot token and deletes the thread-root message it posted
    at capture time. Raises on a non-2xx bridge response so the caller can log
    the best-effort failure — the lq-ai-side deletion is already committed.
    """

    async with httpx.AsyncClient(timeout=10.0) as http:
        res = await http.post(
            f"{settings.lq_ai_bridge_url.rstrip('/')}/internal/escalations/message-delete",
            headers={"Authorization": f"Bearer {settings.lq_ai_bridge_token}"},
            json={
                "team_id": team_id,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
            },
        )
    res.raise_for_status()
