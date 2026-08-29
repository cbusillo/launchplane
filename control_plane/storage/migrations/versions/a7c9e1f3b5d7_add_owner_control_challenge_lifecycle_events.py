"""Add owner-control challenge lifecycle events.

Revision ID: a7c9e1f3b5d7
Revises: f6a1c3e5b7d9
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7c9e1f3b5d7"
down_revision: str | None = "f6a1c3e5b7d9"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE = "launchplane_owner_control_challenge_lifecycle_events"
_INDEX = "launchplane_owner_control_lifecycle_event_challenge_idx"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists() -> bool:
    return _table_exists() and _INDEX in {
        str(name)
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if (name := index.get("name")) is not None
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
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("challenge_id", sa.String(), nullable=False),
            sa.Column("challenge_nonce", sa.String(), nullable=False),
            sa.Column("channel_session_id", sa.String(), nullable=False),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("approval_request_sha256", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("from_state", sa.String(), nullable=False),
            sa.Column("to_state", sa.String(), nullable=False),
            sa.Column("transition_reason", sa.String(), nullable=False),
            sa.Column("challenge_expires_at", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column(
                "authorizes_execution",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            _payload_column(),
            sa.CheckConstraint(
                "from_state = 'issued'",
                name="launchplane_owner_control_lifecycle_event_from_state_ck",
            ),
            sa.CheckConstraint(
                "to_state = 'expired'",
                name="launchplane_owner_control_lifecycle_event_to_state_ck",
            ),
            sa.CheckConstraint(
                "transition_reason = 'expired'",
                name="launchplane_owner_control_lifecycle_event_reason_ck",
            ),
            sa.CheckConstraint(
                "occurred_at >= challenge_expires_at",
                name="launchplane_owner_control_lifecycle_event_time_ck",
            ),
            sa.CheckConstraint(
                "authorizes_execution = false",
                name="launchplane_owner_control_lifecycle_event_authorization_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert'",
                name="launchplane_owner_control_lifecycle_event_authority_ck",
            ),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint(
                "challenge_id",
                "transition_reason",
                name="launchplane_owner_control_lifecycle_event_transition_uq",
            ),
        )
    if not _index_exists():
        op.create_index(
            _INDEX,
            _TABLE,
            ["challenge_nonce", sa.text("occurred_at DESC")],
        )


def downgrade() -> None:
    if _table_has_rows():
        raise RuntimeError(
            "Cannot downgrade owner-control lifecycle storage while audit events exist."
        )
    if _index_exists():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _table_exists():
        op.drop_table(_TABLE)
