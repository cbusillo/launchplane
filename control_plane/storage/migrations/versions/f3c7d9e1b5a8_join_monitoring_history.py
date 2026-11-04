from control_plane.storage.migrations.versions import (
    d1a5b7c9d2e4_join_attestations_compatibility as compatibility,
    e2b6c8d0a4f7_monitoring_authority_history as monitoring_history,
)

revision = "f3c7d9e1b5a8"
down_revision = (compatibility.revision, monitoring_history.revision)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
