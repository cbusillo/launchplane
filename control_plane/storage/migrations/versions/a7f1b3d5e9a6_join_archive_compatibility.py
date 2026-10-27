from control_plane.storage.migrations.versions import (
    a5d9f1b3c7e4_join_archive_topics_retirement as archive_retirement,
    a6e0a2c4d8f5_join_archive_topics_hostnames as archive_hostnames,
    f3d7a1b5c9e2_join_topics_hostname_retirement as compatibility,
)

revision: str = "a7f1b3d5e9a6"
down_revision: tuple[str, str, str] = (
    archive_retirement.revision,
    archive_hostnames.revision,
    compatibility.revision,
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
