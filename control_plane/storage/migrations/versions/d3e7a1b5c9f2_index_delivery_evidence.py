"""Index delivery-monitor sources and release-step dependencies.

Revision ID: d3e7a1b5c9f2
Revises: cd192c59f009
"""

from alembic import op
from sqlalchemy import select, table, column, JSON
from sqlalchemy.dialects.sqlite import insert

from control_plane.storage.migrations.delivery_evidence_v1 import (
    metadata,
    SOURCE_KINDS,
    sources,
    source_values,
    projection_insert,
    install_triggers,
    remove_triggers,
)

revision = "d3e7a1b5c9f2"
down_revision = "cd192c59f009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    metadata.create_all(connection)
    install_triggers(connection)
    # Frozen raw-JSON backfill takes no per-operation advisory locks and does
    # not ask current runtime validators to accept historical payloads.
    for name, kind in SOURCE_KINDS.items():
        if connection.dialect.name == "postgresql":
            from sqlalchemy import text

            identity = "operation_id" if kind == "operation" else "record_id"
            connection.execute(
                text(
                    projection_insert(
                        "postgresql",
                        f"SELECT payload::jsonb AS p, '{kind}' AS kind, {identity} AS source_id FROM {name}",
                        baseline=True,
                    )
                )
            )
        else:
            source = table(name, column("payload", JSON))
            for payload in connection.execute(select(source.c.payload)).scalars():
                values = source_values(kind, payload)
                if values:
                    values["baseline"] = True
                    connection.execute(insert(sources).values(**values).on_conflict_do_nothing())


def downgrade() -> None:
    remove_triggers(op.get_bind())
    metadata.drop_all(op.get_bind())
