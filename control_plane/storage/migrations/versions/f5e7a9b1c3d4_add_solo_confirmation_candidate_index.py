"""Index consumed solo-confirmation backing lookups.

Revision ID: f5e7a9b1c3d4
Revises: c4e6a8b0d2f6
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f5e7a9b1c3d4"
down_revision: str | None = "c4e6a8b0d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_solo_administration_confirmations"
_INDEX = "lp_solo_admin_confirmation_consumed_candidate_idx"


def upgrade() -> None:
    indexes = {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, ["candidate_policy_sha256", "state"])


def downgrade() -> None:
    indexes = {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
