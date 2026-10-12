"""Persist merge train stall observations independently of controller leases."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "bc4256e2d002"
down_revision: str = "ab3145d1c001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if "launchplane_merge_train_stalls" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_merge_train_stalls",
            sa.Column("repository", sa.String(), primary_key=True),
            sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("launchplane_merge_train_stalls")
