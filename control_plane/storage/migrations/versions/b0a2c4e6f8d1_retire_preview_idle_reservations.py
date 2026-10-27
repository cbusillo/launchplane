from alembic import op
import sqlalchemy as sa

from control_plane.storage.migrations.versions import (
    b8f2c4d6e0a7_managed_preview_settings as preview_settings,
)

revision: str = "b0a2c4e6f8d1"
down_revision: str = preview_settings.revision
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
    with op.batch_alter_table("launchplane_idempotency_records") as batch:
        batch.create_check_constraint(
            "launchplane_idempotency_no_preview_idle_ck",
            "NOT (scope = 'launchplane:preview-idle:v1' AND route_path IN "
            "('service:preview-idle', '/internal/provider-original-intent'))",
        )


def downgrade() -> None:
    raise RuntimeError("Retired preview idle reservations cannot be reconstructed.")
