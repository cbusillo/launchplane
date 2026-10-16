"""Preserve retired fresh testing generation custody."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "cd192c59f009"
down_revision: str = "bc081b48f008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if (
        "launchplane_retained_testing_generations"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_retained_testing_generations",
            sa.Column("operation_id", sa.String(), primary_key=True),
            sa.Column("generation_binding_sha256", sa.String(), nullable=False, unique=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # Older images cannot continue retired generations. Preserve their custody.
    pass
