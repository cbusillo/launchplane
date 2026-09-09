"""Persist approved ordinary sessions, leases and finite request jobs.

Revision ID: f3c8e1a2d5b7
Revises: e2b7d9a1c4f6
"""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f3c8e1a2d5b7"
down_revision: str | None = "e2b7d9a1c4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    if (
        "launchplane_ordinary_agent_session_operations"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_session_operations",
            sa.Column("principal_id", sa.String(), primary_key=True),
            sa.Column("operation_id", sa.String(), primary_key=True),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("intent_sha256", sa.String(), nullable=False),
            sa.Column("approval_sha256", sa.String(), nullable=True),
            sa.Column("administrator_github_id", sa.BigInteger(), nullable=True),
            sa.Column("terminal_session_id", sa.String(), nullable=True),
            sa.Column("payload", payload, nullable=False),
        )
    if "launchplane_ordinary_agent_sessions" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_ordinary_agent_sessions",
            sa.Column("session_id", sa.String(), primary_key=True),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("credential_id", sa.String(), nullable=False),
            sa.Column("credential_version", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint(
                "operation_id",
                "principal_id",
                "credential_id",
                "credential_version",
                name="ordinary_session_operation_uq",
            ),
        )
    if "ix_launchplane_ordinary_agent_sessions_principal_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("launchplane_ordinary_agent_sessions")
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_sessions_principal_id",
            "launchplane_ordinary_agent_sessions",
            ["principal_id"],
        )
    if "launchplane_ordinary_agent_leases" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_ordinary_agent_leases",
            sa.Column("lease_id", sa.String(), primary_key=True),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("revision", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
        )
    if "ix_launchplane_ordinary_agent_leases_session_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("launchplane_ordinary_agent_leases")
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_leases_session_id",
            "launchplane_ordinary_agent_leases",
            ["session_id"],
        )
    if (
        "launchplane_ordinary_agent_finite_requests"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_finite_requests",
            sa.Column("request_id", sa.String(), primary_key=True),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("lease_id", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("intent_sha256", sa.String(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint(
                "principal_id", "idempotency_key", name="ordinary_finite_request_idempotency_uq"
            ),
        )
    if "ix_launchplane_ordinary_agent_finite_requests_session_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(
            "launchplane_ordinary_agent_finite_requests"
        )
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_finite_requests_session_id",
            "launchplane_ordinary_agent_finite_requests",
            ["session_id"],
        )


def downgrade() -> None:
    op.drop_table("launchplane_ordinary_agent_session_operations")
    op.drop_table("launchplane_ordinary_agent_finite_requests")
    op.drop_table("launchplane_ordinary_agent_leases")
    op.drop_table("launchplane_ordinary_agent_sessions")
