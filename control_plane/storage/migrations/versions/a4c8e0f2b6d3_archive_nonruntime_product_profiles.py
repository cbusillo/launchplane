from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

from control_plane.storage.migrations.versions import (
    b81ca763d924_add_discussion_topics as topics,
)

revision: str = "a4c8e0f2b6d3"
down_revision: str = topics.revision
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "launchplane_product_profiles"
ARCHIVE_TABLE = "launchplane_product_profile_archive"
MIGRATION_SOURCE = "migration:nonruntime-product-retirement"
MIGRATION_REASON = (
    "Launchplane records only products it deploys; a repository without a product "
    "needs no Client acceptance."
)


def _payload(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("product profile payload must be an object")
    return value


def _archive_table() -> sa.Table:
    connection = op.get_bind()
    if ARCHIVE_TABLE not in sa.inspect(connection).get_table_names():
        return op.create_table(
            ARCHIVE_TABLE,
            sa.Column("product", sa.String(), primary_key=True),
            sa.Column("display_name", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("reason", sa.String(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    return sa.Table(ARCHIVE_TABLE, sa.MetaData(), autoload_with=connection)


def upgrade() -> None:
    connection = op.get_bind()
    archive = _archive_table()
    profiles = sa.Table(_TABLE, sa.MetaData(), autoload_with=connection)
    archived = set(connection.execute(sa.select(archive.c.product)).scalars())
    removed: list[str] = []
    for row in connection.execute(sa.select(profiles)).mappings():
        payload = _payload(row["payload"])
        if payload.get("classification", "managed_runtime") == "managed_runtime":
            continue
        if row["product"] not in archived:
            connection.execute(
                sa.insert(archive).values(
                    product=row["product"],
                    display_name=row["display_name"],
                    repository=row["repository"],
                    updated_at=row["updated_at"],
                    source=MIGRATION_SOURCE,
                    reason=MIGRATION_REASON,
                    payload=payload,
                )
            )
        removed.append(str(row["product"]))
    if removed:
        connection.execute(sa.delete(profiles).where(profiles.c.product.in_(removed)))


def downgrade() -> None:
    raise RuntimeError(
        "Archived product profiles stay in launchplane_product_profile_archive; "
        "restore one only through a reviewed admin procedure."
    )
