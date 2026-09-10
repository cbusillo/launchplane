"""Add ordinary-agent provider custody issue attempts.

Revision ID: c8f2a6d4e9b1
Revises: f3a5b7c9d1e4
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c8f2a6d4e9b1"
down_revision: str | None = "f3a5b7c9d1e4"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "launchplane_ordinary_agent_custody_issue_attempts"
_ACTIVE_INDEX = "launchplane_ordinary_agent_custody_active_fence_uidx"
_STATE_INDEX = "launchplane_ordinary_agent_custody_state_residual_idx"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    return _table_exists() and index_name in {
        str(name)
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if (name := index.get("name")) is not None
    }


def upgrade() -> None:
    payload_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("idempotency_key_sha256", sa.String(), nullable=False),
            sa.Column("request_sha256", sa.String(), nullable=False),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.BigInteger(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("mint_started_at", sa.String(), nullable=False),
            sa.Column("dispatch_deadline", sa.String(), nullable=False),
            sa.Column("residual_expires_at", sa.String(), nullable=True),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("payload", payload_type, nullable=False),
            sa.CheckConstraint(
                "state IN ('minting', 'issued', 'issue_unknown', 'cleanup_unknown', 'closed')",
                name="launchplane_ordinary_agent_custody_issue_state_ck",
            ),
            sa.CheckConstraint(
                "repository_id > 0",
                name="launchplane_ordinary_agent_custody_issue_repository_id_ck",
            ),
            sa.PrimaryKeyConstraint("attempt_id"),
            sa.UniqueConstraint(
                "idempotency_key_sha256",
                name="launchplane_ordinary_agent_custody_issue_idempotency_uq",
            ),
        )
    if not _index_exists(_ACTIVE_INDEX):
        op.create_index(
            _ACTIVE_INDEX,
            _TABLE,
            ["principal_id", "repository_id"],
            unique=True,
            postgresql_where=sa.text(
                "state IN ('minting', 'issued', 'issue_unknown', 'cleanup_unknown')"
            ),
            sqlite_where=sa.text(
                "state IN ('minting', 'issued', 'issue_unknown', 'cleanup_unknown')"
            ),
        )
    if not _index_exists(_STATE_INDEX):
        op.create_index(
            _STATE_INDEX,
            _TABLE,
            ["state", "residual_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    if not _table_exists():
        return
    connection = op.get_bind()
    if connection.execute(sa.text(f"SELECT 1 FROM {_TABLE} LIMIT 1")).first() is not None:
        raise RuntimeError("Refusing to drop non-empty ordinary-agent custody evidence.")
    if _index_exists(_STATE_INDEX):
        op.drop_index(_STATE_INDEX, table_name=_TABLE)
    if _index_exists(_ACTIVE_INDEX):
        op.drop_index(_ACTIVE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
