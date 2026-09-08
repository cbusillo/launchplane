"""Add immutable Every Code feedback-resume evidence storage.

Revision ID: e2f4a6b8c0d3
Revises: d1f3a5b7c9e2
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem
from sqlalchemy.types import TypeEngine


revision: str = "e2f4a6b8c0d3"
down_revision: str | None = "d1f3a5b7c9e2"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _payload() -> TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _create_table_if_missing(table_name: str, *elements: SchemaItem) -> None:
    if table_name in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(table_name, *elements)


def _create_index_if_missing(
    index_name: str,
    table_name: str,
    columns: list[str],
) -> None:
    indexes = sa.inspect(op.get_bind()).get_indexes(table_name)
    if index_name in {str(index["name"]) for index in indexes}:
        return
    op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    _create_table_if_missing(
        "launchplane_every_code_feedback_acceptances",
        sa.Column("acceptance_id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.String(), nullable=False),
        sa.Column("feedback_kind", sa.String(), nullable=False),
        sa.Column("feedback_object_id", sa.String(), nullable=False),
        sa.Column("revision_digest", sa.String(), nullable=False),
        sa.Column("acceptance_digest", sa.String(), nullable=False),
        sa.Column("provider_updated_at", sa.String(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
        sa.Column("eligible_until", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", _payload(), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"], ["launchplane_every_code_work_requests.request_id"]
        ),
        sa.CheckConstraint(
            "repository_id > 0", name="launchplane_every_code_feedback_acceptance_repository_id_ck"
        ),
        sa.UniqueConstraint(
            "repository_id",
            "feedback_kind",
            "feedback_object_id",
            "revision_digest",
            name="launchplane_every_code_feedback_acceptance_revision_uidx",
        ),
        sa.UniqueConstraint(
            "acceptance_digest", name="launchplane_every_code_feedback_acceptance_digest_uidx"
        ),
        sa.UniqueConstraint(
            "repository_id",
            "feedback_kind",
            "feedback_object_id",
            "provider_updated_at",
            name="launchplane_every_code_feedback_acceptance_revision_time_uidx",
        ),
    )
    _create_index_if_missing(
        "launchplane_every_code_feedback_acceptance_request_idx",
        "launchplane_every_code_feedback_acceptances",
        ["request_id", "received_at", "feedback_id"],
    )

    _create_table_if_missing(
        "launchplane_every_code_feedback_resume_intents",
        sa.Column("intent_id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("acceptance_id", sa.String(), nullable=False),
        sa.Column("expected_lifecycle_id", sa.String(), nullable=False),
        sa.Column("expected_fencing_token", sa.Integer(), nullable=False),
        sa.Column("intent_digest", sa.String(), nullable=False),
        sa.Column("issued_at", sa.String(), nullable=False),
        sa.Column("eligible_until", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", _payload(), nullable=False),
        sa.ForeignKeyConstraint(
            ["acceptance_id"], ["launchplane_every_code_feedback_acceptances.acceptance_id"]
        ),
        sa.CheckConstraint(
            "expected_fencing_token >= 0",
            name="launchplane_every_code_feedback_resume_intent_fence_ck",
        ),
        sa.UniqueConstraint(
            "intent_digest", name="launchplane_every_code_feedback_resume_intent_digest_uidx"
        ),
    )
    _create_index_if_missing(
        "launchplane_every_code_feedback_resume_intent_request_idx",
        "launchplane_every_code_feedback_resume_intents",
        ["request_id", "issued_at"],
    )

    _create_table_if_missing(
        "launchplane_every_code_feedback_resume_operations",
        sa.Column("operation_id", sa.String(), primary_key=True),
        sa.Column("intent_id", sa.String(), nullable=False),
        sa.Column("acceptance_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("lifecycle_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("launch_nonce", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("payload", _payload(), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["launchplane_every_code_feedback_resume_intents.intent_id"]
        ),
        sa.ForeignKeyConstraint(
            ["acceptance_id"], ["launchplane_every_code_feedback_acceptances.acceptance_id"]
        ),
        sa.CheckConstraint(
            "fencing_token > 0", name="launchplane_every_code_feedback_resume_operation_fence_ck"
        ),
        sa.UniqueConstraint(
            "intent_id", name="launchplane_every_code_feedback_resume_operation_intent_uidx"
        ),
        sa.UniqueConstraint(
            "request_id",
            "lifecycle_id",
            name="launchplane_every_code_feedback_resume_operation_lifecycle_uidx",
        ),
        sa.UniqueConstraint(
            "launch_nonce", name="launchplane_every_code_feedback_resume_operation_nonce_uidx"
        ),
    )
    _create_index_if_missing(
        "launchplane_every_code_feedback_resume_operation_request_idx",
        "launchplane_every_code_feedback_resume_operations",
        ["request_id", "created_at"],
    )

    _create_table_if_missing(
        "launchplane_every_code_feedback_resume_receipts",
        sa.Column("receipt_id", sa.String(), primary_key=True),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("lifecycle_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("launch_nonce", sa.String(), nullable=False),
        sa.Column("receipt_kind", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column("receipt_digest", sa.String(), nullable=False),
        sa.Column("payload", _payload(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["launchplane_every_code_feedback_resume_operations.operation_id"]
        ),
        sa.CheckConstraint(
            "fencing_token > 0", name="launchplane_every_code_feedback_resume_receipt_fence_ck"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "receipt_kind",
            name="launchplane_every_code_feedback_resume_receipt_kind_uidx",
        ),
    )

    _create_table_if_missing(
        "launchplane_every_code_feedback_resume_recovery_evidence",
        sa.Column("evidence_id", sa.String(), primary_key=True),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("lifecycle_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("launch_nonce", sa.String(), nullable=False),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("observed_at", sa.String(), nullable=False),
        sa.Column("evidence_digest", sa.String(), nullable=False),
        sa.Column("payload", _payload(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["launchplane_every_code_feedback_resume_operations.operation_id"]
        ),
        sa.CheckConstraint(
            "fencing_token > 0", name="launchplane_every_code_feedback_resume_recovery_fence_ck"
        ),
    )
    _create_index_if_missing(
        "launchplane_every_code_feedback_resume_recovery_operation_idx",
        "launchplane_every_code_feedback_resume_recovery_evidence",
        ["operation_id", "observed_at"],
    )

    _create_table_if_missing(
        "launchplane_every_code_pull_request_closures",
        sa.Column("closure_id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("pr_node_id", sa.String(), nullable=False),
        sa.Column("closed_at", sa.String(), nullable=False),
        sa.Column("merged", sa.Boolean(), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("closure_digest", sa.String(), nullable=False),
        sa.Column("payload", _payload(), nullable=False),
        sa.CheckConstraint(
            "repository_id > 0", name="launchplane_every_code_pull_request_closure_repository_id_ck"
        ),
        sa.UniqueConstraint(
            "closure_digest", name="launchplane_every_code_pull_request_closure_digest_uidx"
        ),
        sa.UniqueConstraint(
            "repository_id",
            "pr_number",
            "closed_at",
            name="launchplane_every_code_pull_request_closure_event_uidx",
        ),
    )
    _create_index_if_missing(
        "launchplane_every_code_pull_request_closure_request_idx",
        "launchplane_every_code_pull_request_closures",
        ["request_id", "closed_at"],
    )


def downgrade() -> None:
    op.drop_table("launchplane_every_code_pull_request_closures")
    op.drop_table("launchplane_every_code_feedback_resume_recovery_evidence")
    op.drop_table("launchplane_every_code_feedback_resume_receipts")
    op.drop_table("launchplane_every_code_feedback_resume_operations")
    op.drop_table("launchplane_every_code_feedback_resume_intents")
    op.drop_table("launchplane_every_code_feedback_acceptances")
