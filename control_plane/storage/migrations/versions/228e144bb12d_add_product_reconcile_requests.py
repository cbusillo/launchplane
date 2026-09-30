"""Add GitHub App webhook deliveries and product reconcile requests.

Revision ID: 228e144bb12d
Revises: b1d3f5a7c9e2
"""

from collections.abc import Sequence
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "228e144bb12d"
down_revision: str | None = "b1d3f5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUEST_TABLE = "launchplane_product_reconcile_requests"
_REQUEST_INDEX = "launchplane_product_reconcile_requests_state_idx"
_DELIVERY_TABLE = "launchplane_github_app_webhook_deliveries"
_DELIVERY_INDEX = "launchplane_github_app_webhook_deliveries_received_idx"


def _payload_column() -> sa.Column[Any]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _REQUEST_TABLE not in existing_tables:
        op.create_table(
            _REQUEST_TABLE,
            sa.Column("target_key", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("target_kind", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("requested_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("lease_expires_at", sa.String(), nullable=False, server_default=""),
            _payload_column(),
            sa.PrimaryKeyConstraint("target_key"),
        )
        op.create_index(_REQUEST_INDEX, _REQUEST_TABLE, ["state", "requested_at"])
    if _DELIVERY_TABLE not in existing_tables:
        op.create_table(
            _DELIVERY_TABLE,
            sa.Column("delivery_id", sa.String(), nullable=False),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("received_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("delivery_id"),
        )
        op.create_index(_DELIVERY_INDEX, _DELIVERY_TABLE, ["received_at"])


def downgrade() -> None:
    op.drop_index(_DELIVERY_INDEX, table_name=_DELIVERY_TABLE)
    op.drop_table(_DELIVERY_TABLE)
    op.drop_index(_REQUEST_INDEX, table_name=_REQUEST_TABLE)
    op.drop_table(_REQUEST_TABLE)
