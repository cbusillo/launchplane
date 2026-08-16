"""remove Owner acceptance shadow mode

Revision ID: f0a2c4e6b8d1
Revises: e9b1d3f5a7c0
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import RowMapping


revision: str = "f0a2c4e6b8d1"
down_revision: str | None = "e9b1d3f5a7c0"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "launchplane_product_owner_requirements"
_ARCHIVE_TABLE = "launchplane_product_owner_requirement_authority_migrations"
_COLUMN = "enforcement_mode"
_CHECK = "launchplane_product_owner_requirement_shadow_ck"
_MIGRATION_SOURCE = "migration:owner-authority-cutover"
_MIGRATION_REASON = "Require an explicit Owner requirement revision before authority is active."


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns(_TABLE)}


def _canonical_digest(payload: dict[str, object]) -> str:
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"requirement_digest", "status"} and value is not None
    }
    encoded = json.dumps(
        digest_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision(value: object) -> int:
    if not isinstance(value, int) or value < 1:
        raise ValueError("product Owner requirement revision must be positive")
    return value


def _record_id(*, product: str, system: str, revision: int) -> str:
    scope_digest = _canonical_digest({"product": product, "system": system})
    return f"product-owner-requirement-{scope_digest[:16]}-r{revision}"


def _archive_table() -> sa.Table:
    connection = op.get_bind()
    if _ARCHIVE_TABLE not in sa.inspect(connection).get_table_names():
        return op.create_table(
            _ARCHIVE_TABLE,
            sa.Column("record_id", sa.String(), primary_key=True),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("system", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("requirement_revision", sa.BigInteger(), nullable=False),
            sa.Column("enforcement_mode", sa.String(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("requirement_digest", sa.String(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    return sa.Table(_ARCHIVE_TABLE, sa.MetaData(), autoload_with=connection)


def _archive_and_replace_requirements() -> None:
    connection = op.get_bind()
    table = sa.Table(_TABLE, sa.MetaData(), autoload_with=connection)
    archive = _archive_table()
    rows = tuple(connection.execute(sa.select(table)).mappings())
    archived_record_ids = set(connection.execute(sa.select(archive.c.record_id)).scalars())
    for row in rows:
        if row["record_id"] in archived_record_ids:
            continue
        connection.execute(
            sa.insert(archive).values(
                record_id=row["record_id"],
                product=row["product"],
                system=row["system"],
                status=row["status"],
                requirement_revision=row["requirement_revision"],
                enforcement_mode=row[_COLUMN],
                effective_at=row["effective_at"],
                source=row["source"],
                supersedes_record_id=row["supersedes_record_id"],
                requirement_digest=row["requirement_digest"],
                payload=row["payload"],
            )
        )

    latest_by_scope: dict[tuple[str, str], RowMapping] = {}
    for row in rows:
        scope = str(row["product"]), str(row["system"])
        current = latest_by_scope.get(scope)
        if current is None or _revision(row["requirement_revision"]) > _revision(
            current["requirement_revision"]
        ):
            latest_by_scope[scope] = row

    connection.execute(sa.delete(table))
    for (product, system), previous in latest_by_scope.items():
        revision = 1
        payload: dict[str, object] = {
            "schema_version": 1,
            "record_id": _record_id(product=product, system=system, revision=revision),
            "status": "active",
            "product": product,
            "system": system,
            "requirement_revision": revision,
            "requirements": [],
            "effective_at": str(previous["effective_at"]),
            "source": _MIGRATION_SOURCE,
            "reason": _MIGRATION_REASON,
            "supersedes_record_id": None,
        }
        digest = _canonical_digest(payload)
        payload["requirement_digest"] = digest
        connection.execute(
            sa.insert(table).values(
                record_id=payload["record_id"],
                product=product,
                system=system,
                status="active",
                requirement_revision=revision,
                enforcement_mode="shadow",
                effective_at=payload["effective_at"],
                source=_MIGRATION_SOURCE,
                supersedes_record_id=payload["supersedes_record_id"],
                requirement_digest=digest,
                payload=payload,
            )
        )


def upgrade() -> None:
    if _COLUMN not in _columns():
        return
    _archive_and_replace_requirements()
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CHECK, type_="check")
        batch.drop_column(_COLUMN)


def downgrade() -> None:
    pass
