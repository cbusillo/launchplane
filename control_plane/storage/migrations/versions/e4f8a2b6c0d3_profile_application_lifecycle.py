"""Convert retired application extensions into nonproduction profile declarations."""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

from control_plane.contracts.product_application_lifecycle_migration import (
    migrate_product_application_lifecycle_payload,
)

revision: str = "e4f8a2b6c0d3"
down_revision: str | None = "d3e7a1b5c9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    profiles = sa.table(
        "launchplane_product_profiles",
        sa.column("product", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    for row in connection.execute(sa.select(profiles.c.product, profiles.c.payload)):
        converted = migrate_product_application_lifecycle_payload(row.payload)
        if converted != row.payload:
            connection.execute(
                profiles.update().where(profiles.c.product == row.product).values(payload=converted)
            )


def downgrade() -> None:
    # Declarations are operator-editable after adoption; never remove their work.
    pass
