from control_plane.storage.migrations.versions import (
    a4c8e0f2b6d3_archive_nonruntime_product_profiles as archive,
    f2c6e0a4b8d1_join_topics_public_host_fences as topics_hostnames,
)

revision: str = "a6e0a2c4d8f5"
down_revision: tuple[str, str] = (archive.revision, topics_hostnames.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
