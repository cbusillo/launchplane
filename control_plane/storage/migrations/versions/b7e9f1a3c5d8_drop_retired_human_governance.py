"""Drop retired manager, role-policy and technical-waiver storage.

Revision ID: b7e9f1a3c5d8
Revises: 49a61248b8c5

The Director approved keeping nothing in launchplane#2006 (5969429346).
Upgrade deletes all rows without exporting or counting them. Downgrade restores
only the empty historical schemas; it cannot recover the deleted data.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from control_plane.storage.migrations.versions import (
    a0d2f4b6c8e1_add_repository_human_admission_storage as human_admission,
    e8a0c2d4f6b8_add_manager_preview_approval_events as manager_approval,
)

revision: str = "b7e9f1a3c5d8"
down_revision: str = "49a61248b8c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIRED_TABLES = (
    "launchplane_tenant_technical_human_waiver_events",
    "launchplane_repository_human_role_policies",
    "launchplane_manager_preview_approval_events",
)


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in RETIRED_TABLES:
        if table_name in existing_tables:
            op.drop_table(table_name)


def downgrade() -> None:
    manager_approval.upgrade()
    human_admission.upgrade()
