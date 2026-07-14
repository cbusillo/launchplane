"""add merge train controller states

Revision ID: e1f3a5c7d9b1
Revises: b4d6f8a0c2e4
Create Date: 2026-07-13 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e1f3a5c7d9b1"
down_revision = "b4d6f8a0c2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "launchplane_merge_train_controller_states" in existing_tables:
        return
    op.create_table(
        "launchplane_merge_train_controller_states",
        sa.Column("controller_key", sa.String(), primary_key=True),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("policy_key", sa.String(), nullable=False),
        sa.Column("policy_sha256", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=False),
        sa.Column("lease_expires_at", sa.String(), nullable=False),
        sa.Column("active_action", sa.String(), nullable=False),
        sa.Column("active_phase", sa.String(), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )
    op.create_index(
        "launchplane_merge_train_controller_states_repository_base_idx",
        "launchplane_merge_train_controller_states",
        ["repository", "base_branch", sa.text("updated_at desc")],
    )
    op.create_index(
        "launchplane_merge_train_controller_states_status_idx",
        "launchplane_merge_train_controller_states",
        ["status", sa.text("updated_at desc")],
    )
    op.create_index(
        "launchplane_merge_train_controller_states_lease_idx",
        "launchplane_merge_train_controller_states",
        ["status", "lease_expires_at", sa.text("updated_at desc")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "launchplane_merge_train_controller_states" not in existing_tables:
        return
    op.drop_index(
        "launchplane_merge_train_controller_states_lease_idx",
        table_name="launchplane_merge_train_controller_states",
    )
    op.drop_index(
        "launchplane_merge_train_controller_states_status_idx",
        table_name="launchplane_merge_train_controller_states",
    )
    op.drop_index(
        "launchplane_merge_train_controller_states_repository_base_idx",
        table_name="launchplane_merge_train_controller_states",
    )
    op.drop_table("launchplane_merge_train_controller_states")
