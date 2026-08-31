"""Add inert solo-administration confirmation records.

Revision ID: c4e6a8b0d2f6
Revises: c4e6a8b0d2f5
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4e6a8b0d2f6"
down_revision: str | None = "c4e6a8b0d2f5"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE = "launchplane_solo_administration_confirmations"
_EVENT_TABLE = "launchplane_solo_administration_confirmation_events"
_STATE_EXPIRY_INDEX = "launchplane_solo_administration_confirmation_state_expiry_idx"
_SESSION_INDEX = "launchplane_solo_administration_confirmation_session_idx"
_ISSUED_BINDING_INDEX = "launchplane_solo_administration_confirmation_issued_binding_uq"
_EVENT_CONFIRMATION_INDEX = "lp_solo_admin_confirmation_event_confirmation_idx"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _event_table_exists() -> bool:
    return _EVENT_TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    return _table_exists() and index_name in {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if index.get("name") is not None
    }


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def _table_has_rows() -> bool:
    if not _table_exists():
        return False
    return bool(op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")))


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("confirmation_id", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("active_policy_record_id", sa.String(), nullable=False),
            sa.Column("active_policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("active_policy_sha256", sa.String(), nullable=False),
            sa.Column("candidate_policy_sha256", sa.String(), nullable=False),
            sa.Column("candidate_administrator_quorum", sa.BigInteger(), nullable=False),
            sa.Column(
                "candidate_distinct_human_administrator_count",
                sa.BigInteger(),
                nullable=False,
            ),
            sa.Column("reviewed_plan_sha256", sa.String(), nullable=False),
            sa.Column("human_session_id_sha256", sa.String(), nullable=False),
            sa.Column("github_id", sa.BigInteger(), nullable=False),
            sa.Column("idempotency_scope_sha256", sa.String(), nullable=False),
            sa.Column("idempotency_key_sha256", sa.String(), nullable=False),
            sa.Column("acknowledgement_sha256", sa.String(), nullable=False),
            sa.Column("secret_sha256", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("terminal_at", sa.String(), nullable=True),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            sa.Column(
                "authorizes_policy",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            _payload_column(),
            sa.CheckConstraint(
                "state IN ('issued', 'consumed', 'revoked', 'expired')",
                name="launchplane_solo_admin_confirmation_state_ck",
            ),
            sa.CheckConstraint(
                "active_policy_revision > 0 AND github_id > 0",
                name="launchplane_solo_admin_confirmation_positive_ids_ck",
            ),
            sa.CheckConstraint(
                "candidate_administrator_quorum = 1 AND "
                "candidate_distinct_human_administrator_count = 1",
                name="launchplane_solo_admin_confirmation_solo_quorum_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert' AND authorizes_policy = false",
                name="launchplane_solo_admin_confirmation_no_authority_ck",
            ),
            sa.CheckConstraint(
                "expires_at > created_at",
                name="launchplane_solo_admin_confirmation_expiry_ck",
            ),
            sa.CheckConstraint(
                "strftime('%s', expires_at) IS NOT NULL "
                "AND strftime('%s', created_at) IS NOT NULL "
                "AND CAST(strftime('%s', expires_at) AS INTEGER) "
                "- CAST(strftime('%s', created_at) AS INTEGER) = 300",
                name="lp_solo_admin_confirmation_ttl_sqlite_ck",
            ).ddl_if(dialect="sqlite"),
            sa.CheckConstraint(
                "CAST(expires_at AS timestamptz) - CAST(created_at AS timestamptz) "
                "= INTERVAL '5 minutes'",
                name="lp_solo_admin_confirmation_ttl_pg_ck",
            ).ddl_if(dialect="postgresql"),
            sa.CheckConstraint(
                "(state = 'issued' AND terminal_at IS NULL) OR "
                "(state IN ('consumed', 'revoked') AND terminal_at >= created_at "
                "AND terminal_at < expires_at) OR "
                "(state = 'expired' AND terminal_at >= expires_at)",
                name="launchplane_solo_admin_confirmation_terminal_ck",
            ),
            sa.PrimaryKeyConstraint("confirmation_id"),
        )
    if not _index_exists(_STATE_EXPIRY_INDEX):
        op.create_index(_STATE_EXPIRY_INDEX, _TABLE, ["state", "expires_at"])
    if not _index_exists(_SESSION_INDEX):
        op.create_index(_SESSION_INDEX, _TABLE, ["human_session_id_sha256", "created_at"])
    if not _index_exists(_ISSUED_BINDING_INDEX):
        op.create_index(
            _ISSUED_BINDING_INDEX,
            _TABLE,
            [
                "reviewed_plan_sha256",
                "human_session_id_sha256",
                "idempotency_scope_sha256",
                "idempotency_key_sha256",
            ],
            unique=True,
            postgresql_where=sa.text("state = 'issued'"),
            sqlite_where=sa.text("state = 'issued'"),
        )
    if not _event_table_exists():
        op.create_table(
            _EVENT_TABLE,
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("confirmation_id", sa.String(), nullable=False),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("from_state", sa.String(), nullable=False),
            sa.Column("to_state", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            sa.Column("authorizes_policy", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "event_type IN ('issued', 'consumed', 'revoked', 'expired')",
                name="launchplane_solo_admin_confirmation_event_type_ck",
            ),
            sa.CheckConstraint(
                "(event_type = 'issued' AND from_state = '' AND to_state = 'issued') OR "
                "(event_type IN ('consumed', 'revoked', 'expired') AND from_state = 'issued' "
                "AND to_state = event_type)",
                name="launchplane_solo_admin_confirmation_event_transition_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert' AND authorizes_policy = false",
                name="launchplane_solo_admin_confirmation_event_authority_ck",
            ),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint(
                "confirmation_id",
                "event_type",
                name="launchplane_solo_admin_confirmation_event_transition_uq",
            ),
        )
    if _event_table_exists() and _EVENT_CONFIRMATION_INDEX not in {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(_EVENT_TABLE)
        if index.get("name") is not None
    }:
        op.create_index(_EVENT_CONFIRMATION_INDEX, _EVENT_TABLE, ["confirmation_id", "occurred_at"])


def downgrade() -> None:
    if not _table_exists():
        return
    if _table_has_rows():
        raise RuntimeError(
            "Cannot downgrade solo-administration confirmation storage while records exist."
        )
    if _index_exists(_ISSUED_BINDING_INDEX):
        op.drop_index(_ISSUED_BINDING_INDEX, table_name=_TABLE)
    if _index_exists(_SESSION_INDEX):
        op.drop_index(_SESSION_INDEX, table_name=_TABLE)
    if _index_exists(_STATE_EXPIRY_INDEX):
        op.drop_index(_STATE_EXPIRY_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
    if _event_table_exists():
        if _EVENT_CONFIRMATION_INDEX in {
            str(index["name"])
            for index in sa.inspect(op.get_bind()).get_indexes(_EVENT_TABLE)
            if index.get("name") is not None
        }:
            op.drop_index(_EVENT_CONFIRMATION_INDEX, table_name=_EVENT_TABLE)
        op.drop_table(_EVENT_TABLE)
