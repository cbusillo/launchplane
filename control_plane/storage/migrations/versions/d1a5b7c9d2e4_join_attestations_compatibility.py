from control_plane.storage.migrations.versions import (
    c8d2e4f6a9b1_add_integration_attestations as attestations,
    f87c72d690ab_join_idle_compatibility as compatibility,
)

revision = "d1a5b7c9d2e4"
down_revision = (attestations.revision, compatibility.revision)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
