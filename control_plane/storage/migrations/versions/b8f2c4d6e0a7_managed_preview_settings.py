from alembic import op

from control_plane.storage.preview_setting_migration import migrate_preview_settings

revision: str = "b8f2c4d6e0a7"
down_revision: str = "a4c8e0f2b6d3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    migrate_preview_settings(op.get_bind(), recorded_at="2026-10-26T00:00:00Z")


def downgrade() -> None:
    pass
