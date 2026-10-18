"""Drop consumer-free delegated-delivery and older unmodeled history tables.

Prepared contract migration for #2437. Hosted startup stays on down_revision
until shared-data/release approval. Take and rehearse a full database backup
before applying: downgrade cannot recover these historical rows.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "e7c1d5e9a3b6"
down_revision: str = "e6b0c4d8f2a5"
branch_labels: str | None = None
depends_on: str | None = None

RETIRED_TABLES = (
    "launchplane_authorization_bootstrap",
    "launchplane_authorization_recovery_audits",
    "launchplane_authorization_recovery_challenges",
    "launchplane_authorization_recovery_keys",
    "launchplane_ordinary_agent_candidate_check_observations",
    "launchplane_ordinary_agent_credential_custody",
    "launchplane_ordinary_agent_effect_completions",
    "launchplane_ordinary_agent_effect_reconciliations",
    "launchplane_ordinary_agent_job_claims",
    "launchplane_ordinary_agent_landing_bindings",
    "launchplane_ordinary_agent_landing_preparations",
    "launchplane_ordinary_agent_lifecycle_audits",
    "launchplane_ordinary_agent_no_op_landing_finalizations",
    "launchplane_ordinary_agent_provider_waits",
    "launchplane_ordinary_agent_read_attempts",
    "launchplane_ordinary_agent_read_custody",
    "launchplane_ordinary_agent_read_outcomes",
    "launchplane_ordinary_agent_semantic_dispatches",
    "launchplane_ordinary_agent_semantic_outcomes",
    "launchplane_ordinary_agent_session_operations",
    "launchplane_product_owner_requirement_authority_migrations",
    "launchplane_provider_delivery_inspections",
)


def upgrade() -> None:
    connection = op.get_bind()
    # Lock every existing candidate before the first DROP. RESTRICT (the
    # default) refuses an unexpected dependent object; never cascade into a
    # retained table, view or fence.
    existing = set(sa.inspect(connection).get_table_names())
    selected = tuple(name for name in RETIRED_TABLES if name in existing)
    if connection.dialect.name == "postgresql" and selected:
        names = ", ".join(connection.dialect.identifier_preparer.quote(name) for name in selected)
        connection.execute(sa.text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE NOWAIT"))
    for name in selected:
        op.drop_table(name)
    if connection.dialect.name == "postgresql":
        for name in (
            "launchplane_ordinary_landing_identity_guard",
            "launchplane_ordinary_read_identity_guard",
            "launchplane_ordinary_provider_wait_guard",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    # ordinary_effect_identity_guard and ordinary_append_only_guard still
    # protect retained effect/custody/activation history.


def downgrade() -> None:
    raise RuntimeError(
        "Retired delivery history cannot be recovered by downgrade; "
        "restore the verified pre-migration database backup with its matching image."
    )
