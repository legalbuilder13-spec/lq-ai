"""LQ.AI Slack Bridge — FastAPI entry point (M3-D1).

The bridge surface is intentionally tiny at v0.3.0:

* ``GET  /healthz`` — liveness. Always 200 once the process is up.
* ``GET  /readyz`` — readiness. 200 when ``LQ_AI_BACKEND_URL`` is
  reachable; 503 otherwise. Operators wire this into their orchestrator
  to gate traffic to the bridge.
* ``GET  /slack/oauth/install`` — kicks off the OAuth install flow.
  See ``app.oauth``.
* ``GET  /slack/oauth/callback`` — receives Slack's redirect after the
  user consents.
* ``POST /slack/events`` — inbound webhook from Slack. At v0.3.0 the
  bridge verifies the signature and returns 200; the handler stub is
  the foundation M3-D2 (slash commands, descoped to M4 per DE-288)
  will fill in.

Everything else — slash commands, message handlers, per-user identity
binding — is M3-D2 / M3-D4 / community contribution scope.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .config import Settings, get_settings
from .escalations import (
    ESCALATION_CALLBACK_ID,
    build_confirmation_text,
    build_intake_body,
    build_intake_modal,
    build_status_update_text,
    parse_intake_submission,
)
from .oauth import router as oauth_router
from .observability import init_otel

log = logging.getLogger(__name__)


async def _fetch_bot_token(cfg: Settings, team_id: str) -> str:
    """Fetch the workspace bot token from the api (bridge-auth).

    Invariant P1: the api holds the token encrypted; the bridge is the only
    component that talks to Slack, so it fetches the decrypted token at call
    time to open the modal / post messages. Factored out as a module-level
    function so tests can substitute a stub. Raises on a non-2xx api response.
    """

    async with httpx.AsyncClient(timeout=10.0) as http:
        res = await http.get(
            f"{cfg.lq_ai_backend_url.rstrip('/')}"
            f"/api/v1/integrations/slack/workspaces/{team_id}/bot-token",
            headers={"Authorization": f"Bearer {cfg.lq_ai_bridge_token}"},
        )
    res.raise_for_status()
    return str(res.json()["bot_token"])


async def _post_escalation_intake(cfg: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """POST a captured escalation to the api intake endpoint (bridge-auth).

    Returns the api's confirmation body (id + status + thread). Raises on a
    non-2xx response. Factored out so tests can substitute a stub.
    """

    async with httpx.AsyncClient(timeout=10.0) as http:
        res = await http.post(
            f"{cfg.lq_ai_backend_url.rstrip('/')}/api/v1/integrations/slack/escalations",
            headers={"Authorization": f"Bearer {cfg.lq_ai_bridge_token}"},
            json=body,
        )
    res.raise_for_status()
    return dict(res.json())


def _make_slack_client(token: str) -> Any:  # slack_sdk type imported lazily
    """Build a Slack Web API client. Factored out so tests can substitute a
    stub without patching slack_sdk internals."""

    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=token)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Factored out of module-level instantiation so tests can construct
    isolated app instances with their own ``Settings`` overrides.
    """

    cfg = settings or get_settings()

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="LQ.AI Slack Bridge",
        version="0.1.0",
        description=(
            "OAuth install + workspace persistence for the LQ.AI Slack "
            "integration. M3-D1 plumbing. Slash-command surface deferred "
            "to M4 / community per DE-288."
        ),
    )

    init_otel(cfg)
    FastAPIInstrumentor.instrument_app(app)

    app.include_router(oauth_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness check — verifies the LQ.AI api is reachable.

        The bridge is useless without the api: every OAuth callback
        ends with a POST to the api's bridge-facing persistence
        endpoint. If the api is down, the bridge should report unready
        so operators see the dependency failure rather than a
        confusing OAuth callback error.
        """

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{cfg.lq_ai_backend_url}/healthz")
                if res.status_code != 200:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "status": "unready",
                            "reason": f"backend returned {res.status_code}",
                        },
                    )
        except (httpx.HTTPError, OSError) as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "unready", "reason": f"backend unreachable: {exc}"},
            )
        return JSONResponse(content={"status": "ok"})

    @app.post("/slack/events")
    async def slack_events(request: Request) -> dict[str, str]:
        """Inbound webhook stub.

        Verifies the Slack signature (per the Events API requirement)
        and returns 200. The handler body is left for M3-D2 / community
        contribution to fill in once the slash-command surface lands.

        Even at v0.3.0, signature verification matters: it prevents an
        attacker from POSTing fake events at the bridge and getting
        any observable response. The signature check is the substrate
        the slash-command handler will rely on.
        """

        from .signing import verify_slack_signature

        body = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(
            signing_secret=cfg.slack_signing_secret,
            timestamp=timestamp,
            body=body,
            signature=signature,
        ):
            raise HTTPException(status_code=401, detail="invalid Slack signature")

        # Slack's "url_verification" handshake — Slack sends a one-off
        # POST with `{"type":"url_verification","challenge":"..."}`
        # when the operator configures the Events API URL. Responding
        # with the challenge value confirms the URL belongs to the
        # bridge.
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        # All other event types are a no-op at v0.3.0 — M3-D2 will
        # extend this branch.
        return {"status": "ok"}

    def _verify(request: Request, body: bytes) -> None:
        """Signature-gate an inbound Slack request; raise 401 on failure."""

        from .signing import verify_slack_signature

        if not verify_slack_signature(
            signing_secret=cfg.slack_signing_secret,
            timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
            body=body,
            signature=request.headers.get("X-Slack-Signature", ""),
        ):
            raise HTTPException(status_code=401, detail="invalid Slack signature")

    def _verify_internal(request: Request) -> None:
        """Authenticate a trusted api → bridge internal call by shared bearer.

        The api cannot produce a Slack HMAC signature, so internal calls from
        the backend authenticate with the same ``LQ_AI_BRIDGE_TOKEN`` shared
        secret the bridge already presents to the api (constant-time matched;
        ADR 0022). Fails closed: a missing/empty configured token rejects all
        callers, mirroring the api's ``require_bridge_auth``.
        """

        expected = f"Bearer {cfg.lq_ai_bridge_token}"
        presented = request.headers.get("Authorization", "")
        if not (cfg.lq_ai_bridge_token and presented and hmac.compare_digest(presented, expected)):
            raise HTTPException(status_code=401, detail="invalid bridge token")

    @app.post("/internal/escalations/status-post")
    async def escalation_status_post(request: Request) -> Response:
        """Post an escalation's new status into its Slack thread (api-triggered).

        Invariant P1: the backend never calls Slack. When a legal user changes
        an escalation's status, the backend signals this internal endpoint with
        the escalation's Slack coordinates + the new status; the bridge fetches
        the workspace bot token and posts the update as a threaded reply. The
        bot token is never carried in the request — the bridge resolves it via
        its own ``/bot-token`` hand-off. A Slack failure surfaces as 502 so the
        backend can log the (best-effort) failure.
        """

        _verify_internal(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="malformed body") from exc

        team_id = str(payload.get("team_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        thread_ts = str(payload.get("thread_ts") or "")
        status = str(payload.get("status") or "")
        if not (team_id and channel_id and thread_ts and status):
            raise HTTPException(status_code=400, detail="missing status-post fields")

        try:
            token = await _fetch_bot_token(cfg, team_id)
            client = _make_slack_client(token)
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=build_status_update_text(status),
            )
        except Exception as exc:
            log.exception("slack.status_post.failed team_id=%s", team_id)
            raise HTTPException(status_code=502, detail="status post failed") from exc
        return Response(status_code=200)

    @app.post("/internal/escalations/message-delete")
    async def escalation_message_delete(request: Request) -> Response:
        """Delete an escalation's Slack message (api-triggered deletion-on-request).

        Invariant P1: the backend never calls Slack. On an operator
        deletion-on-request the backend signals this endpoint with the
        escalation's Slack coordinates; the bridge fetches the bot token and
        deletes the thread-root message it posted at capture time. Failure
        surfaces as 502 so the backend can log the (best-effort) failure.
        """

        _verify_internal(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="malformed body") from exc

        team_id = str(payload.get("team_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        thread_ts = str(payload.get("thread_ts") or "")
        if not (team_id and channel_id and thread_ts):
            raise HTTPException(status_code=400, detail="missing message-delete fields")

        try:
            token = await _fetch_bot_token(cfg, team_id)
            client = _make_slack_client(token)
            await client.chat_delete(channel=channel_id, ts=thread_ts)
        except Exception as exc:
            log.exception("slack.message_delete.failed team_id=%s", team_id)
            raise HTTPException(status_code=502, detail="message delete failed") from exc
        return Response(status_code=200)

    @app.post("/slack/commands", response_model=None)
    async def slack_commands(request: Request) -> Response | dict[str, str]:
        """Slash-command entry point — open the legal-escalation intake modal.

        Slack posts a signature-verified, form-encoded request when a user
        runs the configured slash command. We open a modal (which needs the
        workspace bot token, fetched from the api) and ack with an empty 200
        so nothing is posted in-channel until the user submits.
        """

        body = await request.body()
        _verify(request, body)
        form = await request.form()
        team_id = str(form.get("team_id") or "")
        channel_id = str(form.get("channel_id") or "")
        trigger_id = str(form.get("trigger_id") or "")
        if not (team_id and trigger_id):
            raise HTTPException(status_code=400, detail="missing slash-command fields")

        try:
            token = await _fetch_bot_token(cfg, team_id)
            client = _make_slack_client(token)
            await client.views_open(
                trigger_id=trigger_id,
                view=build_intake_modal(team_id=team_id, channel_id=channel_id),
            )
        except Exception:
            log.exception("slack.commands.open_modal_failed team_id=%s", team_id)
            return {
                "response_type": "ephemeral",
                "text": "Sorry — could not open the legal intake form. Please try again.",
            }
        return Response(status_code=200)

    @app.post("/slack/interactivity", response_model=None)
    async def slack_interactivity(request: Request) -> Response | dict[str, object]:
        """Interactive-component entry point — capture a submitted modal.

        On the escalation modal's ``view_submission`` we post a placeholder
        message to the originating channel (its ts becomes the escalation's
        thread), file the escalation via the api intake endpoint, then update
        the message to confirm with the record id. An empty 200 closes the
        modal. All Slack egress happens here in the bridge, never in the api
        (invariant P1).

        v1 does this work inline; if it ever risks Slack's 3s ack window,
        moving the post-ack work to a background task is the polish.
        """

        body = await request.body()
        _verify(request, body)
        form = await request.form()
        raw = form.get("payload")
        if not raw:
            raise HTTPException(status_code=400, detail="missing interactivity payload")
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="malformed interactivity payload") from exc

        is_escalation = (
            isinstance(payload, dict)
            and payload.get("type") == "view_submission"
            and (payload.get("view") or {}).get("callback_id") == ESCALATION_CALLBACK_ID
        )
        if not is_escalation:
            # Some other interactive component — ack so Slack doesn't retry.
            return Response(status_code=200)

        parsed = parse_intake_submission(payload)
        if not (parsed.question and parsed.team_id and parsed.channel_id):
            return {
                "response_action": "errors",
                "errors": {"question": "Something went wrong reading the form — please try again."},
            }

        try:
            token = await _fetch_bot_token(cfg, parsed.team_id)
            client = _make_slack_client(token)
            posted = await client.chat_postMessage(
                channel=parsed.channel_id,
                text=":hourglass_flowing_sand: Filing your question to Legal…",
            )
            thread_ts = str(posted["ts"])
            result = await _post_escalation_intake(
                cfg, build_intake_body(parsed, slack_thread_ts=thread_ts)
            )
            await client.chat_update(
                channel=parsed.channel_id,
                ts=thread_ts,
                text=build_confirmation_text(str(result["id"])),
            )
        except Exception:
            log.exception("slack.interactivity.capture_failed team_id=%s", parsed.team_id)
        # Empty 200 closes the modal regardless; capture failures are logged.
        return Response(status_code=200)

    return app


app = create_app()
