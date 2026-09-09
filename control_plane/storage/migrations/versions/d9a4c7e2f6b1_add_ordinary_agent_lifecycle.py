"""add authoritative ordinary-agent lifecycle records

Revision ID: d9a4c7e2f6b1
Revises: f3a5b7c9d1e4
Create Date: 2026-09-08 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d9a4c7e2f6b1"
down_revision: str | None = "f3a5b7c9d1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRINCIPALS = "launchplane_ordinary_agent_principals"
_CREDENTIALS = "launchplane_ordinary_agent_authentication_credentials"
_CUSTODY = "launchplane_ordinary_agent_credential_custody"
_AUDITS = "launchplane_ordinary_agent_lifecycle_audits"


def _payload_column() -> sa.Column[dict[str, object]]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if _PRINCIPALS not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            _PRINCIPALS,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("principal_revision", sa.BigInteger(), nullable=False),
            sa.Column("lifecycle_status", sa.String(), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False),
            sa.Column("credential_id", sa.String(), nullable=False),
            sa.Column("credential_version", sa.BigInteger(), nullable=False),
            sa.Column("credential_digest", sa.String(), nullable=False),
            sa.Column("custody_record_id", sa.String(), nullable=False),
            sa.Column("custody_sha256", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column("record_sha256", sa.String(), nullable=False),
            _payload_column(),
            sa.CheckConstraint(
                "lifecycle_status IN ('active', 'revoked')",
                name="launchplane_ordinary_agent_principal_status_ck",
            ),
            sa.CheckConstraint(
                "principal_revision >= 1",
                name="launchplane_ordinary_agent_principal_revision_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if "launchplane_ordinary_agent_principal_revision_uidx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_PRINCIPALS)
    }:
        op.create_index(
            "launchplane_ordinary_agent_principal_revision_uidx",
            _PRINCIPALS,
            ["principal_id", "principal_revision"],
            unique=True,
        )
    if "launchplane_ordinary_agent_principal_current_uidx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_PRINCIPALS)
    }:
        op.create_index(
            "launchplane_ordinary_agent_principal_current_uidx",
            _PRINCIPALS,
            ["principal_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
            postgresql_where=sa.text("is_current"),
        )

    if _CREDENTIALS not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            _CREDENTIALS,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("credential_id", sa.String(), nullable=False),
            sa.Column("credential_version", sa.BigInteger(), nullable=False),
            sa.Column("lifecycle_status", sa.String(), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False),
            sa.Column("credential_digest", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column("record_sha256", sa.String(), nullable=False),
            _payload_column(),
            sa.CheckConstraint(
                "lifecycle_status IN ('active', 'superseded', 'revoked')",
                name="launchplane_ordinary_agent_auth_credential_status_ck",
            ),
            sa.CheckConstraint(
                "credential_version >= 1",
                name="launchplane_ordinary_agent_auth_credential_version_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if "launchplane_ordinary_agent_auth_credential_version_uidx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_CREDENTIALS)
    }:
        op.create_index(
            "launchplane_ordinary_agent_auth_credential_version_uidx",
            _CREDENTIALS,
            ["credential_id", "credential_version"],
            unique=True,
        )
    if "launchplane_ordinary_agent_auth_credential_current_uidx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_CREDENTIALS)
    }:
        op.create_index(
            "launchplane_ordinary_agent_auth_credential_current_uidx",
            _CREDENTIALS,
            ["principal_id"],
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
            postgresql_where=sa.text("is_current"),
        )

    if _CUSTODY not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            _CUSTODY,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("credential_id", sa.String(), nullable=False),
            sa.Column("credential_version", sa.BigInteger(), nullable=False),
            sa.Column("predecessor_record_id", sa.String(), nullable=True),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column("custody_sha256", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if "launchplane_ordinary_agent_custody_credential_version_uidx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_CUSTODY)
    }:
        op.create_index(
            "launchplane_ordinary_agent_custody_credential_version_uidx",
            _CUSTODY,
            ["credential_id", "credential_version"],
            unique=True,
        )
    if "launchplane_ordinary_agent_custody_principal_idx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_CUSTODY)
    }:
        op.create_index(
            "launchplane_ordinary_agent_custody_principal_idx",
            _CUSTODY,
            ["principal_id", "credential_version"],
        )

    if _AUDITS not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            _AUDITS,
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("principal_id", sa.String(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("audit_sha256", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("event_id"),
        )
    if "launchplane_ordinary_agent_lifecycle_audit_operation_uidx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_AUDITS)
    }:
        op.create_index(
            "launchplane_ordinary_agent_lifecycle_audit_operation_uidx",
            _AUDITS,
            ["operation_id"],
            unique=True,
        )
    if "launchplane_ordinary_agent_lifecycle_audit_principal_idx" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_AUDITS)
    }:
        op.create_index(
            "launchplane_ordinary_agent_lifecycle_audit_principal_idx",
            _AUDITS,
            ["principal_id", "occurred_at"],
        )


def downgrade() -> None:
    for table_name in (_AUDITS, _CUSTODY, _CREDENTIALS, _PRINCIPALS):
        op.drop_table(table_name)
