"""escalation_config — deployment-wide on/off for Legal Escalation Capture (Phase 5)

A single-row (``id = 'singleton'``) operator switch for whether the deployment
captures new escalations. Fail-safe by design: the ``enabled`` column defaults
to ``false`` and the application treats a missing row as disabled, so a fresh
deployment never captures until an operator turns it on (invariant P4 /
operator control P8). The on/off action itself is recorded in ``audit_log``;
this table only holds the current state.

Revision ID: 0066
Revises: 0065
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "escalation_config",
        sa.Column(
            "id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("'singleton'"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.CheckConstraint("id = 'singleton'", name="chk_escalation_config_singleton"),
    )


def downgrade() -> None:
    op.drop_table("escalation_config")
