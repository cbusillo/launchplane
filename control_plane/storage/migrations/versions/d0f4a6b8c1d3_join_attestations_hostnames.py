from control_plane.storage.migrations.versions import (
    c8d2e4f6a9b1_add_integration_attestations as attestations,
    a6e0a2c4d8f5_join_archive_topics_hostnames as hostnames,
)

revision = "d0f4a6b8c1d3"
down_revision = (attestations.revision, hostnames.revision)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
