"""add product monitoring intent

Revision ID: c6f8a0b2d4e6
Revises: b3d5f7a9c1e4
Create Date: 2026-07-27 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from alembic import op
import sqlalchemy as sa

from control_plane.contracts.product_monitoring_intent_migration import (
    downgrade_product_profile_monitoring_intent_payload,
    migrate_product_profile_monitoring_intent_payload,
)

revision: str = "c6f8a0b2d4e6"
down_revision: str | None = "b3d5f7a9c1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRODUCT_PROFILES_TABLE = "launchplane_product_profiles"


def upgrade() -> None:
    _rewrite_profiles(migrate_product_profile_monitoring_intent_payload)


def downgrade() -> None:
    _rewrite_profiles(downgrade_product_profile_monitoring_intent_payload)


def _rewrite_profiles(transform: Callable[[object], object]) -> None:
    connection = op.get_bind()
    product_profiles = _product_profiles_table()
    for row in connection.execute(
        sa.select(product_profiles.c.product, product_profiles.c.payload)
    ):
        transformed_payload = transform(row.payload)
        if transformed_payload != row.payload:
            connection.execute(
                product_profiles.update()
                .where(product_profiles.c.product == row.product)
                .values(payload=transformed_payload)
            )


def _product_profiles_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        _PRODUCT_PROFILES_TABLE,
        metadata,
        sa.Column("product", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
