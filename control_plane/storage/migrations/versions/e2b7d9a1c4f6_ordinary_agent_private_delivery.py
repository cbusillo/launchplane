"""Add private ordinary-agent delivery and durable issuer provenance.

Revision ID: e2b7d9a1c4f6
Revises: d9a4c7e2f6b1
"""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "e2b7d9a1c4f6"
down_revision: str | None = "d9a4c7e2f6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "launchplane_ordinary_agent_deliveries" not in tables:
        op.create_table(
            "launchplane_ordinary_agent_deliveries",
            sa.Column("operation_id", sa.String(), primary_key=True),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("credential_id", sa.String(), nullable=False),
            sa.Column("credential_version", sa.BigInteger(), nullable=False),
            sa.Column("credential_digest", sa.String(), nullable=False),
            sa.Column("issuance_evidence_sha256", sa.String(), nullable=False),
            sa.Column("intent_sha256", sa.String(), nullable=False),
            sa.Column("receiver_claim_sha256", sa.String(), nullable=False),
            sa.Column("ciphertext", sa.String(), nullable=True),
            sa.Column("ciphertext_sha256", sa.String(), nullable=False),
            sa.Column("key_id", sa.String(), nullable=False),
            sa.Column("delivery_expires_at", sa.BigInteger(), nullable=False),
            sa.Column("delivery_status", sa.String(), nullable=False),
            sa.UniqueConstraint(
                "credential_id", "credential_version", name="ordinary_agent_delivery_version_uq"
            ),
        )
    if "ordinary_agent_delivery_expiry_idx" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("launchplane_ordinary_agent_deliveries")
    }:
        op.create_index(
            "ordinary_agent_delivery_expiry_idx",
            "launchplane_ordinary_agent_deliveries",
            ["delivery_expires_at"],
        )
    if "launchplane_ordinary_agent_delivery_audits" not in tables:
        op.create_table(
            "launchplane_ordinary_agent_delivery_audits",
            sa.Column("event_id", sa.String(), primary_key=True),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("launchplane_ordinary_agent_delivery_audits")
    op.drop_index(
        "ordinary_agent_delivery_expiry_idx", table_name="launchplane_ordinary_agent_deliveries"
    )
    op.drop_table("launchplane_ordinary_agent_deliveries")
