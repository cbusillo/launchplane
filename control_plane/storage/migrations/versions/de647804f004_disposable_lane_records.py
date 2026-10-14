"""Keep disposable observations and captured identities outside older record payloads."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "de647804f004"
down_revision: str = "cd5367f3e003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    payload_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    if "launchplane_disposable_lane_safety" not in existing:
        op.create_table(
            "launchplane_disposable_lane_safety",
            sa.Column("product", sa.String(), primary_key=True),
            sa.Column("context", sa.String(), primary_key=True),
            sa.Column("instance", sa.String(), primary_key=True),
            sa.Column("payload", payload_type, nullable=False),
        )
    if "launchplane_disposable_replacement_preparations" not in existing:
        op.create_table(
            "launchplane_disposable_replacement_preparations",
            sa.Column("operation_id", sa.String(), primary_key=True),
            sa.Column("payload", payload_type, nullable=False),
        )


def downgrade() -> None:
    # Older readers ignore these additive tables. Keep captured retained IDs
    # and evidence across rollback; dropping them can lose the only pointer to
    # a retained target after a rebind. Upgrade deliberately adopts them again.
    pass
