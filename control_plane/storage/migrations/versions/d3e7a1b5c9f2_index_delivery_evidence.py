"""Index delivery-monitor sources and release-step dependencies.

Revision ID: d3e7a1b5c9f2
Revises: bc081b48f008
"""

from alembic import op
from sqlalchemy import select, table, column, JSON

from control_plane.storage.delivery_evidence import (
    metadata,
    SOURCE_KINDS,
    source_changed,
    install_delete_triggers,
    remove_delete_triggers,
)

revision = "d3e7a1b5c9f2"
down_revision = "bc081b48f008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    metadata.create_all(connection)
    install_delete_triggers(connection)
    # One upgrade-time traversal. Ongoing writes mark work transactionally;
    # scheduler passes never enumerate these histories again.
    for name in SOURCE_KINDS:
        source = table(name, column("payload", JSON))
        for payload in connection.execute(select(source.c.payload)).scalars():
            source_changed(connection, name, payload)


def downgrade() -> None:
    remove_delete_triggers(op.get_bind())
    metadata.drop_all(op.get_bind())
