"""Deployment-wide on/off switch for Legal Escalation Capture (Phase 5).

A single ``escalation_config`` row (``id = 'singleton'``) records whether the
deployment captures new escalations. There is one legal team per deployment, so
one switch. Both helpers fail safe: a missing row reads as **disabled**, so a
fresh deployment never captures until an operator explicitly turns it on
(invariants P4 fail-restrictive / P8 operator control).

Mutation flushes inside the caller's transaction — the endpoint owns the commit
so the state change and its audit row land atomically (invariant P5), mirroring
the MCP-toggle service pattern.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.escalation import EscalationConfig

_SINGLETON_ID = "singleton"


async def get_escalation_enabled(db: AsyncSession) -> bool:
    """Whether escalation capture is enabled. Fail-safe: a missing row → False."""

    row = await db.get(EscalationConfig, _SINGLETON_ID)
    return bool(row and row.enabled)


async def set_escalation_enabled(db: AsyncSession, *, enabled: bool) -> bool:
    """Upsert the singleton on/off flag and return the stored value.

    Flushes but does NOT commit — the calling endpoint commits so the change
    and its audit row ride a single transaction (invariant P5).
    """

    row = await db.get(EscalationConfig, _SINGLETON_ID)
    if row is None:
        row = EscalationConfig(id=_SINGLETON_ID, enabled=enabled)
        db.add(row)
    else:
        row.enabled = enabled
    await db.flush()
    return row.enabled
