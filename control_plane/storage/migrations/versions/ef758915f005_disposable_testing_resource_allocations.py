"""Keep fresh resource allocation outside retained target custody payloads."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "ef758915f005"
down_revision: str = "de647804f004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if "launchplane_disposable_testing_resource_allocations" not in set(
        sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_disposable_testing_resource_allocations",
            sa.Column("operation_id", sa.String(), primary_key=True),
            sa.Column(
                "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
            ),
        )


def downgrade() -> None:
    # Preserve partial allocations and their immutable resource identities.
    # Older images ignore this additive table; re-upgrade adopts it.
    pass
