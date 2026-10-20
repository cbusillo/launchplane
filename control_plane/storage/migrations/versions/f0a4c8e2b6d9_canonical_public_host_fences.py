"""Canonical hostname fences on the approved path, independently of held drops."""

from alembic import op
import sqlalchemy as sa

from control_plane.storage.migrations.versions import (
    e8d2f6a0b4c7_drop_preview_enablement as preview,
)

revision: str = "f0a4c8e2b6d9"
down_revision: str = preview.revision
branch_labels: str | None = None
depends_on: str | None = None


def _replace_keys(*, canonical: bool) -> None:
    # Match Python str.strip(), including retained Unicode surrounding whitespace.
    # DNS case folding is ASCII; this changes identities, never stored payloads.
    whitespace = (
        r"\0009\000a\000b\000c\000d\001c\001d\001e\001f\0020\0085\00a0\1680"
        r"\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200a\2028\2029"
        r"\202f\205f\3000"
    )
    hostname = f"lower(rtrim(btrim(host, U&'{whitespace}'), '.'))" if canonical else "lower(host)"
    pending = (
        """
        UNION ALL
        SELECT effect->>'host' AS host
        FROM jsonb_array_elements(
            COALESCE(record->'pending_public_host_effects', '[]'::jsonb)
        ) AS effect
    """
        if canonical
        else ""
    )
    op.execute(
        sa.text(f"""
        CREATE OR REPLACE FUNCTION launchplane_public_host_authority_keys(kind text, record jsonb)
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
                SELECT jsonb_build_array('host', {hostname})
                FROM (
                    SELECT host
                    FROM jsonb_array_elements_text(
                        COALESCE(record->'domains', '[]'::jsonb) ||
                        COALESCE(record->'public_hosts', '[]'::jsonb)
                    ) AS host
                    {pending}
                ) AS hosts
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


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_keys(canonical=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_keys(canonical=False)
