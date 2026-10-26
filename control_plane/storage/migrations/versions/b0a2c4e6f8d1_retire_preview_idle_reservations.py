from alembic import op
import sqlalchemy as sa

from control_plane.storage.migrations.versions import (
    a4c8e0f2b6d3_archive_nonruntime_product_profiles as archive,
)

revision: str = "b0a2c4e6f8d1"
down_revision: str = archive.revision
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    records = sa.table(
        "launchplane_idempotency_records",
        sa.column("scope", sa.String()),
        sa.column("route_path", sa.String()),
    )
    op.execute(
        records.delete().where(
            records.c.scope == "launchplane:preview-idle:v1",
            records.c.route_path.in_(
                ("service:preview-idle", "/internal/provider-original-intent")
            ),
        )
    )


def downgrade() -> None:
    raise RuntimeError("Retired preview idle reservations cannot be reconstructed.")
