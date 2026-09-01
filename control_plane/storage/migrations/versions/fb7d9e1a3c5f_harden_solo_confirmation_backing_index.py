"""Bind consumed solo-confirmation backing to recovery provenance.

Revision ID: fb7d9e1a3c5f
Revises: f5e7a9b1c3d4
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "fb7d9e1a3c5f"
down_revision: str | None = "f5e7a9b1c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_solo_administration_confirmations"
_OLD_INDEX = "lp_solo_admin_confirmation_consumed_candidate_idx"
_INDEX = "lp_solo_admin_confirmation_consumed_recovery_idx"


def upgrade() -> None:
    indexes = {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _OLD_INDEX in indexes:
        op.drop_index(_OLD_INDEX, table_name=_TABLE)
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            _TABLE,
            ["candidate_policy_sha256", "github_id", "idempotency_scope_sha256", "state"],
        )


def downgrade() -> None:
    indexes = {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    if _OLD_INDEX not in indexes:
        op.create_index(_OLD_INDEX, _TABLE, ["candidate_policy_sha256", "state"])
