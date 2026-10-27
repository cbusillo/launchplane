from control_plane.storage.migrations.versions import (
    c8d2e4f6a9b1_add_integration_attestations as attestations,
    a5d9f1b3c7e4_join_archive_topics_retirement as retirement,
)

revision = "c9e3f5a7b0c2"
down_revision = (attestations.revision, retirement.revision)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
