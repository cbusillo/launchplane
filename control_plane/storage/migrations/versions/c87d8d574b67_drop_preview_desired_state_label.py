"""Drop the retired preview label column from preview desired states.

Revision ID: c87d8d574b67
Revises: b7e9f1a3c5d8

Previews follow the pull request's state, not a label (launchplane#2735), and
the column has been written empty since #2737. Each row's payload keeps the
label older records named, so dropping the column loses nothing. Downgrade
restores the column with empty values.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c87d8d574b67"
down_revision: str = "b7e9f1a3c5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_preview_desired_states"


def _has_label_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_TABLE)
    return any(column["name"] == "label" for column in columns)


def upgrade() -> None:
    if _has_label_column():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column("label")


def downgrade() -> None:
    if not _has_label_column():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(sa.Column("label", sa.String(), nullable=False, server_default=""))
