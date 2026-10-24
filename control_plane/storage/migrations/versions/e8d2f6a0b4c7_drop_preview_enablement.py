"""Drop Launchplane's retired preview enablement under Director decision Q533.

This sibling of the held delivery-history migration applies independently at
service startup. It does not authorize the unrelated #2437 table drops.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "e8d2f6a0b4c7"
down_revision: str = "e6b0c4d8f2a5"
branch_labels: str | None = None
depends_on: str | None = None

RETIRED_TABLE = "launchplane_preview_enablement"


def upgrade() -> None:
    connection = op.get_bind()
    if RETIRED_TABLE not in sa.inspect(connection).get_table_names():
        return
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text(f"LOCK TABLE {RETIRED_TABLE} IN ACCESS EXCLUSIVE MODE NOWAIT"))
    # Restrictive DROP refuses unexpected dependencies and never cascades.
    op.drop_table(RETIRED_TABLE)


def downgrade() -> None:
    raise RuntimeError(
        "Retired preview enablement rows cannot be recovered by downgrade; "
        "restore the pre-migration database backup with its matching image."
    )
