"""Preserve immutable ordinary-agent client admission intent.

Revision ID: a3c5e7f9b1d4
Revises: e0f2a4c6d8b1
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a3c5e7f9b1d4"
down_revision: str | None = "e0f2a4c6d8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINITE_REQUESTS = "launchplane_ordinary_agent_finite_requests"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {str(column["name"]) for column in inspector.get_columns(_FINITE_REQUESTS)}
    if "client_intent_payload" not in columns:
        op.add_column(
            _FINITE_REQUESTS,
            sa.Column(
                "client_intent_payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {str(column["name"]) for column in inspector.get_columns(_FINITE_REQUESTS)}
    if "client_intent_payload" in columns:
        op.drop_column(_FINITE_REQUESTS, "client_intent_payload")
