"""add repository human admission storage

Revision ID: a0d2f4b6c8e1
Revises: f9c1d3e5a7b9
Create Date: 2026-08-01 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

revision: str = "a0d2f4b6c8e1"
down_revision: str | None = "f9c1d3e5a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_TABLE = "launchplane_repository_human_role_policies"
_WAIVER_TABLE = "launchplane_tenant_technical_human_waiver_events"
_ROLE_REVISION_INDEX = "launchplane_repo_human_role_revision_uidx"
_ROLE_ACTIVE_INDEX = "launchplane_repo_human_role_active_uidx"
_ROLE_CURRENT_INDEX = "launchplane_repo_human_role_current_idx"
_WAIVER_EXACT_HEAD_INDEX = "launchplane_tenant_human_waiver_exact_head_idx"
_WAIVER_BINDING_INDEX = "launchplane_tenant_human_waiver_binding_idx"
_WAIVER_WAIVER_INDEX = "launchplane_tenant_human_waiver_waiver_idx"
_WAIVER_POLICY_INDEX = "launchplane_tenant_human_waiver_policy_idx"


def _json_payload_type() -> TypeEngine[object]:
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _table_exists(_ROLE_TABLE):
        op.create_table(
            _ROLE_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("role_policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("role_policy_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="launchplane_repo_human_role_status_ck",
            ),
            sa.CheckConstraint(
                "role_policy_revision >= 1",
                name="launchplane_repo_human_role_revision_ck",
            ),
            sa.CheckConstraint(
                "(role_policy_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(role_policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_repo_human_role_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _index_exists(_ROLE_TABLE, _ROLE_REVISION_INDEX):
        op.create_index(
            _ROLE_REVISION_INDEX,
            _ROLE_TABLE,
            ["repository_id", "product", "context", "role_policy_revision"],
            unique=True,
        )
    if not _index_exists(_ROLE_TABLE, _ROLE_ACTIVE_INDEX):
        op.create_index(
            _ROLE_ACTIVE_INDEX,
            _ROLE_TABLE,
            ["repository_id", "product", "context"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
    if not _index_exists(_ROLE_TABLE, _ROLE_CURRENT_INDEX):
        op.create_index(
            _ROLE_CURRENT_INDEX,
            _ROLE_TABLE,
            [
                "repository_id",
                "product",
                "context",
                "status",
                sa.text("role_policy_revision DESC"),
            ],
        )

    if not _table_exists(_WAIVER_TABLE):
        op.create_table(
            _WAIVER_TABLE,
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("waiver_id", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("pull_request_number", sa.BigInteger(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("classification_revision", sa.BigInteger(), nullable=False),
            sa.Column("classification_digest", sa.String(), nullable=False),
            sa.Column("role_policy_record_id", sa.String(), nullable=False),
            sa.Column("role_policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("role_policy_digest", sa.String(), nullable=False),
            sa.Column("authz_policy_record_id", sa.String(), nullable=False),
            sa.Column("authz_policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("authz_policy_digest", sa.String(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("author_github_id", sa.BigInteger(), nullable=False),
            sa.Column("author_login", sa.String(), nullable=False),
            sa.Column("managed_set_id", sa.String(), nullable=False),
            sa.Column("managed_rule_id", sa.String(), nullable=False),
            sa.Column("authorized_at", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("source_event_kind", sa.String(), nullable=False),
            sa.Column("source_event_id", sa.String(), nullable=False),
            sa.Column("event_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "action IN ('created', 'revoked')",
                name="launchplane_tenant_human_waiver_action_ck",
            ),
            sa.CheckConstraint(
                "pull_request_number >= 1",
                name="launchplane_tenant_human_waiver_pr_ck",
            ),
            sa.CheckConstraint(
                "classification_revision >= 1",
                name="launchplane_tenant_human_waiver_class_revision_ck",
            ),
            sa.CheckConstraint(
                "role_policy_revision >= 1",
                name="launchplane_tenant_human_waiver_role_revision_ck",
            ),
            sa.CheckConstraint(
                "authz_policy_revision >= 1",
                name="launchplane_tenant_human_waiver_authz_revision_ck",
            ),
            sa.CheckConstraint(
                "author_github_id >= 1",
                name="launchplane_tenant_human_waiver_author_ck",
            ),
            sa.PrimaryKeyConstraint("event_id"),
        )
    if not _index_exists(_WAIVER_TABLE, _WAIVER_EXACT_HEAD_INDEX):
        op.create_index(
            _WAIVER_EXACT_HEAD_INDEX,
            _WAIVER_TABLE,
            [
                "repository_id",
                "pull_request_number",
                "head_sha",
                sa.text("occurred_at DESC"),
                sa.text("event_id DESC"),
            ],
        )
    if not _index_exists(_WAIVER_TABLE, _WAIVER_BINDING_INDEX):
        op.create_index(
            _WAIVER_BINDING_INDEX,
            _WAIVER_TABLE,
            ["binding_sha256", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
        )
    if not _index_exists(_WAIVER_TABLE, _WAIVER_WAIVER_INDEX):
        op.create_index(
            _WAIVER_WAIVER_INDEX,
            _WAIVER_TABLE,
            ["waiver_id", sa.text("occurred_at DESC"), sa.text("event_id DESC")],
        )
    if not _index_exists(_WAIVER_TABLE, _WAIVER_POLICY_INDEX):
        op.create_index(
            _WAIVER_POLICY_INDEX,
            _WAIVER_TABLE,
            ["role_policy_record_id", "authz_policy_record_id", sa.text("occurred_at DESC")],
        )


def downgrade() -> None:
    if _table_exists(_WAIVER_TABLE):
        for index_name in (
            _WAIVER_POLICY_INDEX,
            _WAIVER_WAIVER_INDEX,
            _WAIVER_BINDING_INDEX,
            _WAIVER_EXACT_HEAD_INDEX,
        ):
            if _index_exists(_WAIVER_TABLE, index_name):
                op.drop_index(index_name, table_name=_WAIVER_TABLE)
        op.drop_table(_WAIVER_TABLE)
    if _table_exists(_ROLE_TABLE):
        for index_name in (_ROLE_CURRENT_INDEX, _ROLE_ACTIVE_INDEX, _ROLE_REVISION_INDEX):
            if _index_exists(_ROLE_TABLE, index_name):
                op.drop_index(index_name, table_name=_ROLE_TABLE)
        op.drop_table(_ROLE_TABLE)
