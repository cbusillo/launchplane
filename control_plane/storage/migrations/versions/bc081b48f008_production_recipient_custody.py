"""Keep fresh production recipient custody independent of the serving binding."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "bc081b48f008"
down_revision: str = "ab970a37f007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if "launchplane_production_recipients" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_production_recipients",
            sa.Column("product", sa.String(), primary_key=True),
            sa.Column("context", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # A rollback of service code must never discard production recovery custody.
    pass
