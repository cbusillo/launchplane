"""add runner lane registration audits

Revision ID: c7f9a1b3d5e7
Revises: c6e8f0a2b4d6
Create Date: 2026-06-08 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c7f9a1b3d5e7"
down_revision: str | None = "c6e8f0a2b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_runner_lane_registration_audits"
_REPOSITORY_INDEX = "launchplane_runner_lane_registration_audits_repo_idx"
_HOST_INDEX = "launchplane_runner_lane_registration_audits_host_idx"
_STATUS_INDEX = "launchplane_runner_lane_registration_audits_status_idx"


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("audit_record_key", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("host_name", sa.String(), nullable=False),
            sa.Column("lane_name", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("mutate", sa.Integer(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("audit_record_key"),
        )
    if not _index_exists(_TABLE, _REPOSITORY_INDEX):
        op.create_index(
            _REPOSITORY_INDEX,
            _TABLE,
            ["repository", sa.text("audit_record_key DESC")],
        )
    if not _index_exists(_TABLE, _HOST_INDEX):
        op.create_index(
            _HOST_INDEX,
            _TABLE,
            ["host_name", sa.text("audit_record_key DESC")],
        )
    if not _index_exists(_TABLE, _STATUS_INDEX):
        op.create_index(
            _STATUS_INDEX,
            _TABLE,
            ["status", sa.text("audit_record_key DESC")],
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _STATUS_INDEX):
        op.drop_index(_STATUS_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _HOST_INDEX):
        op.drop_index(_HOST_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _REPOSITORY_INDEX):
        op.drop_index(_REPOSITORY_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
