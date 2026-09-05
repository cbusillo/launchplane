"""Preserve original-writer attribution beside change-impact policy payloads.

Revision ID: d1f3a5b7c9e2
Revises: c0e2f4a6b8d1
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d1f3a5b7c9e2"
down_revision: str | None = "c0e2f4a6b8d1"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "launchplane_change_impact_policies"


def upgrade() -> None:
    if "audit_payload" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)
    }:
        op.add_column(
            _TABLE,
            sa.Column(
                "audit_payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column(_TABLE, "audit_payload")
