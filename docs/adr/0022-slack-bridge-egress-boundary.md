# ADR 0022 — The slack-bridge is the Slack egress boundary; the backend reaches it over one audited internal door

**Status:** Proposed
**Date:** 2026-06-30
**Owner:** Legal Escalation Capture (Slice A) — feature branch `feat/escalation-capture`

## Context

The Legal Escalation Capture feature (Slice A) lets a non-legal team member file
a legal question from Slack and lets the legal team move it through a fixed
lifecycle inside lq-ai. Two of its phases need lq-ai to **send messages to
Slack**: Phase 3 posts a capture confirmation into the originating thread, and
Phase 4 posts a status-update reply when a legal user changes an escalation's
status.

[ADR 0014](0014-gateway-egress-boundary-for-tool-providers.md) pinned that all
*third-party tool/data egress* flows through the Inference Gateway, and
[ADR 0016 §P1](0016-transparency-and-governance-invariants.md) made that
enforceable: a CI test (`test_backend_makes_no_direct_third_party_egress`)
fails the build if any module under `api/app/` imports a general-purpose HTTP
client, with a single allowlisted file (`clients/gateway.py`). ADR 0016's P1 is
worded precisely: *"All outbound calls to anything **outside the operator's own
infrastructure** go through the Inference Gateway. The backend (`api/`) holds no
**third-party** credentials and makes no **third-party** calls."*

The slack-bridge (introduced in M3-D1) is a separate, operator-run service in
the same deployment that already owns the Slack OAuth dance, signature
verification, and all Slack Web API calls. It is **first-party infrastructure**,
not a third party. Until now the api↔bridge channel ran one direction only —
the bridge calls the api (workspace persist, bot-token hand-off, escalation
intake), authenticating with the shared `LQ_AI_BRIDGE_TOKEN` bearer; the api
never called the bridge. Phase 4 needs the *reverse* leg: a status change
originates inside the api and must cause a Slack post.

This ADR records (a) that the slack-bridge — not the gateway, not the api — is
the Slack egress boundary, and (b) how the backend triggers it without
weakening P1.

## Decision drivers

1. **P1 forbids third-party egress, not first-party internal calls.** Routing a
   call to the operator's own bridge through the *inference* gateway would be a
   category error (the gateway models AI-provider and tool-provider egress, not
   a Slack webhook/OAuth surface). The bridge is the right home for Slack I/O.
2. **The bridge is the only component that should ever hold a Slack token.**
   Bot tokens live encrypted at rest in the api and are handed to the bridge
   only transiently, at call time, through the existing bridge-auth `/bot-token`
   endpoint. The backend should not start carrying plaintext Slack tokens in new
   code paths.
3. **The enforcement test is a deliberately blunt proxy with a documented
   escape hatch.** ADR 0016 driver #3 designed each gate with *"an explicit,
   documented escape hatch (an allowlist a contributor must consciously
   extend)"* precisely so a legitimate call the import-scan cannot distinguish
   from third-party egress is not blocked. A first-party api→bridge call is
   exactly that case.
4. **One door per egress target, each named.** Keeping every outbound HTTP
   client in a small, enumerated set of dedicated modules (gateway for
   third-party; slack-bridge for the internal Slack relay) preserves the
   review-surface discipline P1 exists to protect.

## Considered alternatives

### A. The backend pushes to a dedicated, allowlisted bridge client — **chosen**

The api gains one dedicated module, `app/clients/slack_bridge.py`, whose only
job is to POST to the slack-bridge. It is added to the egress-test allowlist
beside `clients/gateway.py`. On a status change the backend signals the bridge;
the bridge fetches the bot token and posts.

- **Cost:** a one-line allowlist extension (`clients/slack_bridge.py`), a new
  config setting (`LQ_AI_BRIDGE_URL`), and a new bridge-auth internal endpoint.
- **Why it wins:** smallest change; honors the Phase-4 design (status posts back
  "at post time"); the allowlist extension is the mechanism ADR 0016 built for
  exactly this, and it does not widen P1 — the call is first-party.

### B. Outbox + bridge polling

The api records "this status changed; needs a Slack note" (no outbound call at
all); the bridge polls the api for pending notes and posts them.

- **Rejected for v1:** touches no invariant machinery, but costs materially more
  (an outbox table + migration, two endpoints, a recurring poller in the bridge)
  and adds up-to-poll-interval latency, to avoid an allowlist line that the
  enforcement design explicitly sanctions. Kept on record as the fallback if a
  maintainer prefers zero allowlist changes.

### C. Route the api→bridge call through the gateway

- **Rejected:** the gateway is the *third-party* egress boundary (ADR 0014); the
  bridge is first-party. Forcing an internal Slack-relay trigger through the
  inference gateway conflates two distinct boundaries and burdens the gateway
  with Slack concerns it has no role in.

## Decision

### D1. The slack-bridge is the Slack egress boundary

All Slack I/O — opening modals, posting confirmations, posting status updates —
happens in the slack-bridge service. The api never calls Slack directly and
holds no Slack credential in plaintext beyond the transient hand-off. This is
the Slack-specific counterpart of ADR 0014's gateway boundary, justified
separately because Slack is an inbound-webhook + OAuth + interactive surface
that the gateway's request/response adapter model does not fit.

### D2. The backend reaches the bridge over one dedicated, allowlisted client

`app/clients/slack_bridge.py` is the only api module besides `clients/gateway.py`
permitted to construct an outbound HTTP client. It is added to
`_EGRESS_ALLOWLIST` in `api/tests/test_transparency_invariants.py`. This does
**not** weaken P1: P1 forbids calls *outside the operator's own
infrastructure*, and the bridge is first-party. The allowlist entry teaches the
blunt import-scan that this one first-party client is legitimate, per ADR 0016
driver #3.

### D3. Symmetric shared-secret auth, fail-closed

The api authenticates to the bridge with the same `LQ_AI_BRIDGE_TOKEN` already
used in the bridge→api direction (M3-D1 / M3-D3 decision: one shared bearer for
the bridge channel). The bridge's internal endpoint constant-time-matches the
inbound bearer (`hmac.compare_digest`) and fails closed — a missing/empty
configured token rejects all callers, mirroring the api's `require_bridge_auth`.

### D4. The bridge resolves the bot token; the backend never forwards it

The api→bridge trigger carries only non-secret identifiers (`team_id`,
`channel_id`, `thread_ts`, new `status`). The bridge fetches the decrypted bot
token via its existing `/bot-token` hand-off and posts the threaded reply. This
refines the earlier Slice-A decision ("the backend hands the decrypted token to
the bridge at post time"): the token is still encrypted at rest and reaches the
bridge only transiently, but it is never placed in the trigger body and no new
token-handling path is added to the backend. The status-update copy names only
the new status — never the question content (invariant P3).

### D5. New backend config

`LQ_AI_BRIDGE_URL` (default `http://slack-bridge:8002`, the compose service
address) tells the backend where the bridge lives. It is only exercised when an
escalation's status changes, which only happens when escalations exist, which
only happens when the `slack` profile is running.

## Cross-references

- [ADR 0014](0014-gateway-egress-boundary-for-tool-providers.md) (third-party
  egress boundary — the gateway), [ADR 0016](0016-transparency-and-governance-invariants.md)
  (the P1 invariant and its enforcement + escape-hatch design).
- Enforcement: `api/tests/test_transparency_invariants.py` (`_EGRESS_ALLOWLIST`).
- Code: `api/app/clients/slack_bridge.py`, `api/app/api/escalations.py`
  (status-change hook), `slack-bridge/app/main.py`
  (`POST /internal/escalations/status-post`).
- Plan / PRD: `2026-06-25-legal-escalation-capture-{plan,prd}.md` (Slice A,
  Phase 4).
