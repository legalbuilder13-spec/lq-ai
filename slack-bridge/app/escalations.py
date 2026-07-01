"""Pure helpers for the Slack escalation-capture surface (Slice A, Phase 3b).

No I/O. These build the Slack intake modal, parse the modal submission, split
the links field, and shape the bridge -> api intake body. The signature-gated
I/O handlers in :mod:`app.main` verify the request, fetch the workspace bot
token, call Slack, and call the api; they delegate the deterministic shaping
to the functions here so it can be unit-tested without a live Slack or api.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

ESCALATION_CALLBACK_ID = "lq_escalation_submit"
"""``callback_id`` on the modal; the interactivity handler routes on it."""

_QUESTION_BLOCK = "question"
_LINKS_BLOCK = "links"
_INPUT_ACTION = "value"
_MAX_LINKS = 20


def build_intake_modal(*, team_id: str, channel_id: str) -> dict[str, Any]:
    """Build the Slack modal view for filing a legal escalation.

    ``private_metadata`` carries the team + originating channel: a modal
    submission does not otherwise report which channel the slash command ran
    in, and the bridge needs it to attribute the escalation and post the
    confirmation back to the right place.
    """

    return {
        "type": "modal",
        "callback_id": ESCALATION_CALLBACK_ID,
        "private_metadata": json.dumps({"team_id": team_id, "channel_id": channel_id}),
        "title": {"type": "plain_text", "text": "Ask Legal"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": _QUESTION_BLOCK,
                "label": {"type": "plain_text", "text": "Your legal question"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _INPUT_ACTION,
                    "multiline": True,
                },
            },
            {
                "type": "input",
                "block_id": _LINKS_BLOCK,
                "optional": True,
                "label": {"type": "plain_text", "text": "Related links (one per line)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _INPUT_ACTION,
                    "multiline": True,
                },
            },
        ],
    }


def parse_links(raw: str | None) -> list[str]:
    """Split a free-text links field into a de-duplicated, capped list.

    Lines and commas both separate; whitespace is stripped; empty entries are
    dropped; first-seen order is preserved; the result is capped at the api's
    link limit so an oversize paste cannot blow past validation.
    """

    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        link = line.strip()
        if not link or link in seen:
            continue
        seen.add(link)
        out.append(link)
        if len(out) >= _MAX_LINKS:
            break
    return out


@dataclass
class ParsedSubmission:
    """The escalation fields extracted from a Slack ``view_submission``."""

    team_id: str
    channel_id: str
    requester_slack_user_id: str
    requester_slack_display_name: str | None
    question: str
    links: list[str] = field(default_factory=list)


def parse_intake_submission(payload: dict[str, Any]) -> ParsedSubmission:
    """Extract the escalation fields from a Slack ``view_submission`` payload.

    The requester identity is taken from the verified ``user`` block Slack
    populates on the interaction — never from free text — so the recorded
    requester cannot be spoofed by form content.
    """

    view = payload.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    values = (view.get("state") or {}).get("values") or {}
    question = (
        ((values.get(_QUESTION_BLOCK) or {}).get(_INPUT_ACTION) or {}).get("value") or ""
    ).strip()
    links_raw = ((values.get(_LINKS_BLOCK) or {}).get(_INPUT_ACTION) or {}).get("value")

    user = payload.get("user") or {}
    team = payload.get("team") or {}
    return ParsedSubmission(
        team_id=team.get("id") or meta.get("team_id") or "",
        channel_id=meta.get("channel_id") or "",
        requester_slack_user_id=user.get("id") or "",
        requester_slack_display_name=user.get("username") or user.get("name"),
        question=question,
        links=parse_links(links_raw),
    )


def build_intake_body(parsed: ParsedSubmission, *, slack_thread_ts: str) -> dict[str, Any]:
    """Shape the ``EscalationCreate`` body the bridge POSTs to the api."""

    return {
        "team_id": parsed.team_id,
        "requester_slack_user_id": parsed.requester_slack_user_id,
        "requester_slack_display_name": parsed.requester_slack_display_name,
        "slack_channel_id": parsed.channel_id,
        "slack_thread_ts": slack_thread_ts,
        "question": parsed.question,
        "links": parsed.links,
    }


def build_confirmation_text(escalation_id: str) -> str:
    """The message the bridge posts back into the channel/thread on capture."""

    return (
        ":white_check_mark: Your question has been sent to Legal and logged as "
        f"escalation `{escalation_id}`. A member of the legal team will follow up here."
    )


_STATUS_UPDATE_TEXT = {
    "new": ":new: Legal reset this question to *New*.",
    "in_review": ":eyes: Legal is now *reviewing* this question.",
    "answered": ":white_check_mark: Legal has responded — see the thread.",
    "closed": ":lock: Legal has *closed* this question.",
}


def build_status_update_text(status: str) -> str:
    """The threaded reply the bridge posts when an escalation's status changes.

    Names only the new status — never the question content. Falls back to a
    generic line for any status outside the fixed lifecycle, so an unexpected
    value is still reported rather than silently dropped.
    """

    return _STATUS_UPDATE_TEXT.get(
        status, f"Legal updated the status of this question to *{status}*."
    )
