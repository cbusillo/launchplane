"""Rebuild VeriReel's production environment record from its last promotion.

Revision ID: a86c53aea47d
Revises: e2b4d6f8a1c3
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op

from control_plane.storage.verireel_prod_inventory_backfill import (
    backfill_verireel_prod_inventory,
)

revision: str = "a86c53aea47d"
down_revision: str | None = "e2b4d6f8a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    backfill_verireel_prod_inventory(
        op.get_bind(), updated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def downgrade() -> None:
    # The repaired record is correct evidence; restoring the stale one would re-break review.
    pass
