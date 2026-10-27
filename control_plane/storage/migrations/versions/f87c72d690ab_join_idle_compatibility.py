from control_plane.storage.migrations.versions import (
    c1c5f7a9b3d0_join_preview_compatibility as compatibility,
    b1b3d5f7a9e2_join_idle_retirement as retirement,
    b2c4e6a8f0d3_join_idle_hostnames as hostnames,
)

revision: str = "f87c72d690ab"
down_revision: tuple[str, ...] = (compatibility.revision, retirement.revision, hostnames.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
