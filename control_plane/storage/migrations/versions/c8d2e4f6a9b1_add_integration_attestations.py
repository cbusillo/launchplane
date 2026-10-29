from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c8d2e4f6a9b1"
down_revision = "b2c4e6a8f0d3"
branch_labels = None
depends_on = None
TABLE_NAME = "launchplane_integration_attestations"


def upgrade() -> None:
    if TABLE_NAME in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )
    op.create_index(
        "launchplane_integration_attestations_change_idx",
        TABLE_NAME,
        ["repository", "pull_request_number"],
    )


def downgrade() -> None:
    op.drop_table(TABLE_NAME)
