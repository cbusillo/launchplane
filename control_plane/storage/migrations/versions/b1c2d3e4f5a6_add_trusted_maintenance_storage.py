"""add trusted maintenance storage

Revision ID: b1c2d3e4f5a6
Revises: a0d2f4b6c8e1
Create Date: 2026-08-01 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a0d2f4b6c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_TABLE = "launchplane_trusted_maintenance_policies"
_EVIDENCE_TABLE = "launchplane_trusted_maintenance_evidence"
_POLICY_REVISION_INDEX = "launchplane_trusted_maintenance_policy_revision_uidx"
_POLICY_ACTIVE_INDEX = "launchplane_trusted_maintenance_policy_active_uidx"
_POLICY_CURRENT_INDEX = "launchplane_trusted_maintenance_policy_current_idx"
_EVIDENCE_EXACT_HEAD_INDEX = "launchplane_trusted_maintenance_exact_head_idx"
_EVIDENCE_BINDING_INDEX = "launchplane_trusted_maintenance_binding_idx"
_EVIDENCE_POLICY_INDEX = "launchplane_trusted_maintenance_policy_idx"
_EVIDENCE_ACTOR_EVENT_INDEX = "launchplane_trusted_maintenance_actor_event_idx"


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
    if not _table_exists(_POLICY_TABLE):
        op.create_table(
            _POLICY_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("policy_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="launchplane_trusted_maintenance_policy_status_ck",
            ),
            sa.CheckConstraint(
                "policy_revision >= 1",
                name="launchplane_trusted_maintenance_policy_revision_ck",
            ),
            sa.CheckConstraint(
                "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_trusted_maintenance_policy_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _index_exists(_POLICY_TABLE, _POLICY_REVISION_INDEX):
        op.create_index(
            _POLICY_REVISION_INDEX,
            _POLICY_TABLE,
            ["repository_id", "product", "context", "policy_revision"],
            unique=True,
        )
    if not _index_exists(_POLICY_TABLE, _POLICY_ACTIVE_INDEX):
        op.create_index(
            _POLICY_ACTIVE_INDEX,
            _POLICY_TABLE,
            ["repository_id", "product", "context"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
    if not _index_exists(_POLICY_TABLE, _POLICY_CURRENT_INDEX):
        op.create_index(
            _POLICY_CURRENT_INDEX,
            _POLICY_TABLE,
            [
                "repository_id",
                "product",
                "context",
                "status",
                sa.text("policy_revision DESC"),
            ],
        )

    if not _table_exists(_EVIDENCE_TABLE):
        op.create_table(
            _EVIDENCE_TABLE,
            sa.Column("evidence_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("pull_request_number", sa.BigInteger(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("classification_record_id", sa.String(), nullable=False),
            sa.Column("classification_revision", sa.BigInteger(), nullable=False),
            sa.Column("classification_digest", sa.String(), nullable=False),
            sa.Column("policy_record_id", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("policy_digest", sa.String(), nullable=False),
            sa.Column("matched_actor_rule_id", sa.String(), nullable=False),
            sa.Column("pr_author_github_id", sa.BigInteger(), nullable=False),
            sa.Column("pr_author_type", sa.String(), nullable=False),
            sa.Column("pr_author_login", sa.String(), nullable=False),
            sa.Column("sender_github_id", sa.BigInteger(), nullable=False),
            sa.Column("sender_type", sa.String(), nullable=False),
            sa.Column("sender_login", sa.String(), nullable=False),
            sa.Column("head_repository_id", sa.String(), nullable=False),
            sa.Column("head_repository_owner_id", sa.String(), nullable=False),
            sa.Column("head_repository", sa.String(), nullable=False),
            sa.Column("event_name", sa.String(), nullable=False),
            sa.Column("event_action", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("delivery_id", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("evidence_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "pull_request_number >= 1",
                name="launchplane_trusted_maintenance_evidence_pr_ck",
            ),
            sa.CheckConstraint(
                "classification_revision >= 1",
                name="launchplane_trusted_maintenance_evidence_class_revision_ck",
            ),
            sa.CheckConstraint(
                "policy_revision >= 1",
                name="launchplane_trusted_maintenance_evidence_policy_revision_ck",
            ),
            sa.CheckConstraint(
                "pr_author_github_id >= 1",
                name="launchplane_trusted_maintenance_evidence_author_ck",
            ),
            sa.CheckConstraint(
                "sender_github_id >= 1",
                name="launchplane_trusted_maintenance_evidence_sender_ck",
            ),
            sa.CheckConstraint(
                "head_repository_id = repository_id AND "
                "head_repository_owner_id = repository_owner_id AND "
                "head_repository = repository",
                name="launchplane_trusted_maintenance_evidence_same_head_repo_ck",
            ),
            sa.PrimaryKeyConstraint("evidence_id"),
        )
    if not _index_exists(_EVIDENCE_TABLE, _EVIDENCE_EXACT_HEAD_INDEX):
        op.create_index(
            _EVIDENCE_EXACT_HEAD_INDEX,
            _EVIDENCE_TABLE,
            [
                "repository_id",
                "pull_request_number",
                "head_sha",
                sa.text("occurred_at DESC"),
                sa.text("evidence_id DESC"),
            ],
        )
    if not _index_exists(_EVIDENCE_TABLE, _EVIDENCE_BINDING_INDEX):
        op.create_index(
            _EVIDENCE_BINDING_INDEX,
            _EVIDENCE_TABLE,
            ["binding_sha256", sa.text("occurred_at DESC"), sa.text("evidence_id DESC")],
        )
    if not _index_exists(_EVIDENCE_TABLE, _EVIDENCE_POLICY_INDEX):
        op.create_index(
            _EVIDENCE_POLICY_INDEX,
            _EVIDENCE_TABLE,
            ["policy_record_id", "classification_digest", sa.text("occurred_at DESC")],
        )
    if not _index_exists(_EVIDENCE_TABLE, _EVIDENCE_ACTOR_EVENT_INDEX):
        op.create_index(
            _EVIDENCE_ACTOR_EVENT_INDEX,
            _EVIDENCE_TABLE,
            [
                "pr_author_github_id",
                "sender_github_id",
                "event_name",
                "event_action",
                sa.text("occurred_at DESC"),
            ],
        )


def downgrade() -> None:
    if _table_exists(_EVIDENCE_TABLE):
        for index_name in (
            _EVIDENCE_ACTOR_EVENT_INDEX,
            _EVIDENCE_POLICY_INDEX,
            _EVIDENCE_BINDING_INDEX,
            _EVIDENCE_EXACT_HEAD_INDEX,
        ):
            if _index_exists(_EVIDENCE_TABLE, index_name):
                op.drop_index(index_name, table_name=_EVIDENCE_TABLE)
        op.drop_table(_EVIDENCE_TABLE)
    if _table_exists(_POLICY_TABLE):
        for index_name in (_POLICY_CURRENT_INDEX, _POLICY_ACTIVE_INDEX, _POLICY_REVISION_INDEX):
            if _index_exists(_POLICY_TABLE, index_name):
                op.drop_index(index_name, table_name=_POLICY_TABLE)
        op.drop_table(_POLICY_TABLE)
