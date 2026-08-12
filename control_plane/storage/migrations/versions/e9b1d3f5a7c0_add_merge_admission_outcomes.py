"""Add guarded merge admission and landing outcome records.

Revision ID: e9b1d3f5a7c0
Revises: d8a0c2e4f6b8
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ColumnElement, TextClause


revision: str = "e9b1d3f5a7c0"
down_revision: str | None = "d8a0c2e4f6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMISSIONS_TABLE = "launchplane_merge_admissions"
_OUTCOMES_TABLE = "launchplane_merge_landing_outcomes"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _table_exists(_ADMISSIONS_TABLE):
        op.create_table(
            _ADMISSIONS_TABLE,
            sa.Column("admission_id", sa.String(), nullable=False),
            sa.Column("admission_binding_sha256", sa.String(length=64), nullable=False),
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("attempt_sequence", sa.Integer(), nullable=False),
            sa.Column("decision", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("base_branch", sa.String(), nullable=False),
            sa.Column("pull_request_number", sa.Integer(), nullable=False),
            sa.Column("queue_position", sa.Integer(), nullable=False),
            sa.Column("landing_plan_record_id", sa.String(), nullable=False),
            sa.Column("landing_plan_id", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.CheckConstraint(
                "pull_request_number > 0 AND queue_position > 0 AND attempt_sequence > 0",
                name="launchplane_merge_admissions_positive_values_check",
            ),
            sa.CheckConstraint(
                "decision = 'admitted'",
                name="launchplane_merge_admissions_decision_check",
            ),
            sa.PrimaryKeyConstraint("admission_id"),
        )
    admission_indexes: tuple[
        tuple[str, Sequence[str | TextClause | ColumnElement[object]], bool], ...
    ] = (
        (
            "launchplane_merge_admissions_attempt_uidx",
            ["attempt_id"],
            True,
        ),
        (
            "launchplane_merge_admissions_binding_uidx",
            ["admission_binding_sha256"],
            True,
        ),
        (
            "launchplane_merge_admissions_target_idx",
            ["repository", "base_branch", "pull_request_number", sa.text("created_at DESC")],
            False,
        ),
        (
            "launchplane_merge_admissions_plan_idx",
            ["landing_plan_id", "queue_position", sa.text("created_at DESC")],
            False,
        ),
    )
    for index_name, columns, unique in admission_indexes:
        if not _index_exists(_ADMISSIONS_TABLE, index_name):
            op.create_index(
                index_name,
                _ADMISSIONS_TABLE,
                columns,
                unique=unique,
            )

    if not _table_exists(_OUTCOMES_TABLE):
        op.create_table(
            _OUTCOMES_TABLE,
            sa.Column("outcome_id", sa.String(), nullable=False),
            sa.Column("outcome_binding_sha256", sa.String(length=64), nullable=False),
            sa.Column("admission_id", sa.String(), nullable=False),
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("observation_sequence", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("base_branch", sa.String(), nullable=False),
            sa.Column("pull_request_number", sa.Integer(), nullable=False),
            sa.Column("observed_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.CheckConstraint(
                "pull_request_number > 0 AND observation_sequence > 0",
                name="launchplane_merge_landing_outcomes_positive_values_check",
            ),
            sa.CheckConstraint(
                "status IN ('landed', 'rejected', 'reconcile_required')",
                name="launchplane_merge_landing_outcomes_status_check",
            ),
            sa.PrimaryKeyConstraint("outcome_id"),
        )
    outcome_indexes: tuple[
        tuple[str, Sequence[str | TextClause | ColumnElement[object]], bool], ...
    ] = (
        (
            "launchplane_merge_landing_outcomes_observation_uidx",
            ["admission_id", "observation_sequence"],
            True,
        ),
        (
            "launchplane_merge_landing_outcomes_binding_uidx",
            ["outcome_binding_sha256"],
            True,
        ),
        (
            "launchplane_merge_landing_outcomes_target_idx",
            ["repository", "base_branch", "pull_request_number", sa.text("observed_at DESC")],
            False,
        ),
        (
            "launchplane_merge_landing_outcomes_status_idx",
            ["status", sa.text("observed_at DESC")],
            False,
        ),
    )
    for index_name, columns, unique in outcome_indexes:
        if not _index_exists(_OUTCOMES_TABLE, index_name):
            op.create_index(
                index_name,
                _OUTCOMES_TABLE,
                columns,
                unique=unique,
            )


def downgrade() -> None:
    if _table_exists(_OUTCOMES_TABLE):
        op.drop_table(_OUTCOMES_TABLE)
    if _table_exists(_ADMISSIONS_TABLE):
        op.drop_table(_ADMISSIONS_TABLE)
