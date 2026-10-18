"""Keep candidate custody separate from the currently serving testing lane."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e6b0c4d8f2a5"
down_revision: str = "e5a9b3c7d1f4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if "launchplane_testing_candidates" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_testing_candidates",
            sa.Column("namespace", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # Keep original-key custody after rollback, including uncertain creates.
    pass
