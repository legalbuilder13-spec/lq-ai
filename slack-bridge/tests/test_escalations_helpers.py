"""Unit tests for the pure escalation-capture helpers (Slice A, Phase 3b).

These cover the deterministic shaping logic — building the Slack modal,
parsing a view_submission payload, splitting the links field, and shaping
the bridge -> api intake body — with no I/O. The signature-gated I/O
handlers in app.main are exercised separately.
"""

from __future__ import annotations

import json

from app.escalations import (
    ESCALATION_CALLBACK_ID,
    ParsedSubmission,
    build_confirmation_text,
    build_intake_body,
    build_intake_modal,
    parse_intake_submission,
    parse_links,
)


def test_build_intake_modal_shape() -> None:
    view = build_intake_modal(team_id="T01", channel_id="C99")
    assert view["type"] == "modal"
    assert view["callback_id"] == ESCALATION_CALLBACK_ID
    # private_metadata round-trips the routing context.
    meta = json.loads(view["private_metadata"])
    assert meta == {"team_id": "T01", "channel_id": "C99"}
    block_ids = {b["block_id"] for b in view["blocks"]}
    assert {"question", "links"} <= block_ids
    # the question block is required; the links block is optional.
    by_id = {b["block_id"]: b for b in view["blocks"]}
    assert by_id["question"].get("optional") is not True
    assert by_id["links"].get("optional") is True


def test_parse_links_splits_dedupes_and_caps() -> None:
    assert parse_links(None) == []
    assert parse_links("") == []
    assert parse_links("https://a.com\nhttps://b.com") == ["https://a.com", "https://b.com"]
    # commas also separate; whitespace stripped; duplicates dropped (first-seen order).
    assert parse_links("https://a.com, https://a.com , https://c.com") == [
        "https://a.com",
        "https://c.com",
    ]
    # cap at 20.
    many = "\n".join(f"https://x{i}.com" for i in range(50))
    assert len(parse_links(many)) == 20


def _submission_payload() -> dict:
    return {
        "type": "view_submission",
        "user": {"id": "U0REQ", "username": "dana"},
        "team": {"id": "T01"},
        "view": {
            "callback_id": ESCALATION_CALLBACK_ID,
            "private_metadata": json.dumps({"team_id": "T01", "channel_id": "C99"}),
            "state": {
                "values": {
                    "question": {"value": {"value": "  Can we use this clause?  "}},
                    "links": {"value": {"value": "https://a.com\nhttps://a.com"}},
                }
            },
        },
    }


def test_parse_intake_submission_extracts_verified_identity() -> None:
    parsed = parse_intake_submission(_submission_payload())
    assert isinstance(parsed, ParsedSubmission)
    assert parsed.team_id == "T01"
    assert parsed.channel_id == "C99"
    assert parsed.requester_slack_user_id == "U0REQ"  # from the verified user block
    assert parsed.requester_slack_display_name == "dana"
    assert parsed.question == "Can we use this clause?"  # trimmed
    assert parsed.links == ["https://a.com"]  # de-duplicated


def test_build_intake_body_shapes_api_contract() -> None:
    parsed = parse_intake_submission(_submission_payload())
    body = build_intake_body(parsed, slack_thread_ts="1700000000.000100")
    assert body == {
        "team_id": "T01",
        "requester_slack_user_id": "U0REQ",
        "requester_slack_display_name": "dana",
        "slack_channel_id": "C99",
        "slack_thread_ts": "1700000000.000100",
        "question": "Can we use this clause?",
        "links": ["https://a.com"],
    }


def test_build_confirmation_text_includes_id() -> None:
    text = build_confirmation_text("abc-123")
    assert "abc-123" in text
