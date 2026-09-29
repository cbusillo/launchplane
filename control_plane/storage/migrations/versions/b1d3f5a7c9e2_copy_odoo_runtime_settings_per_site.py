"""Give each Odoo site its own copy of the global Odoo runtime settings.

``ODOO_DB_USER`` and three ``ENV_OVERRIDE_*`` flags are stored only as global
settings, which every product resolves and a site's own environment leaves out.
Each Odoo site gets its own copy, so its target replacement and backup restore can
move onto the site environment. A site's existing value is kept, and the global
settings stay for the paths that still read them.

The downgrade leaves the copies in place: each equals the global value it came
from, so every resolver returns the same environment with or without them, and a
site value the operator has since changed must not be removed.

Revision ID: b1d3f5a7c9e2
Revises: a5d7f9b1c3e6
"""

from collections.abc import Sequence

from alembic import op

from control_plane.storage.site_setting_migration import copy_global_settings_to_contexts

revision: str = "b1d3f5a7c9e2"
down_revision: str | None = "a5d7f9b1c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ODOO_SITE_CONTEXTS = ("cm", "cm_website", "opw")
ODOO_SITE_RUNTIME_KEYS = (
    "ODOO_DB_USER",
    "ENV_OVERRIDE_DISABLE_CRON",
    "ENV_OVERRIDE_SHOPIFY__API_VERSION",
    "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL_FREEZE",
)
_COPIED_AT = "2026-09-29T00:00:00Z"


def upgrade() -> None:
    copy_global_settings_to_contexts(
        op.get_bind(),
        keys=ODOO_SITE_RUNTIME_KEYS,
        contexts=ODOO_SITE_CONTEXTS,
        recorded_at=_COPIED_AT,
        source_label=f"migration:{revision}",
    )


def downgrade() -> None:
    pass
