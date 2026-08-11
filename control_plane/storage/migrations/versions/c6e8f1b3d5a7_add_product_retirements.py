"""add product retirement audit records

Revision ID: c6e8f1b3d5a7
Revises: b5d7f9a1c3e6
Create Date: 2026-08-11 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c6e8f1b3d5a7"
down_revision: str | None = "b5d7f9a1c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_product_retirements"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    if not _table_exists():
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("plan_record_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("outcome", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    for index_name, first_column in (
        ("launchplane_product_retirements_product_idx", "product"),
        ("launchplane_product_retirements_plan_idx", "plan_record_id"),
        ("launchplane_product_retirements_idempotency_idx", "idempotency_key"),
    ):
        if not _index_exists(index_name):
            op.create_index(
                index_name,
                _TABLE,
                [first_column, sa.text("recorded_at DESC")],
            )
    if not _index_exists("launchplane_product_retirements_plan_idempotency_unique"):
        op.create_index(
            "launchplane_product_retirements_plan_idempotency_unique",
            _TABLE,
            ["product", "actor", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("mode = 'plan'"),
            sqlite_where=sa.text("mode = 'plan'"),
        )


def downgrade() -> None:
    if not _table_exists():
        return
    for index_name in (
        "launchplane_product_retirements_plan_idempotency_unique",
        "launchplane_product_retirements_idempotency_idx",
        "launchplane_product_retirements_plan_idx",
        "launchplane_product_retirements_product_idx",
    ):
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
