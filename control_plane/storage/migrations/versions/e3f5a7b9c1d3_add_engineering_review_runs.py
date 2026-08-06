"""add engineering review runs

Revision ID: e3f5a7b9c1d3
Revises: d2e4f6a8b0c2
Create Date: 2026-08-05 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e3f5a7b9c1d3"
down_revision: str | None = "d2e4f6a8b0c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTHORITY_TABLE = "launchplane_engineering_review_authorities"
_TABLE = "launchplane_engineering_review_runs"
_AUTHORITY_REVISION_UIDX = "launchplane_eng_review_authority_revision_uidx"
_AUTHORITY_ACTIVE_UIDX = "launchplane_eng_review_authority_active_uidx"
_PR_IDX = "launchplane_eng_review_runs_pr_idx"
_WORK_REQUEST_IDX = "launchplane_eng_review_runs_work_request_idx"
_STATE_LEASE_IDX = "launchplane_eng_review_runs_state_lease_idx"
_WORKER_CLAIM_IDX = "launchplane_eng_review_runs_worker_claim_idx"
_ASSIGNMENT_UIDX = "launchplane_eng_review_runs_assignment_uidx"
_CREDENTIAL_UIDX = "launchplane_eng_review_runs_credential_uidx"


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(idx["name"]) for idx in inspector.get_indexes(table_name)}


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if not _table_exists(_AUTHORITY_TABLE):
        op.create_table(
            _AUTHORITY_TABLE,
            sa.Column("authority_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("authority_digest", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            _payload_column(),
            sa.CheckConstraint(
                "status IN ('active', 'retired')",
                name="launchplane_eng_review_authority_status_ck",
            ),
            sa.CheckConstraint(
                "policy_revision >= 1",
                name="launchplane_eng_review_authority_revision_ck",
            ),
            sa.PrimaryKeyConstraint("authority_id"),
        )
    if not _index_exists(_AUTHORITY_TABLE, _AUTHORITY_REVISION_UIDX):
        op.create_index(
            _AUTHORITY_REVISION_UIDX,
            _AUTHORITY_TABLE,
            ["repository", "policy_revision"],
            unique=True,
        )
    if not _index_exists(_AUTHORITY_TABLE, _AUTHORITY_ACTIVE_UIDX):
        op.create_index(
            _AUTHORITY_ACTIVE_UIDX,
            _AUTHORITY_TABLE,
            ["repository"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("assignment_fingerprint", sa.String(), nullable=False),
            sa.Column("review_slot", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("authority_id", sa.String(), nullable=False),
            sa.Column("authority_digest", sa.String(), nullable=False),
            sa.Column("work_request_id", sa.String(), nullable=False),
            sa.Column("work_request_lifecycle_id", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.Integer(), nullable=False),
            sa.Column("worker_runtime_id", sa.String(), nullable=False),
            sa.Column("worker_host", sa.String(), nullable=False),
            sa.Column("credential_hash", sa.String(), nullable=False),
            sa.Column("lease_expires_at", sa.String(), nullable=False, server_default=""),
            sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("run_id"),
        )
    if not _index_exists(_TABLE, _PR_IDX):
        op.create_index(
            _PR_IDX,
            _TABLE,
            ["repository", "pr_number", sa.text("created_at DESC")],
        )
    if not _index_exists(_TABLE, _WORK_REQUEST_IDX):
        op.create_index(
            _WORK_REQUEST_IDX,
            _TABLE,
            ["work_request_id", sa.text("created_at DESC")],
        )
    if not _index_exists(_TABLE, _STATE_LEASE_IDX):
        op.create_index(
            _STATE_LEASE_IDX,
            _TABLE,
            ["state", "lease_expires_at"],
        )
    if not _index_exists(_TABLE, _WORKER_CLAIM_IDX):
        op.create_index(
            _WORKER_CLAIM_IDX,
            _TABLE,
            ["worker_host", "worker_runtime_id", "state", "created_at"],
        )
    if not _index_exists(_TABLE, _ASSIGNMENT_UIDX):
        op.create_index(
            _ASSIGNMENT_UIDX,
            _TABLE,
            ["assignment_fingerprint"],
            unique=True,
        )
    if not _index_exists(_TABLE, _CREDENTIAL_UIDX):
        op.create_index(
            _CREDENTIAL_UIDX,
            _TABLE,
            ["credential_hash"],
            unique=True,
        )


def downgrade() -> None:
    if _table_exists(_TABLE):
        if _index_exists(_TABLE, _CREDENTIAL_UIDX):
            op.drop_index(_CREDENTIAL_UIDX, table_name=_TABLE)
        if _index_exists(_TABLE, _ASSIGNMENT_UIDX):
            op.drop_index(_ASSIGNMENT_UIDX, table_name=_TABLE)
        if _index_exists(_TABLE, _WORKER_CLAIM_IDX):
            op.drop_index(_WORKER_CLAIM_IDX, table_name=_TABLE)
        if _index_exists(_TABLE, _STATE_LEASE_IDX):
            op.drop_index(_STATE_LEASE_IDX, table_name=_TABLE)
        if _index_exists(_TABLE, _WORK_REQUEST_IDX):
            op.drop_index(_WORK_REQUEST_IDX, table_name=_TABLE)
        if _index_exists(_TABLE, _PR_IDX):
            op.drop_index(_PR_IDX, table_name=_TABLE)
        op.drop_table(_TABLE)
    if _table_exists(_AUTHORITY_TABLE):
        if _index_exists(_AUTHORITY_TABLE, _AUTHORITY_ACTIVE_UIDX):
            op.drop_index(_AUTHORITY_ACTIVE_UIDX, table_name=_AUTHORITY_TABLE)
        if _index_exists(_AUTHORITY_TABLE, _AUTHORITY_REVISION_UIDX):
            op.drop_index(_AUTHORITY_REVISION_UIDX, table_name=_AUTHORITY_TABLE)
        op.drop_table(_AUTHORITY_TABLE)
