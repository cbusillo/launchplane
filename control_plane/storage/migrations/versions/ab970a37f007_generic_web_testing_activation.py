"""Retain testing activation custody independently of historical provider effects."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "ab970a37f007"
down_revision: str = "fa869a26f006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if "launchplane_testing_activations" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_testing_activations",
            sa.Column("product", sa.String(), primary_key=True),
            sa.Column("context", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # Old images cannot consume this generation; retain every custody receipt.
    pass
