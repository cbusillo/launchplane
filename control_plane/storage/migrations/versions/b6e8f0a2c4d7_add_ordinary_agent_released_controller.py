"""Add durable ordinary-agent controller release checkpoints.

Revision ID: b6e8f0a2c4d7
Revises: a4d9e2f6b8c1
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b6e8f0a2c4d7"
down_revision: str | None = "a4d9e2f6b8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    released_controller = sa.JSON().with_variant(JSONB(), "postgresql")
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("launchplane_ordinary_agent_job_claims")
    }
    if "released_controller" not in columns:
        op.add_column(
            "launchplane_ordinary_agent_job_claims",
            sa.Column("released_controller", released_controller, nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("launchplane_ordinary_agent_job_claims")
    }
    if "released_controller" in columns:
        op.drop_column("launchplane_ordinary_agent_job_claims", "released_controller")
