"""bind owner review identity, age, policy, and preview trust context

Revision ID: b2d4f6a8c0e2
Revises: a4c6e8f0b2d5
Create Date: 2026-08-10 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from alembic import op
import sqlalchemy as sa

revision: str = "b2d4f6a8c0e2"
down_revision: str | None = "a4c6e8f0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TABLE = "launchplane_owner_acceptance_events"
_POLICY_TABLE = "launchplane_product_owner_policies"

_ROUTINE_REVIEW_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_ELEVATED_REVIEW_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

_REVIEW_AGE_DEFAULT = {
    "schema_version": 1,
    "routine_max_age_seconds": _ROUTINE_REVIEW_MAX_AGE_SECONDS,
    "elevated_max_age_seconds": _ELEVATED_REVIEW_MAX_AGE_SECONDS,
}
_SELF_REVIEW_DEFAULT = {
    "schema_version": 1,
    "routine_exception_enabled": False,
    "exception_revision": 0,
    "exception_reason": "",
}
_PREVIEW_TRUST_DEFAULT = {
    "schema_version": 1,
    "minimum_isolation_class": "synthetic_seeded",
}

_EVENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[Any], str], ...] = (
    ("base_ref", sa.String(), "''"),
    ("base_sha", sa.String(), "''"),
    ("change_class", sa.String(), "''"),
    ("review_max_age_seconds", sa.BigInteger(), "0"),
    ("contribution_resolution", sa.String(), "''"),
    ("preview_isolation_class", sa.String(), "''"),
)


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names(_EVENT_TABLE)
    if existing:
        for column_name, column_type, server_default in _EVENT_COLUMNS:
            if column_name in existing:
                continue
            op.add_column(
                _EVENT_TABLE,
                sa.Column(
                    column_name,
                    column_type,
                    nullable=False,
                    server_default=sa.text(server_default),
                ),
            )
        if "self_review" not in existing:
            op.add_column(
                _EVENT_TABLE,
                sa.Column(
                    "self_review",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )
    _backfill_owner_policy_defaults()


def _backfill_owner_policy_defaults() -> None:
    """Make the fail-closed review-age, self-review, and preview-trust defaults explicit.

    Records written before this revision carry no scoped policy payload. The
    contract already defaults them to 30-day routine and 7-day
    sensitive/unknown/production review ages, denied self-review, and
    ``synthetic_seeded`` minimum preview isolation. Writing those values into the
    stored payload keeps the persisted record self-describing. It cannot change
    ``policy_digest`` because the scoped policies are deliberately outside the
    membership digest.
    """
    if not _table_exists(_POLICY_TABLE):
        return
    connection = op.get_bind()
    rows = connection.execute(sa.text(f"SELECT record_id, payload FROM {_POLICY_TABLE}")).fetchall()
    for record_id, payload in rows:
        decoded = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(decoded, dict):
            continue
        updated = dict(decoded)
        changed = False
        for key, default in (
            ("review_age", _REVIEW_AGE_DEFAULT),
            ("self_review", _SELF_REVIEW_DEFAULT),
            ("preview_trust", _PREVIEW_TRUST_DEFAULT),
        ):
            if key not in updated:
                updated[key] = dict(default)
                changed = True
        if not changed:
            continue
        connection.execute(
            sa.text(f"UPDATE {_POLICY_TABLE} SET payload = :payload WHERE record_id = :record_id"),
            {
                "payload": (
                    json.dumps(updated, sort_keys=True) if isinstance(payload, str) else updated
                ),
                "record_id": record_id,
            },
        )


def downgrade() -> None:
    existing = _column_names(_EVENT_TABLE)
    for column_name in (
        "self_review",
        "preview_isolation_class",
        "contribution_resolution",
        "review_max_age_seconds",
        "change_class",
        "base_sha",
        "base_ref",
    ):
        if column_name in existing:
            op.drop_column(_EVENT_TABLE, column_name)
