"""Add inert administrator enrollment records.

Revision ID: c4e6a8b0d2f5
Revises: a7c9e1f3b5d7
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4e6a8b0d2f5"
down_revision: str | None = "a7c9e1f3b5d7"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE = "launchplane_administrator_enrollments"
_INDEX = "launchplane_administrator_enrollment_state_expiry_idx"


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
            sa.Column("enrollment_id", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("proposer_github_id", sa.BigInteger(), nullable=False),
            sa.Column("candidate_github_id", sa.BigInteger(), nullable=True),
            sa.Column("challenge_sha256", sa.String(), nullable=False),
            sa.Column("reason", sa.String(), nullable=False),
            sa.Column("provenance_sha256", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("control_proven_at", sa.String(), nullable=True),
            sa.Column("withdrawn_at", sa.String(), nullable=True),
            sa.Column("expired_at", sa.String(), nullable=True),
            sa.Column("enrolled_at", sa.String(), nullable=True),
            sa.Column("enrolled_policy_record_id", sa.String(), nullable=True),
            sa.Column("enrolled_policy_revision", sa.BigInteger(), nullable=True),
            sa.Column("enrolled_policy_sha256", sa.String(), nullable=True),
            sa.Column("reviewed_plan_sha256", sa.String(), nullable=True),
            sa.Column("bridge_idempotency_key_sha256", sa.String(), nullable=True),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            sa.Column(
                "authorizes_policy",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "policy_bridge_state",
                sa.String(),
                nullable=False,
                server_default="not_applied",
            ),
            _payload_column(),
            sa.CheckConstraint(
                "state IN ('issued', 'control_proven', 'withdrawn', 'expired', 'enrolled')",
                name="launchplane_administrator_enrollment_state_ck",
            ),
            sa.CheckConstraint(
                "proposer_github_id > 0 AND "
                "(candidate_github_id IS NULL OR candidate_github_id > 0)",
                name="launchplane_administrator_enrollment_github_id_ck",
            ),
            sa.CheckConstraint(
                "expires_at > created_at",
                name="launchplane_administrator_enrollment_expiry_ck",
            ),
            sa.CheckConstraint(
                "strftime('%s', created_at) IS NOT NULL "
                "AND strftime('%s', expires_at) IS NOT NULL "
                "AND CAST(strftime('%s', expires_at) AS INTEGER) "
                "- CAST(strftime('%s', created_at) AS INTEGER) = 1800",
                name="lp_admin_enrollment_ttl_sqlite_ck",
            ).ddl_if(dialect="sqlite"),
            sa.CheckConstraint(
                "CAST(expires_at AS timestamptz) - CAST(created_at AS timestamptz) "
                "= INTERVAL '30 minutes'",
                name="lp_admin_enrollment_ttl_pg_ck",
            ).ddl_if(dialect="postgresql"),
            sa.CheckConstraint(
                "candidate_github_id IS NULL OR candidate_github_id <> proposer_github_id",
                name="launchplane_administrator_enrollment_distinct_human_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert' AND authorizes_policy = false",
                name="launchplane_administrator_enrollment_no_authority_ck",
            ),
            sa.CheckConstraint(
                "policy_bridge_state IN ('not_applied', 'applied')",
                name="launchplane_administrator_enrollment_bridge_state_ck",
            ),
            sa.CheckConstraint(
                "(candidate_github_id IS NULL AND control_proven_at IS NULL) OR "
                "(candidate_github_id IS NOT NULL AND control_proven_at IS NOT NULL)",
                name="launchplane_administrator_enrollment_control_proof_ck",
            ),
            sa.CheckConstraint(
                "control_proven_at IS NULL OR "
                "(control_proven_at >= created_at AND control_proven_at < expires_at)",
                name="launchplane_administrator_enrollment_control_time_ck",
            ),
            sa.CheckConstraint(
                "withdrawn_at IS NULL OR "
                "(withdrawn_at >= COALESCE(control_proven_at, created_at) "
                "AND withdrawn_at < expires_at)",
                name="launchplane_administrator_enrollment_withdrawal_time_ck",
            ),
            sa.CheckConstraint(
                "expired_at IS NULL OR expired_at >= expires_at",
                name="launchplane_administrator_enrollment_expired_time_ck",
            ),
            sa.CheckConstraint(
                "enrolled_at IS NULL OR "
                "(control_proven_at IS NOT NULL AND enrolled_at >= control_proven_at "
                "AND enrolled_at < expires_at)",
                name="launchplane_administrator_enrollment_enrolled_time_ck",
            ),
            sa.CheckConstraint(
                "(enrolled_policy_record_id IS NULL AND enrolled_policy_revision IS NULL "
                "AND enrolled_policy_sha256 IS NULL AND reviewed_plan_sha256 IS NULL "
                "AND bridge_idempotency_key_sha256 IS NULL) OR "
                "(enrolled_policy_record_id IS NOT NULL AND enrolled_policy_record_id <> '' "
                "AND enrolled_policy_revision > 0 AND enrolled_policy_sha256 IS NOT NULL "
                "AND reviewed_plan_sha256 IS NOT NULL "
                "AND bridge_idempotency_key_sha256 IS NOT NULL)",
                name="launchplane_administrator_enrollment_policy_evidence_ck",
            ),
            sa.CheckConstraint(
                "state <> 'enrolled' OR enrolled_policy_record_id = "
                "'launchplane-authz-policy-r' || printf('%020d', enrolled_policy_revision) "
                "|| '-' || substr(enrolled_policy_sha256, 1, 12)",
                name="lp_admin_enrollment_policy_record_sqlite_ck",
            ).ddl_if(dialect="sqlite"),
            sa.CheckConstraint(
                "state <> 'enrolled' OR enrolled_policy_record_id = "
                "'launchplane-authz-policy-r' "
                "|| lpad(CAST(enrolled_policy_revision AS text), 20, '0') "
                "|| '-' || substr(enrolled_policy_sha256, 1, 12)",
                name="lp_admin_enrollment_policy_record_pg_ck",
            ).ddl_if(dialect="postgresql"),
            sa.CheckConstraint(
                "(state = 'issued' AND candidate_github_id IS NULL AND control_proven_at IS NULL "
                "AND withdrawn_at IS NULL AND expired_at IS NULL AND enrolled_at IS NULL "
                "AND enrolled_policy_record_id IS NULL AND policy_bridge_state = 'not_applied') OR "
                "(state = 'control_proven' AND candidate_github_id IS NOT NULL "
                "AND control_proven_at IS NOT NULL AND withdrawn_at IS NULL "
                "AND expired_at IS NULL AND enrolled_at IS NULL "
                "AND enrolled_policy_record_id IS NULL AND policy_bridge_state = 'not_applied') OR "
                "(state = 'withdrawn' AND withdrawn_at IS NOT NULL AND expired_at IS NULL "
                "AND enrolled_at IS NULL AND enrolled_policy_record_id IS NULL "
                "AND policy_bridge_state = 'not_applied') OR "
                "(state = 'expired' AND expired_at IS NOT NULL AND withdrawn_at IS NULL "
                "AND enrolled_at IS NULL AND enrolled_policy_record_id IS NULL "
                "AND policy_bridge_state = 'not_applied') OR "
                "(state = 'enrolled' AND candidate_github_id IS NOT NULL "
                "AND control_proven_at IS NOT NULL AND enrolled_at IS NOT NULL "
                "AND withdrawn_at IS NULL AND expired_at IS NULL "
                "AND enrolled_policy_record_id IS NOT NULL AND policy_bridge_state = 'applied')",
                name="launchplane_administrator_enrollment_terminal_ck",
            ),
            sa.PrimaryKeyConstraint("enrollment_id"),
            sa.UniqueConstraint(
                "challenge_sha256",
                name="launchplane_administrator_enrollment_challenge_uq",
            ),
        )
    if not _index_exists():
        op.create_index(_INDEX, _TABLE, ["state", "expires_at"])


def downgrade() -> None:
    if _table_has_rows():
        raise RuntimeError("Cannot downgrade administrator enrollment storage while records exist.")
    if _index_exists():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _table_exists():
        op.drop_table(_TABLE)
