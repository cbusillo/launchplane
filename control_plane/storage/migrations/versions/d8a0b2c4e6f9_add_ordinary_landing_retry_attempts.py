"""Add bounded ordinary-agent landing retry attempt associations.

Revision ID: d8a0b2c4e6f9
Revises: c7f9a1b3d5e8
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d8a0b2c4e6f9"
down_revision: str | None = "c7f9a1b3d5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREPARATION_TABLE = "launchplane_ordinary_agent_landing_preparations"
_BINDING_TABLE = "launchplane_ordinary_agent_landing_bindings"
_DISPATCH_TABLE = "launchplane_ordinary_agent_semantic_dispatches"
_OUTCOME_TABLE = "launchplane_merge_landing_outcomes"
_SQLITE_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _unique_constraint_name(
    inspector: sa.Inspector,
    *,
    table_name: str,
    column_names: tuple[str, ...],
    sqlite_fallback: str,
) -> str | None:
    for constraint in inspector.get_unique_constraints(table_name):
        if tuple(constraint.get("column_names") or ()) == column_names:
            name = constraint.get("name")
            return str(name) if name else sqlite_fallback
    return None


def _payload_has_key_clause(*, dialect_name: str, path: str) -> str:
    if dialect_name == "postgresql":
        keys = path.split(".")
        if len(keys) == 1:
            return f"payload ? '{keys[0]}'"
        parent = ",".join(keys[:-1])
        return f"(payload #> '{{{parent}}}') ? '{keys[-1]}'"
    return f"json_type(payload, '$.{path}') IS NOT NULL"


def _install_retry_landing_identity_guard() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION launchplane_ordinary_landing_identity_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ordinary landing history is permanent'; END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.preparation_id, NEW.request_id, NEW.lease_id, NEW.binding_revision,
                 NEW.pull_request_number, NEW.attempt_ordinal, NEW.predecessor_preparation_id,
                 NEW.action_ordinal, NEW.custody_attempt_id)
                IS DISTINCT FROM
                (OLD.preparation_id, OLD.request_id, OLD.lease_id, OLD.binding_revision,
                 OLD.pull_request_number, OLD.attempt_ordinal, OLD.predecessor_preparation_id,
                 OLD.action_ordinal, OLD.custody_attempt_id)
            THEN RAISE EXCEPTION 'ordinary landing identity is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.payload - ARRAY['revision','state','work_expires_at','evidence','authority','effect_id','reason_code'])
                IS DISTINCT FROM
                (OLD.payload - ARRAY['revision','state','work_expires_at','evidence','authority','effect_id','reason_code'])
            THEN RAISE EXCEPTION 'ordinary landing intent is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'work_expires_at' IS NOT NULL
                AND NEW.payload->'work_expires_at' IS DISTINCT FROM OLD.payload->'work_expires_at'
            THEN RAISE EXCEPTION 'ordinary landing deadline is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'state' IN ('observed','consumed') AND
                (NEW.payload->'evidence', NEW.payload->'authority') IS DISTINCT FROM
                (OLD.payload->'evidence', OLD.payload->'authority')
            THEN RAISE EXCEPTION 'ordinary landing evidence is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'state' IN ('consumed','superseded')
                AND NEW.payload IS DISTINCT FROM OLD.payload
            THEN RAISE EXCEPTION 'ordinary landing terminal history is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'state' = 'terminal'
                AND NEW.payload IS DISTINCT FROM OLD.payload
                AND NOT COALESCE((
                    NEW.payload->>'state' = 'superseded'
                    AND (NEW.payload->>'revision')::bigint =
                        (OLD.payload->>'revision')::bigint + 1
                    AND (NEW.payload - ARRAY['revision','state']) IS NOT DISTINCT FROM
                        (OLD.payload - ARRAY['revision','state'])
                ), FALSE)
            THEN RAISE EXCEPTION 'ordinary landing terminal history is immutable'; END IF;
            IF NEW.payload->>'preparation_id' IS DISTINCT FROM NEW.preparation_id
                OR NEW.payload->>'request_id' IS DISTINCT FROM NEW.request_id
                OR NEW.payload->>'lease_id' IS DISTINCT FROM NEW.lease_id
                OR (NEW.payload->>'binding_revision')::bigint IS DISTINCT FROM NEW.binding_revision
                OR (NEW.payload->'entry'->>'pull_request_number')::bigint IS DISTINCT FROM NEW.pull_request_number
                OR COALESCE((NEW.payload->>'attempt_ordinal')::bigint, 1)
                    IS DISTINCT FROM NEW.attempt_ordinal
                OR NEW.payload->>'predecessor_preparation_id'
                    IS DISTINCT FROM NEW.predecessor_preparation_id
                OR (NEW.payload->>'action_ordinal')::bigint IS DISTINCT FROM NEW.action_ordinal
                OR NEW.payload->>'custody_attempt_id' IS DISTINCT FROM NEW.custody_attempt_id
                OR (NEW.payload->>'revision')::bigint IS DISTINCT FROM NEW.revision
            THEN RAISE EXCEPTION 'ordinary landing payload identity mismatch'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)


def _restore_legacy_landing_identity_guard() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION launchplane_ordinary_landing_identity_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ordinary landing history is permanent'; END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.preparation_id, NEW.request_id, NEW.lease_id, NEW.binding_revision,
                 NEW.pull_request_number, NEW.action_ordinal, NEW.custody_attempt_id)
                IS DISTINCT FROM
                (OLD.preparation_id, OLD.request_id, OLD.lease_id, OLD.binding_revision,
                 OLD.pull_request_number, OLD.action_ordinal, OLD.custody_attempt_id)
            THEN RAISE EXCEPTION 'ordinary landing identity is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.payload - ARRAY['revision','state','work_expires_at','evidence','authority','effect_id','reason_code'])
                IS DISTINCT FROM
                (OLD.payload - ARRAY['revision','state','work_expires_at','evidence','authority','effect_id','reason_code'])
            THEN RAISE EXCEPTION 'ordinary landing intent is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'work_expires_at' IS NOT NULL
                AND NEW.payload->'work_expires_at' IS DISTINCT FROM OLD.payload->'work_expires_at'
            THEN RAISE EXCEPTION 'ordinary landing deadline is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'state' IN ('observed','consumed') AND
                (NEW.payload->'evidence', NEW.payload->'authority') IS DISTINCT FROM
                (OLD.payload->'evidence', OLD.payload->'authority')
            THEN RAISE EXCEPTION 'ordinary landing evidence is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload->>'state' IN ('consumed','terminal')
                AND NEW.payload IS DISTINCT FROM OLD.payload
            THEN RAISE EXCEPTION 'ordinary landing terminal history is immutable'; END IF;
            IF NEW.payload->>'preparation_id' IS DISTINCT FROM NEW.preparation_id
                OR NEW.payload->>'request_id' IS DISTINCT FROM NEW.request_id
                OR NEW.payload->>'lease_id' IS DISTINCT FROM NEW.lease_id
                OR (NEW.payload->>'binding_revision')::bigint IS DISTINCT FROM NEW.binding_revision
                OR (NEW.payload->'entry'->>'pull_request_number')::bigint IS DISTINCT FROM NEW.pull_request_number
                OR (NEW.payload->>'action_ordinal')::bigint IS DISTINCT FROM NEW.action_ordinal
                OR NEW.payload->>'custody_attempt_id' IS DISTINCT FROM NEW.custody_attempt_id
                OR (NEW.payload->>'revision')::bigint IS DISTINCT FROM NEW.revision
            THEN RAISE EXCEPTION 'ordinary landing payload identity mismatch'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if _PREPARATION_TABLE in table_names:
        columns = {str(column["name"]) for column in inspector.get_columns(_PREPARATION_TABLE)}
        charge_constraint = _unique_constraint_name(
            inspector,
            table_name=_PREPARATION_TABLE,
            column_names=("lease_id", "action_ordinal"),
            sqlite_fallback="ordinary_landing_charge_uq",
        )
        entry_constraint = _unique_constraint_name(
            inspector,
            table_name=_PREPARATION_TABLE,
            column_names=("request_id", "binding_revision", "pull_request_number"),
            sqlite_fallback="ordinary_landing_entry_uq",
        )
        unique_constraints = {
            str(constraint.get("name") or ""): tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(_PREPARATION_TABLE)
        }
        needs_batch = (
            "attempt_ordinal" not in columns
            or "predecessor_preparation_id" not in columns
            or charge_constraint is not None
            or entry_constraint is not None
            or unique_constraints.get("ordinary_landing_entry_attempt_uq")
            != ("request_id", "binding_revision", "pull_request_number", "attempt_ordinal")
            or unique_constraints.get("ordinary_landing_predecessor_uq")
            != ("predecessor_preparation_id",)
        )
        if needs_batch:
            with op.batch_alter_table(
                _PREPARATION_TABLE,
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch_op:
                if "attempt_ordinal" not in columns:
                    batch_op.add_column(
                        sa.Column(
                            "attempt_ordinal",
                            sa.BigInteger(),
                            nullable=False,
                            server_default=sa.text("1"),
                        )
                    )
                if "predecessor_preparation_id" not in columns:
                    batch_op.add_column(
                        sa.Column("predecessor_preparation_id", sa.String(), nullable=True)
                    )
                if charge_constraint is not None:
                    batch_op.drop_constraint(charge_constraint, type_="unique")
                if entry_constraint is not None:
                    batch_op.drop_constraint(entry_constraint, type_="unique")
                if unique_constraints.get("ordinary_landing_entry_attempt_uq") != (
                    "request_id",
                    "binding_revision",
                    "pull_request_number",
                    "attempt_ordinal",
                ):
                    batch_op.create_unique_constraint(
                        "ordinary_landing_entry_attempt_uq",
                        (
                            "request_id",
                            "binding_revision",
                            "pull_request_number",
                            "attempt_ordinal",
                        ),
                    )
                if unique_constraints.get("ordinary_landing_predecessor_uq") != (
                    "predecessor_preparation_id",
                ):
                    batch_op.create_unique_constraint(
                        "ordinary_landing_predecessor_uq",
                        ("predecessor_preparation_id",),
                    )
        inspector = sa.inspect(bind)
        preparation_indexes = {
            str(index["name"]) for index in inspector.get_indexes(_PREPARATION_TABLE)
        }
        if "ordinary_landing_root_charge_uq" not in preparation_indexes:
            op.create_index(
                "ordinary_landing_root_charge_uq",
                _PREPARATION_TABLE,
                ("lease_id", "action_ordinal"),
                unique=True,
                postgresql_where=sa.text("predecessor_preparation_id IS NULL"),
                sqlite_where=sa.text("predecessor_preparation_id IS NULL"),
            )

    inspector = sa.inspect(bind)
    if _BINDING_TABLE in table_names:
        columns = {str(column["name"]) for column in inspector.get_columns(_BINDING_TABLE)}
        effect_constraint = _unique_constraint_name(
            inspector,
            table_name=_BINDING_TABLE,
            column_names=("effect_id",),
            sqlite_fallback=f"uq_{_BINDING_TABLE}_effect_id",
        )
        unique_constraints = {
            str(constraint.get("name") or ""): tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(_BINDING_TABLE)
        }
        needs_batch = (
            "dispatch_ordinal" not in columns
            or effect_constraint is not None
            or unique_constraints.get("ordinary_landing_effect_dispatch_uq")
            != ("effect_id", "dispatch_ordinal")
        )
        if needs_batch:
            with op.batch_alter_table(
                _BINDING_TABLE,
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch_op:
                if "dispatch_ordinal" not in columns:
                    batch_op.add_column(
                        sa.Column(
                            "dispatch_ordinal",
                            sa.BigInteger(),
                            nullable=False,
                            server_default=sa.text("1"),
                        )
                    )
                if effect_constraint is not None:
                    batch_op.drop_constraint(effect_constraint, type_="unique")
                if unique_constraints.get("ordinary_landing_effect_dispatch_uq") != (
                    "effect_id",
                    "dispatch_ordinal",
                ):
                    batch_op.create_unique_constraint(
                        "ordinary_landing_effect_dispatch_uq",
                        ("effect_id", "dispatch_ordinal"),
                    )
        inspector = sa.inspect(bind)
        binding_indexes = {str(index["name"]) for index in inspector.get_indexes(_BINDING_TABLE)}
        if "ix_launchplane_ordinary_agent_landing_bindings_effect_id" not in binding_indexes:
            op.create_index(
                "ix_launchplane_ordinary_agent_landing_bindings_effect_id",
                _BINDING_TABLE,
                ("effect_id",),
                unique=False,
            )
    if bind.dialect.name == "postgresql" and _PREPARATION_TABLE in table_names:
        _install_retry_landing_identity_guard()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    preparation_columns = (
        {str(column["name"]) for column in inspector.get_columns(_PREPARATION_TABLE)}
        if _PREPARATION_TABLE in table_names
        else set()
    )
    binding_columns = (
        {str(column["name"]) for column in inspector.get_columns(_BINDING_TABLE)}
        if _BINDING_TABLE in table_names
        else set()
    )
    has_retry_preparation = {
        "attempt_ordinal",
        "predecessor_preparation_id",
    } <= preparation_columns and bind.execute(
        sa.text(
            f"SELECT 1 FROM {_PREPARATION_TABLE} "
            "WHERE attempt_ordinal > 1 OR predecessor_preparation_id IS NOT NULL LIMIT 1"
        )
    ).first() is not None
    has_retry_binding = (
        "dispatch_ordinal" in binding_columns
        and bind.execute(
            sa.text(f"SELECT 1 FROM {_BINDING_TABLE} WHERE dispatch_ordinal > 1 LIMIT 1")
        ).first()
        is not None
    )
    has_admission_association = False
    association_clause = _payload_has_key_clause(
        dialect_name=bind.dialect.name, path="admission_id"
    )
    if _DISPATCH_TABLE in table_names:
        has_admission_association = (
            bind.execute(
                sa.text(f"SELECT 1 FROM {_DISPATCH_TABLE} WHERE {association_clause} LIMIT 1")
            ).first()
            is not None
        )
    if not has_admission_association and _BINDING_TABLE in table_names:
        nested_association_clause = _payload_has_key_clause(
            dialect_name=bind.dialect.name, path="child.admission_id"
        )
        has_admission_association = (
            bind.execute(
                sa.text(f"SELECT 1 FROM {_BINDING_TABLE} WHERE {nested_association_clause} LIMIT 1")
            ).first()
            is not None
        )
    has_dispatch_not_attempted = False
    if _OUTCOME_TABLE in table_names:
        reason_expression = (
            "payload->>'reason'"
            if bind.dialect.name == "postgresql"
            else "json_extract(payload, '$.reason')"
        )
        has_dispatch_not_attempted = (
            bind.execute(
                sa.text(
                    f"SELECT 1 FROM {_OUTCOME_TABLE} "
                    f"WHERE {reason_expression} = 'dispatch_not_attempted' LIMIT 1"
                )
            ).first()
            is not None
        )
    if has_retry_preparation or has_retry_binding:
        raise RuntimeError(
            "Cannot downgrade ordinary landing retry storage while retry attempts exist; "
            "restore from a c7-compatible backup instead."
        )
    if has_admission_association:
        raise RuntimeError(
            "Cannot downgrade ordinary landing retry storage while admission-associated landing "
            "dispatch history exists; restore from a c7-compatible backup instead."
        )
    if has_dispatch_not_attempted:
        raise RuntimeError(
            "Cannot downgrade ordinary landing retry storage while dispatch-not-attempted landing "
            "outcomes exist; restore from a c7-compatible backup instead."
        )

    if bind.dialect.name == "postgresql" and _PREPARATION_TABLE in table_names:
        _restore_legacy_landing_identity_guard()

    if _BINDING_TABLE in table_names:
        indexes = {str(index["name"]) for index in inspector.get_indexes(_BINDING_TABLE)}
        if "ix_launchplane_ordinary_agent_landing_bindings_effect_id" in indexes:
            op.drop_index(
                "ix_launchplane_ordinary_agent_landing_bindings_effect_id",
                table_name=_BINDING_TABLE,
            )
        inspector = sa.inspect(bind)
        columns = {str(column["name"]) for column in inspector.get_columns(_BINDING_TABLE)}
        effect_constraint = _unique_constraint_name(
            inspector,
            table_name=_BINDING_TABLE,
            column_names=("effect_id",),
            sqlite_fallback=f"uq_{_BINDING_TABLE}_effect_id",
        )
        dispatch_constraint = _unique_constraint_name(
            inspector,
            table_name=_BINDING_TABLE,
            column_names=("effect_id", "dispatch_ordinal"),
            sqlite_fallback="ordinary_landing_effect_dispatch_uq",
        )
        with op.batch_alter_table(
            _BINDING_TABLE,
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch_op:
            if dispatch_constraint is not None:
                batch_op.drop_constraint(dispatch_constraint, type_="unique")
            if "dispatch_ordinal" in columns:
                batch_op.drop_column("dispatch_ordinal")
            if effect_constraint is None:
                batch_op.create_unique_constraint(
                    "ordinary_landing_effect_id_uq",
                    ("effect_id",),
                )

    inspector = sa.inspect(bind)
    if _PREPARATION_TABLE in table_names:
        indexes = {str(index["name"]) for index in inspector.get_indexes(_PREPARATION_TABLE)}
        if "ordinary_landing_root_charge_uq" in indexes:
            op.drop_index("ordinary_landing_root_charge_uq", table_name=_PREPARATION_TABLE)
        inspector = sa.inspect(bind)
        columns = {str(column["name"]) for column in inspector.get_columns(_PREPARATION_TABLE)}
        entry_attempt_constraint = _unique_constraint_name(
            inspector,
            table_name=_PREPARATION_TABLE,
            column_names=(
                "request_id",
                "binding_revision",
                "pull_request_number",
                "attempt_ordinal",
            ),
            sqlite_fallback="ordinary_landing_entry_attempt_uq",
        )
        predecessor_constraint = _unique_constraint_name(
            inspector,
            table_name=_PREPARATION_TABLE,
            column_names=("predecessor_preparation_id",),
            sqlite_fallback="ordinary_landing_predecessor_uq",
        )
        charge_constraint = _unique_constraint_name(
            inspector,
            table_name=_PREPARATION_TABLE,
            column_names=("lease_id", "action_ordinal"),
            sqlite_fallback="ordinary_landing_charge_uq",
        )
        entry_constraint = _unique_constraint_name(
            inspector,
            table_name=_PREPARATION_TABLE,
            column_names=("request_id", "binding_revision", "pull_request_number"),
            sqlite_fallback="ordinary_landing_entry_uq",
        )
        with op.batch_alter_table(
            _PREPARATION_TABLE,
            naming_convention=_SQLITE_NAMING_CONVENTION,
        ) as batch_op:
            if entry_attempt_constraint is not None:
                batch_op.drop_constraint(entry_attempt_constraint, type_="unique")
            if predecessor_constraint is not None:
                batch_op.drop_constraint(predecessor_constraint, type_="unique")
            if "predecessor_preparation_id" in columns:
                batch_op.drop_column("predecessor_preparation_id")
            if "attempt_ordinal" in columns:
                batch_op.drop_column("attempt_ordinal")
            if charge_constraint is None:
                batch_op.create_unique_constraint(
                    "ordinary_landing_charge_uq",
                    ("lease_id", "action_ordinal"),
                )
            if entry_constraint is None:
                batch_op.create_unique_constraint(
                    "ordinary_landing_entry_uq",
                    ("request_id", "binding_revision", "pull_request_number"),
                )
