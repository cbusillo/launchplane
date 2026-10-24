from control_plane.storage.migrations.versions import (
    a4c8e0f2b6d3_archive_nonruntime_product_profiles as archive,
    da72490fce13_join_topics_retirement as topics_retirement,
)

revision: str = "a5d9f1b3c7e4"
down_revision: tuple[str, str] = (archive.revision, topics_retirement.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
