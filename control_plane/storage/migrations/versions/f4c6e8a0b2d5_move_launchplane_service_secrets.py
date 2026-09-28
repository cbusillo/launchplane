"""Move Launchplane's own credentials out of the app runtime environment store.

Also disables the records stored under the misspelled `runtime-environment`
integration, which nothing reads.

Revision ID: f4c6e8a0b2d5
Revises: e2b4d6f8a1c3
"""

from collections.abc import Sequence

from alembic import op

from control_plane.storage.worker_secret_migration import (
    LAUNCHPLANE_SERVICE_INTEGRATION,
    MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION,
    RUNTIME_ENVIRONMENT_INTEGRATION,
    move_service_secrets,
    set_integration_status,
)

revision: str = "f4c6e8a0b2d5"
down_revision: str | None = "e2b4d6f8a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    move_service_secrets(
        connection,
        source=RUNTIME_ENVIRONMENT_INTEGRATION,
        destination=LAUNCHPLANE_SERVICE_INTEGRATION,
    )
    set_integration_status(
        connection, integration=MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION, status="disabled"
    )


def downgrade() -> None:
    connection = op.get_bind()
    set_integration_status(
        connection, integration=MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION, status="configured"
    )
    move_service_secrets(
        connection,
        source=LAUNCHPLANE_SERVICE_INTEGRATION,
        destination=RUNTIME_ENVIRONMENT_INTEGRATION,
    )
