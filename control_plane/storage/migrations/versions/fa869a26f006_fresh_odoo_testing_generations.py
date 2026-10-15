"""Retain the exact fresh testing bootstrap generation across restarts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "fa869a26f006"
down_revision: str = "ef758915f005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if (
        "launchplane_fresh_odoo_testing_generations"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_fresh_odoo_testing_generations",
            sa.Column("product", sa.String(), primary_key=True),
            sa.Column("context", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # Older images cannot continue this generation; never erase its custody.
    pass
