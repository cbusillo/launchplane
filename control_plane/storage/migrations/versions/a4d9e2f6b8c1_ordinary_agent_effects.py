"""Persist scoped ordinary effects, provider observations and worker claims."""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a4d9e2f6b8c1"
down_revision: str | None = "f3c8e1a2d5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    if (
        "launchplane_ordinary_agent_landing_preparations"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_landing_preparations",
            sa.Column("preparation_id", sa.String(), primary_key=True),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("lease_id", sa.String(), nullable=False),
            sa.Column("binding_revision", sa.BigInteger(), nullable=False),
            sa.Column("pull_request_number", sa.BigInteger(), nullable=False),
            sa.Column("action_ordinal", sa.BigInteger(), nullable=False),
            sa.Column("custody_attempt_id", sa.String(), nullable=False),
            sa.Column("revision", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint("lease_id", "action_ordinal", name="ordinary_landing_charge_uq"),
            sa.UniqueConstraint(
                "request_id",
                "binding_revision",
                "pull_request_number",
                name="ordinary_landing_entry_uq",
            ),
            sa.UniqueConstraint("custody_attempt_id", name="ordinary_landing_custody_uq"),
        )
    if (
        "launchplane_ordinary_agent_landing_bindings"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_landing_bindings",
            sa.Column("preparation_id", sa.String(), primary_key=True),
            sa.Column("admission_id", sa.String(), nullable=False, unique=True),
            sa.Column("effect_id", sa.String(), nullable=False, unique=True),
            sa.Column("child_id", sa.String(), nullable=False, unique=True),
            sa.Column("payload", payload, nullable=False),
        )
    if "launchplane_ordinary_agent_effects" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_ordinary_agent_effects",
            sa.Column("effect_id", sa.String(), primary_key=True),
            sa.Column("lease_id", sa.String(), nullable=False),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("scope_sha256", sa.String(), nullable=False),
            sa.Column("binding_revision", sa.BigInteger(), nullable=False),
            sa.Column("action_ordinal", sa.BigInteger(), nullable=False),
            sa.Column("semantic_key", sa.String(), nullable=False),
            sa.Column("command_sha256", sa.String(), nullable=False),
            sa.Column("revision", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint("lease_id", "action_ordinal", name="ordinary_effect_charge_uq"),
            sa.UniqueConstraint(
                "request_id", "binding_revision", "semantic_key", name="ordinary_effect_semantic_uq"
            ),
        )
    if "ix_launchplane_ordinary_agent_effects_request_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("launchplane_ordinary_agent_effects")
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_effects_request_id",
            "launchplane_ordinary_agent_effects",
            ["request_id"],
            unique=False,
        )
    if (
        "launchplane_ordinary_agent_semantic_dispatches"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_semantic_dispatches",
            sa.Column("child_id", sa.String(), primary_key=True),
            sa.Column("effect_id", sa.String(), nullable=False),
            sa.Column("semantic_ordinal", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint(
                "effect_id", "semantic_ordinal", name="ordinary_dispatch_ordinal_uq"
            ),
        )
    if (
        "launchplane_ordinary_agent_semantic_outcomes"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_semantic_outcomes",
            sa.Column("child_id", sa.String(), primary_key=True),
            sa.Column("payload", payload, nullable=False),
        )
    if (
        "launchplane_ordinary_agent_effect_reconciliations"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_effect_reconciliations",
            sa.Column("observation_id", sa.String(), primary_key=True),
            sa.Column("child_id", sa.String(), nullable=False),
            sa.Column("payload", payload, nullable=False),
        )
    if "ix_launchplane_ordinary_agent_effect_reconciliations_child_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(
            "launchplane_ordinary_agent_effect_reconciliations"
        )
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_effect_reconciliations_child_id",
            "launchplane_ordinary_agent_effect_reconciliations",
            ["child_id"],
            unique=False,
        )
    if (
        "launchplane_ordinary_agent_effect_completions"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_effect_completions",
            sa.Column("effect_id", sa.String(), primary_key=True),
            sa.Column("payload", payload, nullable=False),
        )
    if (
        "launchplane_ordinary_agent_effect_custody"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_effect_custody",
            sa.Column("attempt_id", sa.String(), primary_key=True),
            sa.Column("effect_id", sa.String(), nullable=False),
            sa.Column("purpose", sa.String(), nullable=False),
            sa.Column("payload", payload, nullable=False),
        )
    if "ix_launchplane_ordinary_agent_effect_custody_effect_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(
            "launchplane_ordinary_agent_effect_custody"
        )
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_effect_custody_effect_id",
            "launchplane_ordinary_agent_effect_custody",
            ["effect_id"],
            unique=False,
        )
    if (
        "launchplane_ordinary_agent_provider_waits"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_provider_waits",
            sa.Column("quota_key_sha256", sa.String(), primary_key=True),
            sa.Column("retry_not_before", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
        )
    if "launchplane_ordinary_agent_job_claims" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_ordinary_agent_job_claims",
            sa.Column("request_id", sa.String(), primary_key=True),
            sa.Column("worker_id", sa.String(), nullable=False),
            sa.Column("generation", sa.BigInteger(), nullable=False),
            sa.Column("claim_expires_at", sa.BigInteger(), nullable=False),
            sa.Column("next_due_at", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("reason_code", sa.String(), nullable=True),
        )
    if (
        "launchplane_ordinary_agent_read_attempts"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_read_attempts",
            sa.Column("attempt_id", sa.String(), primary_key=True),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("binding_revision", sa.BigInteger(), nullable=False),
            sa.Column("purpose", sa.String(), nullable=False),
            sa.Column("candidate_sha", sa.String(), nullable=False),
            sa.Column("attempt_ordinal", sa.BigInteger(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("revision", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint(
                "request_id",
                "binding_revision",
                "purpose",
                "candidate_sha",
                "attempt_ordinal",
                name="ordinary_read_attempt_ordinal_uq",
            ),
        )
    if "ordinary_read_active_uq" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(
            "launchplane_ordinary_agent_read_attempts"
        )
    }:
        op.create_index(
            "ordinary_read_active_uq",
            "launchplane_ordinary_agent_read_attempts",
            ["request_id", "binding_revision", "purpose"],
            unique=True,
            postgresql_where=sa.text("state IN ('reserved', 'reading')"),
            sqlite_where=sa.text("state IN ('reserved', 'reading')"),
        )
    if "launchplane_ordinary_agent_read_custody" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "launchplane_ordinary_agent_read_custody",
            sa.Column("custody_attempt_id", sa.String(), primary_key=True),
            sa.Column("read_attempt_id", sa.String(), nullable=False),
            sa.Column("payload", payload, nullable=False),
        )
    if "ix_launchplane_ordinary_agent_read_custody_read_attempt_id" not in {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("launchplane_ordinary_agent_read_custody")
    }:
        op.create_index(
            "ix_launchplane_ordinary_agent_read_custody_read_attempt_id",
            "launchplane_ordinary_agent_read_custody",
            ["read_attempt_id"],
            unique=False,
        )
    if (
        "launchplane_ordinary_agent_read_outcomes"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_read_outcomes",
            sa.Column("attempt_id", sa.String(), primary_key=True),
            sa.Column("payload", payload, nullable=False),
        )
    if (
        "launchplane_ordinary_agent_candidate_check_observations"
        not in sa.inspect(op.get_bind()).get_table_names()
    ):
        op.create_table(
            "launchplane_ordinary_agent_candidate_check_observations",
            sa.Column("observation_id", sa.String(), primary_key=True),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("binding_revision", sa.BigInteger(), nullable=False),
            sa.Column("candidate_sha", sa.String(), nullable=False),
            sa.Column("observation_ordinal", sa.BigInteger(), nullable=False),
            sa.Column("payload", payload, nullable=False),
            sa.UniqueConstraint(
                "request_id",
                "binding_revision",
                "candidate_sha",
                "observation_ordinal",
                name="ordinary_candidate_observation_uq",
            ),
        )
    if op.get_bind().dialect.name == "postgresql":
        _install_guards()


def _install_guards() -> None:
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
    op.execute(
        "CREATE TRIGGER ordinary_landing_identity BEFORE INSERT OR UPDATE OR DELETE ON launchplane_ordinary_agent_landing_preparations FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_landing_identity_guard()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION launchplane_ordinary_effect_identity_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ordinary effect history is permanent'; END IF;
            IF TG_OP = 'UPDATE' AND (NEW.effect_id, NEW.lease_id, NEW.request_id, NEW.scope_sha256,
                NEW.binding_revision, NEW.action_ordinal, NEW.semantic_key, NEW.command_sha256)
                IS DISTINCT FROM (OLD.effect_id, OLD.lease_id, OLD.request_id, OLD.scope_sha256,
                OLD.binding_revision, OLD.action_ordinal, OLD.semantic_key, OLD.command_sha256)
            THEN RAISE EXCEPTION 'ordinary effect identity is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.payload - ARRAY['state','revision','updated_at','dispatch_count','dispatch_custody_count','reconciliation_count','reconciliation_custody_count','next_observation_at','reason_code','rebound_revision'])
                IS DISTINCT FROM
                (OLD.payload - ARRAY['state','revision','updated_at','dispatch_count','dispatch_custody_count','reconciliation_count','reconciliation_custody_count','next_observation_at','reason_code','rebound_revision'])
            THEN RAISE EXCEPTION 'ordinary effect command and provenance are immutable'; END IF;
            IF NEW.payload->>'effect_id' IS DISTINCT FROM NEW.effect_id
                OR NEW.payload->>'lease_id' IS DISTINCT FROM NEW.lease_id
                OR NEW.payload->>'request_id' IS DISTINCT FROM NEW.request_id
                OR NEW.payload->>'scope_sha256' IS DISTINCT FROM NEW.scope_sha256
                OR (NEW.payload->>'binding_revision')::bigint IS DISTINCT FROM NEW.binding_revision
                OR (NEW.payload->>'action_ordinal')::bigint IS DISTINCT FROM NEW.action_ordinal
                OR NEW.payload->>'command_sha256' IS DISTINCT FROM NEW.command_sha256
            THEN RAISE EXCEPTION 'ordinary effect payload identity mismatch'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER ordinary_effect_identity BEFORE INSERT OR UPDATE OR DELETE ON launchplane_ordinary_agent_effects FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_effect_identity_guard()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION launchplane_ordinary_read_identity_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ordinary read history is permanent'; END IF;
            IF TG_OP = 'UPDATE' AND
                (NEW.attempt_id,NEW.request_id,NEW.binding_revision,NEW.purpose,NEW.candidate_sha,NEW.attempt_ordinal)
                IS DISTINCT FROM
                (OLD.attempt_id,OLD.request_id,OLD.binding_revision,OLD.purpose,OLD.candidate_sha,OLD.attempt_ordinal)
            THEN RAISE EXCEPTION 'ordinary read identity is immutable'; END IF;
            IF TG_OP = 'UPDATE' AND OLD.payload ? 'result' AND OLD.payload->'result' IS DISTINCT FROM NEW.payload->'result'
            THEN RAISE EXCEPTION 'ordinary read result is immutable'; END IF;
            IF NEW.payload->>'attempt_id' IS DISTINCT FROM NEW.attempt_id
                OR NEW.payload->>'request_id' IS DISTINCT FROM NEW.request_id
                OR (NEW.payload->>'binding_revision')::bigint IS DISTINCT FROM NEW.binding_revision
                OR NEW.payload->>'purpose' IS DISTINCT FROM NEW.purpose
                OR NEW.payload->>'candidate_sha' IS DISTINCT FROM NEW.candidate_sha
                OR (NEW.payload->>'attempt_ordinal')::bigint IS DISTINCT FROM NEW.attempt_ordinal
            THEN RAISE EXCEPTION 'ordinary read payload identity mismatch'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER ordinary_read_identity BEFORE INSERT OR UPDATE OR DELETE ON launchplane_ordinary_agent_read_attempts FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_read_identity_guard()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION launchplane_ordinary_provider_wait_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ordinary provider wait history is permanent'; END IF;
            IF TG_OP = 'UPDATE' AND (NEW.quota_key_sha256 IS DISTINCT FROM OLD.quota_key_sha256
                OR NEW.payload->'quota_key' IS DISTINCT FROM OLD.payload->'quota_key'
                OR NEW.retry_not_before < OLD.retry_not_before)
            THEN RAISE EXCEPTION 'ordinary provider wait cannot move backwards'; END IF;
            IF (NEW.payload->>'retry_not_before')::bigint IS DISTINCT FROM NEW.retry_not_before
            THEN RAISE EXCEPTION 'ordinary provider wait payload mismatch'; END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER ordinary_provider_wait BEFORE INSERT OR UPDATE OR DELETE ON launchplane_ordinary_agent_provider_waits FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_provider_wait_guard()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION launchplane_ordinary_append_only_guard() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'ordinary dispatch evidence is append-only'; END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_semantic_dispatches FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_semantic_outcomes FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_effect_reconciliations FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_effect_completions FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_effect_custody FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_read_custody FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_read_outcomes FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_candidate_check_observations FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )
    op.execute(
        "CREATE TRIGGER ordinary_append_only BEFORE UPDATE OR DELETE ON launchplane_ordinary_agent_landing_bindings FOR EACH ROW EXECUTE FUNCTION launchplane_ordinary_append_only_guard()"
    )


def downgrade() -> None:
    op.drop_table("launchplane_ordinary_agent_landing_bindings")
    op.drop_table("launchplane_ordinary_agent_landing_preparations")
    op.drop_table("launchplane_ordinary_agent_candidate_check_observations")
    op.drop_table("launchplane_ordinary_agent_read_outcomes")
    op.drop_table("launchplane_ordinary_agent_read_custody")
    op.drop_table("launchplane_ordinary_agent_read_attempts")
    op.drop_table("launchplane_ordinary_agent_job_claims")
    op.drop_table("launchplane_ordinary_agent_provider_waits")
    op.drop_table("launchplane_ordinary_agent_effect_custody")
    op.drop_table("launchplane_ordinary_agent_effect_completions")
    op.drop_table("launchplane_ordinary_agent_effect_reconciliations")
    op.drop_table("launchplane_ordinary_agent_semantic_outcomes")
    op.drop_table("launchplane_ordinary_agent_semantic_dispatches")
    op.drop_table("launchplane_ordinary_agent_effects")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS launchplane_ordinary_landing_identity_guard()")
        op.execute("DROP FUNCTION IF EXISTS launchplane_ordinary_effect_identity_guard()")
        op.execute("DROP FUNCTION IF EXISTS launchplane_ordinary_append_only_guard()")
        op.execute("DROP FUNCTION IF EXISTS launchplane_ordinary_read_identity_guard()")
        op.execute("DROP FUNCTION IF EXISTS launchplane_ordinary_provider_wait_guard()")
