"""Keep upgrade rehearsal custody separate from canonical product lanes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e5a9b3c7d1f4"
down_revision: str = "e4f8a2b6c0d3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if "launchplane_odoo_upgrade_rehearsals" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_odoo_upgrade_rehearsals",
            sa.Column("rehearsal_id", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # Custody is needed after rollback, including uncertain provider effects.
    pass
