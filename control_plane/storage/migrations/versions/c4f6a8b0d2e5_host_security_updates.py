from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from control_plane.storage.migrations.versions import (
    f3c7d9e1b5a8_join_monitoring_history as predecessor,
)

revision = "c4f6a8b0d2e5"
down_revision = predecessor.revision
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "launchplane_host_security_updates",
        sa.Column("context", sa.String(), primary_key=True),
        sa.Column("instance", sa.String(), primary_key=True),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("launchplane_host_security_updates")
