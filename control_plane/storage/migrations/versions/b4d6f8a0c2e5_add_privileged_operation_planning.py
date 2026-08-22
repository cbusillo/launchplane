"""add privileged operation planning records

Revision ID: b4d6f8a0c2e5
Revises: a2c4e6f8b0d3
Create Date: 2026-08-22 20:20:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b4d6f8a0c2e5"
down_revision: str | None = "a2c4e6f8b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    payload_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )
    if "launchplane_privileged_operations" not in table_names:
        op.create_table(
            "launchplane_privileged_operations",
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("descriptor_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("requester_github_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("payload", payload_type, nullable=False),
            sa.PrimaryKeyConstraint("operation_id"),
        )
        op.create_index(
            "launchplane_privileged_operations_status_idx",
            "launchplane_privileged_operations",
            ["status", sa.text("created_at DESC")],
        )
        op.create_index(
            "launchplane_privileged_operations_descriptor_idx",
            "launchplane_privileged_operations",
            ["descriptor_id", sa.text("created_at DESC")],
        )
    if "launchplane_privileged_operation_events" not in table_names:
        op.create_table(
            "launchplane_privileged_operation_events",
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("sequence", sa.BigInteger(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("payload", payload_type, nullable=False),
            sa.PrimaryKeyConstraint("event_id"),
        )
        op.create_index(
            "launchplane_privileged_operation_events_operation_sequence_uidx",
            "launchplane_privileged_operation_events",
            ["operation_id", "sequence"],
            unique=True,
        )
        op.create_index(
            "launchplane_privileged_operation_events_occurred_idx",
            "launchplane_privileged_operation_events",
            [sa.text("occurred_at DESC")],
        )


def downgrade() -> None:
    table_names = set(sa.inspect(op.get_bind()).get_table_names())
    if "launchplane_privileged_operation_events" in table_names:
        op.drop_index(
            "launchplane_privileged_operation_events_occurred_idx",
            table_name="launchplane_privileged_operation_events",
        )
        op.drop_index(
            "launchplane_privileged_operation_events_operation_sequence_uidx",
            table_name="launchplane_privileged_operation_events",
        )
        op.drop_table("launchplane_privileged_operation_events")
    if "launchplane_privileged_operations" in table_names:
        op.drop_index(
            "launchplane_privileged_operations_descriptor_idx",
            table_name="launchplane_privileged_operations",
        )
        op.drop_index(
            "launchplane_privileged_operations_status_idx",
            table_name="launchplane_privileged_operations",
        )
        op.drop_table("launchplane_privileged_operations")
