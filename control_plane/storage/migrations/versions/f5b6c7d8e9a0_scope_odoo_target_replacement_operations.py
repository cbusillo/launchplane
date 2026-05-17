"""scope Odoo target replacement operation reservations

Revision ID: f5b6c7d8e9a0
Revises: f4a5b6c7d8e9
Create Date: 2026-05-17 19:35:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f5b6c7d8e9a0"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_odoo_stable_target_replacement_operations"
_ACTIVE_LANE_INDEX = "launchplane_odoo_replacement_active_lane_uidx"
_IDEMPOTENCY_INDEX = "launchplane_odoo_replacement_operation_idempotency_idx"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "idempotency_scope",
            sa.String(),
            nullable=False,
            server_default="",
        ),
    )
    op.drop_index(_IDEMPOTENCY_INDEX, table_name=_TABLE)
    op.create_index(
        _IDEMPOTENCY_INDEX,
        _TABLE,
        ["idempotency_scope", "idempotency_key", sa.text("updated_at DESC")],
    )
    op.create_index(
        _ACTIVE_LANE_INDEX,
        _TABLE,
        ["product", "context", "instance"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(_ACTIVE_LANE_INDEX, table_name=_TABLE)
    op.drop_index(_IDEMPOTENCY_INDEX, table_name=_TABLE)
    op.create_index(
        _IDEMPOTENCY_INDEX,
        _TABLE,
        ["idempotency_key", sa.text("updated_at DESC")],
    )
    op.drop_column(_TABLE, "idempotency_scope")
