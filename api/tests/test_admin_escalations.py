"""Integration tests for the operator escalation-capture on/off surface
(Slice A, Phase 5).

Covers ``GET``/``PATCH`` ``/api/v1/admin/escalations``:

* GET reports the deployment-wide switch; the fail-safe default is disabled.
* PATCH toggles it, writing an ``escalation.enabled`` / ``escalation.disabled``
  audit row (the new value only — never escalation content, P3) atomically.
* Unknown body fields are rejected (``extra="forbid"``).
* Non-admin authenticated users are forbidden (403); unauthenticated → 401.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.main import app
from app.models import AuditLog, User
from app.security import create_access_token, hash_password


def _override_get_db(db_session: AsyncSession):
    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    return _override


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = _override_get_db(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


async def _make_user(db_session: AsyncSession, *, is_admin: bool, suffix: str) -> User:
    user = User(
        email=f"escadmin-{suffix}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=f"Esc Admin {suffix}",
        hashed_password=hash_password("correct-horse-battery-staple"),
        is_admin=is_admin,
        mfa_enabled=False,
        must_change_password=False,
        role="admin" if is_admin else "member",
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    return await _make_user(db_session, is_admin=True, suffix="a")


@pytest_asyncio.fixture
async def member(db_session: AsyncSession) -> User:
    return await _make_user(db_session, is_admin=False, suffix="m")


def _bearer(user: User) -> dict[str, str]:
    token = create_access_token(user.id, user.email, is_admin=user.is_admin)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
async def test_status_defaults_disabled(client: AsyncClient, admin: User) -> None:
    resp = await client.get("/api/v1/admin/escalations", headers=_bearer(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": False}


@pytest.mark.integration
async def test_enable_then_disable_with_audit(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    resp = await client.patch(
        "/api/v1/admin/escalations", headers=_bearer(admin), json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": True}

    resp = await client.get("/api/v1/admin/escalations", headers=_bearer(admin))
    assert resp.json() == {"enabled": True}

    resp = await client.patch(
        "/api/v1/admin/escalations", headers=_bearer(admin), json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": False}

    actions = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "escalation_config",
                    AuditLog.resource_id == "singleton",
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(a.action for a in actions) == [
        "escalation.disabled",
        "escalation.enabled",
    ]
    # P3: the audit details carry only the new value, never escalation content.
    for a in actions:
        assert set((a.details or {}).keys()) <= {"enabled"}


@pytest.mark.integration
async def test_toggle_is_idempotent_no_redundant_audit(
    client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    """Enabling when already enabled is a no-op: one audit row, not two."""
    first = await client.patch(
        "/api/v1/admin/escalations", headers=_bearer(admin), json={"enabled": True}
    )
    assert first.status_code == 200, first.text
    second = await client.patch(
        "/api/v1/admin/escalations", headers=_bearer(admin), json={"enabled": True}
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"enabled": True}

    actions = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == "escalation.enabled")))
        .scalars()
        .all()
    )
    assert len(actions) == 1  # the redundant second toggle wrote no audit row


@pytest.mark.integration
async def test_patch_rejects_unknown_field(client: AsyncClient, admin: User) -> None:
    resp = await client.patch(
        "/api/v1/admin/escalations",
        headers=_bearer(admin),
        json={"enabled": True, "scope": "everything"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_non_admin_forbidden(client: AsyncClient, member: User) -> None:
    get_resp = await client.get("/api/v1/admin/escalations", headers=_bearer(member))
    assert get_resp.status_code == 403, get_resp.text
    patch_resp = await client.patch(
        "/api/v1/admin/escalations", headers=_bearer(member), json={"enabled": True}
    )
    assert patch_resp.status_code == 403, patch_resp.text


@pytest.mark.integration
async def test_unauthenticated_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/admin/escalations")
    assert resp.status_code == 401
