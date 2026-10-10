"""Persist the self-deploy release admission fence.

Revision ID: ab3145d1c001
Revises: f3021a0b1c2d
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "ab3145d1c001"
down_revision: str = "f3021a0b1c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "launchplane_service_deploy_drains" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "launchplane_service_deploy_drains",
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("launchplane_service_deploy_drains")
