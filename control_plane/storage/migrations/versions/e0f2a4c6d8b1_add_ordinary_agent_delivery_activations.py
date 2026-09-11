"""Add ordinary-agent delivery activation records and append-only events.

Revision ID: e0f2a4c6d8b1
Revises: d8a0b2c4e6f9
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e0f2a4c6d8b1"
down_revision: str | None = "d8a0b2c4e6f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVATIONS = "launchplane_ordinary_agent_delivery_activations"
_EVENTS = "launchplane_ordinary_agent_delivery_activation_events"
_CURRENT_SCOPE_PREDICATE = "revoked_at IS NULL AND superseded_at IS NULL"
_EXPECTED_COLUMNS = {
    _ACTIVATIONS: {
        "activation_id",
        "repository_id",
        "repository",
        "base_branch",
        "managed_set_id",
        "managed_rule_id",
        "source_setup_operation_id",
        "desired_state",
        "effective_state",
        "activation_expires_at",
        "revision",
        "installed_at",
        "updated_at",
        "revoked_at",
        "superseded_by_activation_id",
        "superseded_at",
        "activation_sha256",
        "payload",
    },
    _EVENTS: {
        "event_id",
        "activation_id",
        "sequence",
        "action",
        "previous_revision",
        "previous_activation_sha256",
        "resulting_revision",
        "resulting_activation_sha256",
        "resulting_desired_state",
        "resulting_effective_state",
        "occurred_at",
        "source_operation_id",
        "payload",
    },
}
_EXPECTED_INDEXES = {
    _ACTIVATIONS: {
        "launchplane_ordinary_agent_activation_setup_operation_uidx",
        "launchplane_ordinary_agent_activation_current_scope_uidx",
        "launchplane_ordinary_agent_activation_scope_history_idx",
    },
    _EVENTS: {
        "launchplane_ordinary_agent_activation_event_sequence_uidx",
        "launchplane_agent_activation_event_source_idx",
    },
}
_EXPECTED_CHECKS = {
    _ACTIVATIONS: {
        "launchplane_ordinary_agent_activation_desired_state_ck",
        "launchplane_ordinary_agent_activation_effective_state_ck",
        "launchplane_ordinary_agent_activation_revision_ck",
        "launchplane_ordinary_agent_activation_state_ck",
        "launchplane_ordinary_agent_activation_supersession_ck",
    },
    _EVENTS: {
        "launchplane_ordinary_agent_activation_event_action_ck",
        "launchplane_ordinary_agent_activation_event_revision_floor_ck",
        "launchplane_ordinary_agent_activation_event_transition_ck",
        "launchplane_ordinary_agent_activation_event_source_ck",
    },
}


def _payload_column() -> sa.Column[dict[str, object]]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def _existing_schema_is_complete(inspector: sa.Inspector) -> bool:
    for table_name in (_ACTIVATIONS, _EVENTS):
        columns = {str(column["name"]) for column in inspector.get_columns(table_name)}
        indexes = {str(index["name"]) for index in inspector.get_indexes(table_name)}
        checks = {
            str(check["name"])
            for check in inspector.get_check_constraints(table_name)
            if check.get("name")
        }
        if (
            columns != _EXPECTED_COLUMNS[table_name]
            or indexes != _EXPECTED_INDEXES[table_name]
            or checks != _EXPECTED_CHECKS[table_name]
        ):
            return False
    return True


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    activation_tables = {_ACTIVATIONS, _EVENTS} & existing_tables
    if activation_tables:
        if activation_tables != {_ACTIVATIONS, _EVENTS} or not _existing_schema_is_complete(
            inspector
        ):
            raise RuntimeError(
                "Existing ordinary-agent activation schema is incomplete; refusing to stamp it."
            )
        return
    op.create_table(
        _ACTIVATIONS,
        sa.Column("activation_id", sa.String(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("managed_set_id", sa.String(), nullable=False),
        sa.Column("managed_rule_id", sa.String(), nullable=False),
        sa.Column("source_setup_operation_id", sa.String(), nullable=False),
        sa.Column("desired_state", sa.String(), nullable=False),
        sa.Column("effective_state", sa.String(), nullable=False),
        sa.Column("activation_expires_at", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("installed_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.Column("superseded_by_activation_id", sa.String(), nullable=True),
        sa.Column("superseded_at", sa.String(), nullable=True),
        sa.Column("activation_sha256", sa.String(64), nullable=False),
        _payload_column(),
        sa.CheckConstraint(
            "desired_state IN ('guarded', 'revoked')",
            name="launchplane_ordinary_agent_activation_desired_state_ck",
        ),
        sa.CheckConstraint(
            "effective_state IN ('qualification_only', 'guarded', 'revoked')",
            name="launchplane_ordinary_agent_activation_effective_state_ck",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="launchplane_ordinary_agent_activation_revision_ck",
        ),
        sa.CheckConstraint(
            "((desired_state = 'guarded' AND effective_state IN "
            "('qualification_only', 'guarded') AND revoked_at IS NULL) OR "
            "(desired_state = 'revoked' AND effective_state = 'revoked' "
            "AND revoked_at IS NOT NULL))",
            name="launchplane_ordinary_agent_activation_state_ck",
        ),
        sa.CheckConstraint(
            "((superseded_by_activation_id IS NULL AND superseded_at IS NULL) OR "
            "(superseded_by_activation_id IS NOT NULL AND superseded_at IS NOT NULL "
            "AND revoked_at IS NULL))",
            name="launchplane_ordinary_agent_activation_supersession_ck",
        ),
        sa.PrimaryKeyConstraint("activation_id"),
    )
    op.create_index(
        "launchplane_ordinary_agent_activation_setup_operation_uidx",
        _ACTIVATIONS,
        ["source_setup_operation_id"],
        unique=True,
    )
    op.create_index(
        "launchplane_ordinary_agent_activation_current_scope_uidx",
        _ACTIVATIONS,
        [
            "repository_id",
            "base_branch",
            "managed_set_id",
            "managed_rule_id",
        ],
        unique=True,
        postgresql_where=sa.text(_CURRENT_SCOPE_PREDICATE),
        sqlite_where=sa.text(_CURRENT_SCOPE_PREDICATE),
    )
    op.create_index(
        "launchplane_ordinary_agent_activation_scope_history_idx",
        _ACTIVATIONS,
        [
            "repository_id",
            "base_branch",
            "managed_set_id",
            "managed_rule_id",
            "installed_at",
        ],
    )

    op.create_table(
        _EVENTS,
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("activation_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("previous_revision", sa.BigInteger(), nullable=False),
        sa.Column("previous_activation_sha256", sa.String(64), nullable=True),
        sa.Column("resulting_revision", sa.BigInteger(), nullable=False),
        sa.Column("resulting_activation_sha256", sa.String(64), nullable=False),
        sa.Column("resulting_desired_state", sa.String(), nullable=False),
        sa.Column("resulting_effective_state", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("source_operation_id", sa.String(), nullable=True),
        _payload_column(),
        sa.CheckConstraint(
            "action IN ('installed', 'guarded_derived', 'readiness_lost', 'revoked', 'superseded')",
            name="launchplane_ordinary_agent_activation_event_action_ck",
        ),
        sa.CheckConstraint(
            "sequence >= 1 AND previous_revision >= 0 AND resulting_revision >= 1",
            name="launchplane_ordinary_agent_activation_event_revision_floor_ck",
        ),
        sa.CheckConstraint(
            "((action = 'installed' AND sequence = 1 AND previous_revision = 0 "
            "AND previous_activation_sha256 IS NULL AND resulting_revision = 1) OR "
            "(action <> 'installed' AND previous_revision >= 1 "
            "AND previous_activation_sha256 IS NOT NULL "
            "AND resulting_revision = previous_revision + 1))",
            name="launchplane_ordinary_agent_activation_event_transition_ck",
        ),
        sa.CheckConstraint(
            "((action IN ('installed', 'revoked', 'superseded') "
            "AND source_operation_id IS NOT NULL) OR "
            "(action IN ('guarded_derived', 'readiness_lost') "
            "AND source_operation_id IS NULL))",
            name="launchplane_ordinary_agent_activation_event_source_ck",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "launchplane_ordinary_agent_activation_event_sequence_uidx",
        _EVENTS,
        ["activation_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "launchplane_agent_activation_event_source_idx",
        _EVENTS,
        ["source_operation_id", "action"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON "
            "launchplane_ordinary_agent_delivery_activation_events FOR EACH ROW "
            "EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
        )


def downgrade() -> None:
    op.drop_table(_EVENTS)
    op.drop_table(_ACTIVATIONS)
