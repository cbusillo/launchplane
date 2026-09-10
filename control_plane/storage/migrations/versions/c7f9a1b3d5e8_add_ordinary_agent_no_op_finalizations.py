"""Add immutable ordinary-agent no-op landing finalizations.

Revision ID: c7f9a1b3d5e8
Revises: b6e8f0a2c4d7
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "c7f9a1b3d5e8"
down_revision: str | None = "b6e8f0a2c4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if (
        "launchplane_ordinary_agent_no_op_landing_finalizations"
        in sa.inspect(op.get_bind()).get_table_names()
    ):
        return
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "launchplane_ordinary_agent_no_op_landing_finalizations",
        sa.Column("preparation_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("scope_sha256", sa.String(), nullable=False),
        sa.Column("binding_revision", sa.BigInteger(), nullable=False),
        sa.Column("pull_request_number", sa.BigInteger(), nullable=False),
        sa.Column("admission_id", sa.String(), nullable=False),
        sa.Column("outcome_id", sa.String(), nullable=False),
        sa.Column("successor_record_id", sa.String(), nullable=False),
        sa.Column("payload", payload, nullable=False),
        sa.PrimaryKeyConstraint("preparation_id"),
        sa.UniqueConstraint("admission_id", name="ordinary_no_op_landing_admission_uq"),
        sa.UniqueConstraint("outcome_id", name="ordinary_no_op_landing_outcome_uq"),
        sa.UniqueConstraint("successor_record_id", name="ordinary_no_op_landing_successor_uq"),
        sa.UniqueConstraint(
            "request_id",
            "binding_revision",
            "pull_request_number",
            name="ordinary_no_op_landing_entry_uq",
        ),
    )


def downgrade() -> None:
    if (
        "launchplane_ordinary_agent_no_op_landing_finalizations"
        in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.drop_table("launchplane_ordinary_agent_no_op_landing_finalizations")
