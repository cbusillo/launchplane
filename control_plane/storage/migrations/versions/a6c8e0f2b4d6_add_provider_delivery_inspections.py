"""Add provider-delivery inspection generations, custody and receipts.

Revision ID: a6c8e0f2b4d6
Revises: a3c5e7f9b1d4
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a6c8e0f2b4d6"
down_revision: str | None = "a3c5e7f9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_provider_delivery_inspections"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    payload_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        _TABLE,
        sa.Column("attempt_id", sa.String(), primary_key=True),
        sa.Column("demand_id", sa.String(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("provider_attempt_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("action_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("profile_sha256", sa.String(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("inspection_phase", sa.String(), nullable=False),
        sa.Column("custody_phase", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("inspection_started_at", sa.BigInteger(), nullable=False),
        sa.Column("dispatch_deadline", sa.BigInteger(), nullable=False),
        sa.Column("publication_deadline", sa.BigInteger(), nullable=False),
        sa.Column("token_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("next_retry_not_before", sa.BigInteger(), nullable=True),
        sa.Column("terminal_class", sa.String(), nullable=True),
        sa.Column("terminal_status", sa.String(), nullable=True),
        sa.Column("repository_completion_sequence", sa.BigInteger(), nullable=True),
        sa.Column("receipt_id", sa.String(), nullable=True),
        sa.Column("payload", payload_type, nullable=False),
        sa.CheckConstraint("generation > 0", name="provider_delivery_generation_ck"),
        sa.CheckConstraint(
            "provider_attempt_ordinal BETWEEN 1 AND 3",
            name="provider_delivery_attempt_ordinal_ck",
        ),
        sa.CheckConstraint("action_ordinal > 0", name="provider_delivery_action_ordinal_ck"),
        sa.CheckConstraint("repository_id > 0", name="provider_delivery_repository_id_ck"),
        sa.CheckConstraint("revision > 0", name="provider_delivery_revision_ck"),
        sa.CheckConstraint(
            "inspection_phase IN ('active', 'terminal')",
            name="provider_delivery_inspection_phase_ck",
        ),
        sa.CheckConstraint(
            "custody_phase IN ('reserved', 'minting', 'issued', 'issue_unknown', "
            "'cleanup_unknown', 'closed')",
            name="provider_delivery_custody_phase_ck",
        ),
        sa.CheckConstraint(
            "dispatch_deadline = inspection_started_at + 45 AND "
            "publication_deadline = inspection_started_at + 60",
            name="provider_delivery_deadline_ck",
        ),
        sa.CheckConstraint(
            "(inspection_phase = 'active' AND terminal_class IS NULL "
            "AND terminal_status IS NULL AND repository_completion_sequence IS NULL "
            "AND receipt_id IS NULL) OR "
            "(inspection_phase = 'terminal' AND custody_phase = 'closed' "
            "AND terminal_class IS NOT NULL AND terminal_status IS NOT NULL "
            "AND repository_completion_sequence IS NOT NULL)",
            name="provider_delivery_terminal_ck",
        ),
        sa.UniqueConstraint(
            "demand_id",
            "generation",
            "provider_attempt_ordinal",
            name="provider_delivery_attempt_uq",
        ),
        sa.UniqueConstraint(
            "repository_id",
            "repository_completion_sequence",
            name="provider_delivery_completion_sequence_uq",
        ),
        sa.UniqueConstraint("receipt_id", name="provider_delivery_receipt_uq"),
    )
    op.create_index(
        "provider_delivery_generation_charge_uq",
        _TABLE,
        ["demand_id", "generation"],
        unique=True,
        postgresql_where=sa.text("provider_attempt_ordinal = 1"),
        sqlite_where=sa.text("provider_attempt_ordinal = 1"),
    )
    op.create_index(
        "provider_delivery_repository_flight_uq",
        _TABLE,
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text("inspection_phase = 'active'"),
        sqlite_where=sa.text("inspection_phase = 'active'"),
    )
    op.create_index(
        "provider_delivery_repository_custody_uq",
        _TABLE,
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text(
            "custody_phase IN ('minting', 'issued', 'issue_unknown', 'cleanup_unknown')"
        ),
        sqlite_where=sa.text(
            "custody_phase IN ('minting', 'issued', 'issue_unknown', 'cleanup_unknown')"
        ),
    )
    op.create_index(
        "provider_delivery_target_observation_idx",
        _TABLE,
        ["repository_id", "base_branch", "repository_completion_sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
