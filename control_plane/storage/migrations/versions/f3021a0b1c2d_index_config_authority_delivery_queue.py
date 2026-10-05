"""Index source-event scan state without rewriting delivery history.

Revision ID: f3021a0b1c2d
Revises: c87d8d574b67
"""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "f3021a0b1c2d"
down_revision: str = "c87d8d574b67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_github_app_webhook_deliveries"
_INDEX = "launchplane_github_app_webhook_deliveries_config_scan_idx"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        table = sa.table(_TABLE, sa.column("payload", sa.JSON()))
        op.create_index(
            _INDEX,
            _TABLE,
            [table.c.payload["config_authority_state"].as_string(), "received_at"],
            if_not_exists=True,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(_INDEX, table_name=_TABLE, if_exists=True)
