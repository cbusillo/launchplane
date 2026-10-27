from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c8d2e4f6a9b1"
down_revision = "a4c8e0f2b6d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "launchplane_integration_attestations"
    if table in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        table,
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )
    op.create_index(
        "launchplane_integration_attestations_change_idx",
        table,
        ["repository", "pull_request_number"],
    )


def downgrade() -> None:
    op.drop_table("launchplane_integration_attestations")
