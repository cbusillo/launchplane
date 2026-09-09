"""Add the terminal-snapshot feedback intent uniqueness fence.

Revision ID: f3a5b7c9d1e4
Revises: e2f4a6b8c0d3

V1 payloads remain unchanged and unverified. V2 issuance evidence is embedded
in the existing JSON payload, never backfilled from historical expectations.
Conflicting historical snapshots fail migration rather than deleting evidence.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "f3a5b7c9d1e4"
down_revision: str | None = "e2f4a6b8c0d3"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "launchplane_every_code_feedback_resume_intents"
_INDEX = "launchplane_every_code_feedback_resume_intent_snapshot_uidx"


def upgrade() -> None:
    existing = sa.inspect(op.get_bind()).get_indexes(_TABLE)
    if _INDEX not in {index["name"] for index in existing}:
        op.create_index(
            _INDEX,
            _TABLE,
            ["acceptance_id", "expected_lifecycle_id", "expected_fencing_token"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
