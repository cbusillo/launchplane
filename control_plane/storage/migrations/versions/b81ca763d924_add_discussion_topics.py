"""Add discussion records without traversing the held delivery-table drop."""

from alembic import op
import sqlalchemy as sa

from control_plane.storage.migrations.versions import (
    e8d2f6a0b4c7_drop_preview_enablement as preview,
)

revision: str = "b81ca763d924"
down_revision: str = preview.revision
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Supported schema repair may replay after stamping a newer physical schema
    # to an older revision. Preserve its existing discussion history.
    if "launchplane_discussion_topics" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "launchplane_discussion_topics",
        sa.Column("product", sa.String(), primary_key=True),
        sa.Column("topic_id", sa.String(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("issue_url", sa.Text(), nullable=False),
        sa.Column("added_at", sa.String(), nullable=False),
        sa.Column("closed_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError("Discussion history requires a reviewed backup/restore; downgrade refuses.")
