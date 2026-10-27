from control_plane.storage.migrations.versions import (
    c9e3f5a7b0c2_join_attestations_retirement as retirement,
    d0f4a6b8c1d3_join_attestations_hostnames as hostnames,
    a7f1b3d5e9a6_join_archive_compatibility as compatibility,
)

revision = "d1a5b7c9d2e4"
down_revision = (retirement.revision, hostnames.revision, compatibility.revision)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
