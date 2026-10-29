"""Join topics with the already-applied delivery branch; never the release default."""

from control_plane.storage.migrations.versions import (
    e9e3a7b1c5d8_join_retirement_paths as retirement,
    b81ca763d924_add_discussion_topics as topics,
)

revision: str = "da72490fce13"
down_revision: tuple[str, str] = (retirement.revision, topics.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
