from control_plane.storage.migrations.versions import (
    da72490fce13_join_topics_retirement as topics_retirement,
    f1b5d9a3c7e0_join_public_host_fences as hostname_retirement,
    f2c6e0a4b8d1_join_topics_public_host_fences as topics_hostnames,
)

revision: str = "f3d7a1b5c9e2"
down_revision: tuple[str, str, str] = (
    topics_hostnames.revision,
    hostname_retirement.revision,
    topics_retirement.revision,
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
