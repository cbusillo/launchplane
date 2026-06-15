"""migrate lane health monitoring

Revision ID: fa2c4e6f8a0b
Revises: f9b1c3d5e7a9
Create Date: 2026-06-15 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from control_plane.contracts.product_health_monitoring_migration import (
    downgrade_product_profile_health_monitoring_payload,
)
from control_plane.contracts.product_health_monitoring_migration import (
    migrate_product_profile_health_monitoring_payload,
)

revision: str = "fa2c4e6f8a0b"
down_revision: str | None = "f9b1c3d5e7a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRODUCT_PROFILES_TABLE = "launchplane_product_profiles"


def upgrade() -> None:
    connection = op.get_bind()
    product_profiles = _product_profiles_table()
    for row in connection.execute(
        sa.select(product_profiles.c.product, product_profiles.c.payload)
    ):
        migrated_payload = migrate_product_profile_health_monitoring_payload(row.payload)
        if migrated_payload != row.payload:
            connection.execute(
                product_profiles.update()
                .where(product_profiles.c.product == row.product)
                .values(payload=migrated_payload)
            )


def downgrade() -> None:
    connection = op.get_bind()
    product_profiles = _product_profiles_table()
    for row in connection.execute(
        sa.select(product_profiles.c.product, product_profiles.c.payload)
    ):
        downgraded_payload = downgrade_product_profile_health_monitoring_payload(row.payload)
        if downgraded_payload != row.payload:
            connection.execute(
                product_profiles.update()
                .where(product_profiles.c.product == row.product)
                .values(payload=downgraded_payload)
            )


def _product_profiles_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        _PRODUCT_PROFILES_TABLE,
        metadata,
        sa.Column("product", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
