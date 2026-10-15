"""retain native testing copy artifact receipts

Revision ID: 46acf53ad998
Revises: ab970a37f007
Create Date: 2026-10-15 13:37:47.891414+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "46acf53ad998"
down_revision: str | None = "ab970a37f007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if (
        "launchplane_native_testing_copy_captures"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_native_testing_copy_captures",
            sa.Column("generation_binding_sha256", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # A rollback must not erase a retained source-capture witness.
    pass
