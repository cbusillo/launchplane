"""Store ODOO_KEY per Odoo site instead of only globally.

Each Odoo site gets its own context-scoped copy of the global ODOO_KEY, so a
site's environment no longer depends on a secret shared by every product. The
global record stays for the paths that still read globals.

Revision ID: a5d7f9b1c3e6
Revises: f4c6e8a0b2d5
"""

from collections.abc import Sequence

from alembic import op

from control_plane.storage.worker_secret_migration import (
    RUNTIME_ENVIRONMENT_INTEGRATION,
    copy_global_secret_to_contexts,
    remove_copied_secrets,
)

revision: str = "a5d7f9b1c3e6"
down_revision: str | None = "f4c6e8a0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ODOO_SITE_CONTEXTS = ("cm", "cm_website", "opw")
_COPIED_AT = "2026-09-29T00:00:00Z"


def upgrade() -> None:
    copy_global_secret_to_contexts(
        op.get_bind(),
        integration=RUNTIME_ENVIRONMENT_INTEGRATION,
        binding_key="ODOO_KEY",
        contexts=ODOO_SITE_CONTEXTS,
        recorded_at=_COPIED_AT,
    )


def downgrade() -> None:
    remove_copied_secrets(
        op.get_bind(),
        integration=RUNTIME_ENVIRONMENT_INTEGRATION,
        contexts=ODOO_SITE_CONTEXTS,
    )
