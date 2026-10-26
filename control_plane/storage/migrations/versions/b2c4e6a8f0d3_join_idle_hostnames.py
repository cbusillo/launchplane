from control_plane.storage.migrations.versions import (
    a6e0a2c4d8f5_join_archive_topics_hostnames as hostnames,
    b0a2c4e6f8d1_retire_preview_idle_reservations as idle,
)

revision: str = "b2c4e6a8f0d3"
down_revision: tuple[str, ...] = (hostnames.revision, idle.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
