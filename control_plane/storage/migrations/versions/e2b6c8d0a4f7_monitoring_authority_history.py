from alembic import op

from control_plane.storage.migrations.monitoring_authority_v1 import initialize
from control_plane.storage.migrations.versions import (
    c8d2e4f6a9b1_add_integration_attestations as attestations,
)

revision = "e2b6c8d0a4f7"
down_revision = attestations.revision
branch_labels = None
depends_on = None


def upgrade() -> None:
    initialize(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Monitoring authority decisions and their coverage epoch must be retained.")
