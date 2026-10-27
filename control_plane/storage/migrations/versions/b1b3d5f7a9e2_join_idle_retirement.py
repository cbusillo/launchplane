from control_plane.storage.migrations.versions import (
    b9a3d5e7f1b8_join_preview_retirement as retirement,
    b0a2c4e6f8d1_retire_preview_idle_reservations as idle,
)

revision: str = "b1b3d5f7a9e2"
down_revision: tuple[str, ...] = (retirement.revision, idle.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
