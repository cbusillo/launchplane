"""Fence public-host effects without a global authority transaction over I/O."""

from alembic import op
import sqlalchemy as sa

revision: str = "cd5367f3e003"
down_revision: str = "bc4256e2d002"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = (
    "launchplane_product_profiles",
    "launchplane_dokploy_targets",
    "launchplane_dokploy_target_ids",
    "launchplane_provider_targets",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text("""
        CREATE FUNCTION launchplane_public_host_authority_keys(kind text, record jsonb)
        RETURNS SETOF text LANGUAGE sql IMMUTABLE AS $$
            SELECT 'launchplane:public-host-authority:' || resource::text
            FROM (
                SELECT jsonb_build_array('context', lower(btrim(record->>'context'))) AS resource
                WHERE kind <> 'launchplane_product_profiles' AND record->>'context' IS NOT NULL
                UNION
                SELECT jsonb_build_array('context', lower(btrim(lane->>'context')))
                FROM jsonb_array_elements(COALESCE(record->'lanes', '[]'::jsonb)) AS lane
                WHERE kind = 'launchplane_product_profiles'
                UNION
                SELECT jsonb_build_array('context', lower(btrim(context)))
                FROM jsonb_array_elements_text(
                    COALESCE(record->'historical_contexts', '[]'::jsonb)
                ) AS context
                WHERE kind = 'launchplane_product_profiles'
                UNION
                SELECT jsonb_build_array('host', lower(host))
                FROM jsonb_array_elements_text(
                    COALESCE(record->'domains', '[]'::jsonb) ||
                    COALESCE(record->'public_hosts', '[]'::jsonb)
                ) AS host
                WHERE kind = 'launchplane_dokploy_targets'
                UNION
                SELECT jsonb_build_array('provider-target',
                    CASE WHEN kind = 'launchplane_dokploy_target_ids'
                         THEN 'dokploy' ELSE record->>'provider_id' END,
                    record->>'target_id')
                WHERE kind IN ('launchplane_dokploy_target_ids', 'launchplane_provider_targets')
            ) AS resources
            ORDER BY resource::text
        $$
    """)
    )
    op.execute(
        sa.text("""
        CREATE FUNCTION launchplane_lock_public_host_authority(kind text, record jsonb)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE resource text;
        BEGIN
            FOR resource IN SELECT launchplane_public_host_authority_keys(kind, record) LOOP
                IF NOT pg_try_advisory_xact_lock(hashtextextended(resource, 0)) THEN
                    RAISE EXCEPTION 'Public-host authority is being reconciled; review a fresh dry-run.'
                        USING ERRCODE = '55P03', CONSTRAINT = 'launchplane_public_hosts_fence';
                END IF;
            END LOOP;
        END;
        $$
    """)
    )
    op.execute(
        sa.text("""
        CREATE FUNCTION launchplane_public_host_authority_write_fence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                PERFORM launchplane_lock_public_host_authority(TG_TABLE_NAME, OLD.payload::jsonb);
            END IF;
            IF TG_OP <> 'DELETE' THEN
                PERFORM launchplane_lock_public_host_authority(TG_TABLE_NAME, NEW.payload::jsonb);
                RETURN NEW;
            END IF;
            RETURN OLD;
        END;
        $$
    """)
    )
    for table in _TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER launchplane_public_host_authority_write_fence "
                f"BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION launchplane_public_host_authority_write_fence()"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(
            sa.text(f"DROP TRIGGER launchplane_public_host_authority_write_fence ON {table}")
        )
    op.execute(sa.text("DROP FUNCTION launchplane_public_host_authority_write_fence()"))
    op.execute(sa.text("DROP FUNCTION launchplane_lock_public_host_authority(text, jsonb)"))
    op.execute(sa.text("DROP FUNCTION launchplane_public_host_authority_keys(text, jsonb)"))
