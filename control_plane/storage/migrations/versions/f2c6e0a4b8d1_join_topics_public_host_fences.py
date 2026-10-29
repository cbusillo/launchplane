from control_plane.storage.migrations.versions import (
    b81ca763d924_add_discussion_topics as topics,
    f0a4c8e2b6d9_canonical_public_host_fences as hostnames,
)

revision: str = "f2c6e0a4b8d1"
down_revision: tuple[str, str] = (topics.revision, hostnames.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
