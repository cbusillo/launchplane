"""add owner control shadow verifier

Revision ID: e5f7a9b1c3d6
Revises: f2241a0b1c2d
Create Date: 2026-08-28 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e5f7a9b1c3d6"
down_revision: str | None = "f2241a0b1c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SESSIONS_TABLE = "launchplane_owner_control_channel_sessions"
_CHALLENGES_TABLE = "launchplane_owner_control_issued_challenges"
_EVENTS_TABLE = "launchplane_owner_control_shadow_verification_events"
_SESSIONS_STATUS_INDEX = "launchplane_owner_control_session_status_idx"
_CHALLENGES_SESSION_INDEX = "launchplane_owner_control_challenge_session_idx"
_CHALLENGES_STATE_INDEX = "launchplane_owner_control_challenge_state_idx"
_EVENTS_CHALLENGE_INDEX = "launchplane_owner_control_shadow_event_challenge_idx"
_EVENTS_SESSION_INDEX = "launchplane_owner_control_shadow_event_session_idx"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    return _table_exists(table_name) and index_name in {
        str(name)
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if (name := index.get("name")) is not None
    }


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if not _table_exists(_SESSIONS_TABLE):
        op.create_table(
            _SESSIONS_TABLE,
            sa.Column("channel_session_id", sa.String(), nullable=False),
            sa.Column("owner_github_id", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("session_issued_at", sa.String(), nullable=False),
            sa.Column("session_expires_at", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("enrolled_at", sa.String(), nullable=False),
            sa.Column("revoked_at", sa.String(), nullable=True),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            _payload_column(),
            sa.CheckConstraint(
                "status IN ('enrolled', 'revoked')",
                name="launchplane_owner_control_session_status_ck",
            ),
            sa.CheckConstraint(
                "(status = 'enrolled' AND revoked_at IS NULL) OR "
                "(status = 'revoked' AND revoked_at IS NOT NULL)",
                name="launchplane_owner_control_session_revocation_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert'",
                name="launchplane_owner_control_session_authority_ck",
            ),
            sa.PrimaryKeyConstraint("channel_session_id"),
            sa.UniqueConstraint(
                "owner_github_id",
                "binding_sha256",
                name="launchplane_owner_control_session_owner_binding_uq",
            ),
        )
    if not _index_exists(_SESSIONS_TABLE, _SESSIONS_STATUS_INDEX):
        op.create_index(
            _SESSIONS_STATUS_INDEX,
            _SESSIONS_TABLE,
            ["status", "session_expires_at"],
        )

    if not _table_exists(_CHALLENGES_TABLE):
        op.create_table(
            _CHALLENGES_TABLE,
            sa.Column("challenge_id", sa.String(), nullable=False),
            sa.Column("challenge_nonce", sa.String(), nullable=False),
            sa.Column("channel_session_id", sa.String(), nullable=False),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("descriptor_id", sa.String(), nullable=False),
            sa.Column("owner_github_id", sa.BigInteger(), nullable=False),
            sa.Column("issued_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("approval_request_sha256", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("consumed_at", sa.String(), nullable=True),
            sa.Column("terminal_event_id", sa.String(), nullable=True),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            _payload_column(),
            sa.CheckConstraint(
                "expires_at > issued_at",
                name="launchplane_owner_control_challenge_expiry_ck",
            ),
            sa.CheckConstraint(
                "state IN ('issued', 'consumed', 'expired', 'rejected')",
                name="launchplane_owner_control_challenge_state_ck",
            ),
            sa.CheckConstraint(
                "attempt_count BETWEEN 0 AND 8",
                name="launchplane_owner_control_challenge_attempt_count_ck",
            ),
            sa.CheckConstraint(
                "(state = 'issued' AND consumed_at IS NULL AND terminal_event_id IS NULL) OR "
                "(state = 'consumed' AND consumed_at IS NOT NULL AND terminal_event_id IS NOT NULL) OR "
                "(state IN ('expired', 'rejected') AND consumed_at IS NULL AND terminal_event_id IS NOT NULL)",
                name="launchplane_owner_control_challenge_terminal_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert'",
                name="launchplane_owner_control_challenge_authority_ck",
            ),
            sa.PrimaryKeyConstraint("challenge_id"),
            sa.UniqueConstraint(
                "challenge_nonce",
                name="launchplane_owner_control_challenge_nonce_uq",
            ),
            sa.UniqueConstraint(
                "approval_request_sha256",
                name="launchplane_owner_control_challenge_request_digest_uq",
            ),
        )
    if not _index_exists(_CHALLENGES_TABLE, _CHALLENGES_SESSION_INDEX):
        op.create_index(
            _CHALLENGES_SESSION_INDEX,
            _CHALLENGES_TABLE,
            ["channel_session_id", "expires_at"],
        )
    if not _index_exists(_CHALLENGES_TABLE, _CHALLENGES_STATE_INDEX):
        op.create_index(
            _CHALLENGES_STATE_INDEX,
            _CHALLENGES_TABLE,
            ["state", "expires_at"],
        )

    if not _table_exists(_EVENTS_TABLE):
        op.create_table(
            _EVENTS_TABLE,
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("challenge_id", sa.String(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("channel_session_id", sa.String(), nullable=False),
            sa.Column("challenge_nonce", sa.String(), nullable=False),
            sa.Column("envelope_sha256", sa.String(), nullable=False),
            sa.Column("approval_request_sha256", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("verification_status", sa.String(), nullable=False),
            sa.Column("rejection_reason", sa.String(), nullable=True),
            sa.Column("resulting_challenge_state", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("verifier_mode", sa.String(), nullable=False, server_default="shadow"),
            sa.Column(
                "authorizes_execution",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            _payload_column(),
            sa.CheckConstraint(
                "verification_status IN ('verified', 'rejected')",
                name="launchplane_owner_control_shadow_event_status_ck",
            ),
            sa.CheckConstraint(
                "verifier_mode = 'shadow'",
                name="launchplane_owner_control_shadow_event_mode_ck",
            ),
            sa.CheckConstraint(
                "authorizes_execution = false",
                name="launchplane_owner_control_shadow_event_authorization_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert'",
                name="launchplane_owner_control_shadow_event_authority_ck",
            ),
            sa.CheckConstraint(
                "sequence BETWEEN 1 AND 8",
                name="launchplane_owner_control_shadow_event_sequence_ck",
            ),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint(
                "challenge_id",
                "sequence",
                name="launchplane_owner_control_shadow_event_sequence_uq",
            ),
        )
    if not _index_exists(_EVENTS_TABLE, _EVENTS_CHALLENGE_INDEX):
        op.create_index(
            _EVENTS_CHALLENGE_INDEX,
            _EVENTS_TABLE,
            ["challenge_nonce", sa.text("occurred_at DESC")],
        )
    if not _index_exists(_EVENTS_TABLE, _EVENTS_SESSION_INDEX):
        op.create_index(
            _EVENTS_SESSION_INDEX,
            _EVENTS_TABLE,
            ["channel_session_id", sa.text("occurred_at DESC")],
        )


def downgrade() -> None:
    for table_name, index_name in (
        (_EVENTS_TABLE, _EVENTS_SESSION_INDEX),
        (_EVENTS_TABLE, _EVENTS_CHALLENGE_INDEX),
        (_CHALLENGES_TABLE, _CHALLENGES_STATE_INDEX),
        (_CHALLENGES_TABLE, _CHALLENGES_SESSION_INDEX),
        (_SESSIONS_TABLE, _SESSIONS_STATUS_INDEX),
    ):
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
    for table_name in (_EVENTS_TABLE, _CHALLENGES_TABLE, _SESSIONS_TABLE):
        if _table_exists(table_name):
            op.drop_table(table_name)
