"""Join canonical hostname fences only where the held drop is already approved."""

from control_plane.storage.migrations.versions import (
    e9e3a7b1c5d8_join_retirement_paths as retirement,
    f0a4c8e2b6d9_canonical_public_host_fences as hostnames,
)

revision: str = "f1b5d9a3c7e0"
down_revision: tuple[str, str] = (retirement.revision, hostnames.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
