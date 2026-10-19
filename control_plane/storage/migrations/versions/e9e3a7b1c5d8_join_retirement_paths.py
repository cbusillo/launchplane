"""Join independently applied preview and explicitly approved delivery drops.

The release default targets the preview branch, not this merge. A database
already at the delivery drop can reach this revision without replaying it.
"""

from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as delivery,
    e8d2f6a0b4c7_drop_preview_enablement as preview,
)

revision: str = "e9e3a7b1c5d8"
down_revision: tuple[str, str] = (delivery.revision, preview.revision)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
