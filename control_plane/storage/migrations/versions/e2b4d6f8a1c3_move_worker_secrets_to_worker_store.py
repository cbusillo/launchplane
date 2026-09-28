"""Move worker-only lane credentials to the Launchplane worker store.

Revision ID: e2b4d6f8a1c3
Revises: d8f66a78c02a
"""

from collections.abc import Sequence

from alembic import op

from control_plane.storage.worker_secret_migration import (
    LAUNCHPLANE_WORKER_INTEGRATION,
    RUNTIME_ENVIRONMENT_INTEGRATION,
    move_worker_secrets,
)

revision: str = "e2b4d6f8a1c3"
down_revision: str | None = "d8f66a78c02a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    move_worker_secrets(
        op.get_bind(),
        source=RUNTIME_ENVIRONMENT_INTEGRATION,
        destination=LAUNCHPLANE_WORKER_INTEGRATION,
    )


def downgrade() -> None:
    move_worker_secrets(
        op.get_bind(),
        source=LAUNCHPLANE_WORKER_INTEGRATION,
        destination=RUNTIME_ENVIRONMENT_INTEGRATION,
    )
