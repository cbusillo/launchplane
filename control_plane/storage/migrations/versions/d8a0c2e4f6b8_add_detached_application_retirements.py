"""Add detached application retirement records.

Revision ID: d8a0c2e4f6b8
Revises: c6e8f1b3d5a7
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d8a0c2e4f6b8"
down_revision: str | None = "c6e8f1b3d5a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_detached_application_retirements"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    if not _table_exists():
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("plan_record_id", sa.String(), nullable=False),
            sa.Column("candidate_target_sha256", sa.String(length=64), nullable=False),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("outcome", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column("protected_target_count", sa.Integer(), nullable=False),
            sa.Column(
                "authority_write_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.CheckConstraint(
                "authority_write_count = 0",
                name="launchplane_detached_app_retirements_no_authority_writes",
            ),
            sa.CheckConstraint(
                "protected_target_count > 0",
                name="launchplane_detached_app_retirements_protected_nonempty",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    for index_name, first_column in (
        ("launchplane_detached_app_retirements_candidate_idx", "candidate_target_sha256"),
        ("launchplane_detached_app_retirements_plan_idx", "plan_record_id"),
        ("launchplane_detached_app_retirements_idempotency_idx", "idempotency_key"),
    ):
        if not _index_exists(index_name):
            op.create_index(
                index_name,
                _TABLE,
                [first_column, sa.text("recorded_at DESC")],
            )
    if not _index_exists("launchplane_detached_app_retirements_plan_idempotency_unique"):
        op.create_index(
            "launchplane_detached_app_retirements_plan_idempotency_unique",
            _TABLE,
            ["candidate_target_sha256", "actor", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("mode = 'plan'"),
            sqlite_where=sa.text("mode = 'plan'"),
        )


def downgrade() -> None:
    if not _table_exists():
        return
    op.drop_table(_TABLE)
