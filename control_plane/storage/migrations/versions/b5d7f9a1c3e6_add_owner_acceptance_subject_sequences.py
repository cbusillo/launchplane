"""add Owner acceptance subject sequences

Revision ID: b5d7f9a1c3e6
Revises: a4c6e8f0b2d5
Create Date: 2026-08-11 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b5d7f9a1c3e6"
down_revision: str | None = "a4c6e8f0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TABLE = "launchplane_owner_acceptance_events"
_SEQUENCE_TABLE = "launchplane_owner_acceptance_subject_sequences"
_SUBJECT_COLUMNS = (
    "repository_id",
    "pr_number",
    "product",
    "system",
    "owner_action",
    "environment",
)
_SUBJECT_INDEX = "launchplane_owner_acceptance_events_subject_idx"
_SUBJECT_SEQUENCE_INDEX = "launchplane_owner_acceptance_events_subject_sequence_uidx"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _column_exists(_EVENT_TABLE, "subject_sequence"):
        op.add_column(
            _EVENT_TABLE,
            sa.Column("subject_sequence", sa.BigInteger(), nullable=True),
        )

    events = sa.table(
        _EVENT_TABLE,
        sa.column("event_id", sa.String()),
        *(sa.column(column_name) for column_name in _SUBJECT_COLUMNS),
        sa.column("occurred_at", sa.String()),
        sa.column("subject_sequence", sa.BigInteger()),
    )
    rows = (
        op.get_bind()
        .execute(
            sa.select(
                events.c.event_id,
                *(getattr(events.c, column_name) for column_name in _SUBJECT_COLUMNS),
                events.c.occurred_at,
            )
        )
        .mappings()
    )
    grouped_rows: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row_mapping in rows:
        subject_key = tuple(row_mapping[column_name] for column_name in _SUBJECT_COLUMNS)
        grouped_rows.setdefault(subject_key, []).append(dict(row_mapping))

    subject_last_sequences: list[dict[str, object]] = []
    for subject_key, subject_rows in grouped_rows.items():
        ordered_rows = sorted(
            subject_rows,
            key=lambda row: (str(row["occurred_at"]), str(row["event_id"])),
        )
        for sequence, row in enumerate(ordered_rows, start=1):
            op.get_bind().execute(
                sa.update(events)
                .where(events.c.event_id == str(row["event_id"]))
                .values(subject_sequence=sequence)
            )
        subject_last_sequences.append(
            dict(zip(_SUBJECT_COLUMNS, subject_key, strict=True))
            | {"last_sequence": len(ordered_rows)}
        )

    with op.batch_alter_table(_EVENT_TABLE) as batch_op:
        batch_op.alter_column(
            "subject_sequence",
            existing_type=sa.BigInteger(),
            nullable=False,
        )

    if _index_exists(_EVENT_TABLE, _SUBJECT_INDEX):
        op.drop_index(_SUBJECT_INDEX, table_name=_EVENT_TABLE)
    op.create_index(
        _SUBJECT_INDEX,
        _EVENT_TABLE,
        [*_SUBJECT_COLUMNS, sa.text("subject_sequence DESC")],
    )
    if not _index_exists(_EVENT_TABLE, _SUBJECT_SEQUENCE_INDEX):
        op.create_index(
            _SUBJECT_SEQUENCE_INDEX,
            _EVENT_TABLE,
            [*_SUBJECT_COLUMNS, "subject_sequence"],
            unique=True,
        )

    if not _table_exists(_SEQUENCE_TABLE):
        op.create_table(
            _SEQUENCE_TABLE,
            *(
                sa.Column(column_name, sa.String(), nullable=False)
                for column_name in _SUBJECT_COLUMNS
                if column_name != "pr_number"
            ),
            sa.Column("pr_number", sa.BigInteger(), nullable=False),
            sa.Column("last_sequence", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint(*_SUBJECT_COLUMNS),
        )
    if subject_last_sequences:
        sequences = sa.table(
            _SEQUENCE_TABLE,
            *(sa.column(column_name) for column_name in _SUBJECT_COLUMNS),
            sa.column("last_sequence"),
        )
        connection = op.get_bind()
        for subject_seed in subject_last_sequences:
            subject_filter = sa.and_(
                *(
                    sequences.c[column_name] == subject_seed[column_name]
                    for column_name in _SUBJECT_COLUMNS
                )
            )
            existing = connection.execute(
                sa.select(sequences.c.last_sequence).where(subject_filter)
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(sa.insert(sequences).values(**subject_seed))
            elif int(existing) < int(str(subject_seed["last_sequence"])):
                connection.execute(
                    sa.update(sequences)
                    .where(subject_filter)
                    .values(last_sequence=subject_seed["last_sequence"])
                )


def downgrade() -> None:
    if _table_exists(_SEQUENCE_TABLE):
        op.drop_table(_SEQUENCE_TABLE)
    if _index_exists(_EVENT_TABLE, _SUBJECT_SEQUENCE_INDEX):
        op.drop_index(_SUBJECT_SEQUENCE_INDEX, table_name=_EVENT_TABLE)
    if _index_exists(_EVENT_TABLE, _SUBJECT_INDEX):
        op.drop_index(_SUBJECT_INDEX, table_name=_EVENT_TABLE)
    op.create_index(
        _SUBJECT_INDEX,
        _EVENT_TABLE,
        [
            "repository_id",
            "pr_number",
            "product",
            "system",
            "owner_action",
            sa.text("occurred_at DESC"),
        ],
    )
    if _column_exists(_EVENT_TABLE, "subject_sequence"):
        with op.batch_alter_table(_EVENT_TABLE) as batch_op:
            batch_op.drop_column("subject_sequence")
