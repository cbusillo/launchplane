from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.clean_testing_snapshot import preview_snapshot
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.native_copy_stream import (
    inspect_native_testing_receiver,
    NativeSourceAuthorizationDenied,
    NativeProgramFailed,
)
from control_plane.integration_readback import (
    INTEGRATION_READBACK_LIBRARY,
    IntegrationReadbackPolicy,
    INTEGRATION_FAMILIES,
)
from control_plane.launchplane_mutations import control_plane_root
from control_plane.odoo_runtime_reads import (
    select_stable_runtime,
    assert_selection_current,
    OdooRuntimeReadError,
)
from control_plane.odoo_testing_drill_usage import testing_drill_environment
from control_plane.odoo_testing_restore import (
    read_current_testing_generation,
    generation_binding,
    require_distinct_testing_credentials,
)
from control_plane.storage.postgres import PostgresRecordStore


ROUTE = "/v1/products/{product}/environments/{environment}/aggregate-counts"
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$", max_length=63)]
Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class AggregateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: Identifier
    key: Identifier
    partition: Identifier | None = None
    active: Identifier = "active"


class AggregateCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_rows: Count
    duplicate_groups: Count
    duplicate_rows: Count
    missing_key_rows: Count


class AggregateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    instance: Literal["testing"] = "testing"
    generation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_database_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_captured_at: str
    source_mode: Literal["ssh", "container-export"]
    source_binding_sha256: str
    operation_id: str
    observed_at: str
    sanitization_verified: Literal[True] = True
    resources_isolated: Literal[True] = True
    read_only: Literal[True] = True
    counts: AggregateCounts


AGGREGATE_QUERY_PROGRAM = r"""
import base64,json,os,re,sys
import psycopg2
from psycopg2 import sql

spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
query = spec['aggregate_query']
if set(query) != {'table','key','partition','active'} or any(
    not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value)
    for key,value in query.items() if key != 'partition' or value is not None):
    raise RuntimeError('Invalid aggregate identifiers')
if os.environ.get('PLATFORM_INSTANCE') != 'testing' or os.environ.get('ODOO_DB_HOST') != 'database':
    raise RuntimeError('Aggregate copy endpoint is not local testing')
connection = psycopg2.connect(
    host='database', port='5432', dbname=spec['database_name'],
    user=os.environ['ODOO_DB_USER'], password=os.environ['ODOO_DB_PASSWORD'],
    connect_timeout=5, options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000 -c search_path=pg_catalog,public')
try:
    connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
    with connection.cursor() as cursor:
        cursor.execute('SHOW transaction_read_only')
        if cursor.fetchone() != ('on',):
            raise RuntimeError('Aggregate transaction is not read only')
        cursor.execute('SELECT value FROM public.ir_config_parameter WHERE key=%s', (spec['witness_key'],))
        if cursor.fetchall() != [(spec['copy_binding_sha256'],)]:
            raise RuntimeError('Aggregate copy witness changed')
        refused, allowed, checked = read_integration_settings(cursor, spec['readback'])
        if refused or allowed or not checked:
            raise RuntimeError('Aggregate copy is not sanitized')
        cursor.execute("SELECT c.relkind, c.relrowsecurity FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=%s", (query['table'],))
        if cursor.fetchall() != [('r', False)]:
            raise RuntimeError('Aggregates require a local base table without row policies')
        cursor.execute("SELECT a.attname, t.typname FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type t ON t.oid=a.atttypid JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped AND n.nspname='pg_catalog'", ('public.'+query['table'],))
        columns = dict(cursor.fetchall())
        text_types = {'text','varchar','bpchar'}
        if (columns.get(query['key']) not in text_types or columns.get(query['active']) != 'bool'
            or query['partition'] is not None and columns.get(query['partition']) not in text_types | {'int2','int4','int8'}):
            raise RuntimeError('Aggregate column types are unsupported')
        table = sql.Identifier('public', query['table'])
        key = sql.SQL("lower(btrim(coalesce({}::text, '')))").format(sql.Identifier(query['key']))
        active = sql.Identifier(query['active'])
        cursor.execute(sql.SQL("SELECT count(*) FILTER (WHERE {} IS TRUE), count(*) FILTER (WHERE {} = '') FROM ONLY {}").format(active,key,table))
        active_rows, missing_rows = cursor.fetchone()
        grouping = key if query['partition'] is None else sql.SQL('{}, {}').format(key,sql.Identifier(query['partition']))
        cursor.execute(sql.SQL("SELECT count(*), coalesce(sum(n), 0) FROM (SELECT count(*) n FROM ONLY {} WHERE {} <> '' GROUP BY {} HAVING count(*) > 1) groups").format(table,key,grouping))
        groups, duplicates = cursor.fetchone()
        result = {'active_rows': int(active_rows), 'duplicate_groups': int(groups),
            'duplicate_rows': int(duplicates), 'missing_key_rows': int(missing_rows)}
finally:
    connection.rollback()
    connection.close()
print(json.dumps(result))
"""

AGGREGATE_PROGRAM = INTEGRATION_READBACK_LIBRARY + AGGREGATE_QUERY_PROGRAM


def read_aggregates(
    *, store: PostgresRecordStore, product: str, query: AggregateQuery
) -> AggregateEvidence:
    profile = store.read_product_profile_record(product)
    if not profile.is_active or profile.driver_id != "odoo":
        raise ValueError("Aggregate reads require an active Odoo testing copy.")
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    if lane.odoo_data_policy.data_authority != "resettable":
        raise ValueError("Aggregate reads require resettable copy custody.")
    snapshot = preview_snapshot(store, profile)
    require_distinct_testing_credentials(store, profile, lane.context)
    generation = read_current_testing_generation(
        store, product=product, context=lane.context, instance="testing", read_only_inspection=True
    )
    if (
        generation is None
        or not generation.completed_at
        or snapshot.snapshot_id != generation_binding(generation)
    ):
        raise ValueError("The current copy lacks its completed clean snapshot provenance.")
    selection = select_stable_runtime(store, profile, "testing")
    target_id = store.read_dokploy_target_id_record(
        context_name=lane.context, instance_name="testing"
    )
    if target_id.target_id != generation.target_id:
        raise ValueError("The qualified copy no longer owns this target.")
    host, token = source.read_dokploy_config(
        control_plane_root=control_plane_root(), database_url=store.database_url
    )
    provider = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id.target_id
    )
    if provider.get("serverId") != snapshot.server_id:
        raise ValueError("The current copy differs from its snapshot server.")
    environment = testing_drill_environment(store, lane.context)
    readback = json.loads(
        base64.b64decode(
            IntegrationReadbackPolicy(
                families=INTEGRATION_FAMILIES,
                allowed_integrations=(),
                protected_shopify_store_handles=(),
            ).encoded_spec()
        )
    )
    frames: list[dict[str, object]] = []
    try:
        inspect_native_testing_receiver(
            host=host,
            token=token,
            server_id=snapshot.server_id,
            spec={
                "generation": generation.model_dump(mode="json"),
                "compose_project": str(provider.get("appName") or ""),
                "image_reference": selection.identity.image_reference,
                "database_name": environment.get("ODOO_DB_NAME", ""),
                "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
                "copy_binding_sha256": generation_binding(generation),
                "workflow_environment": {},
                "usage_only": True,
                "aggregate_query": query.model_dump(mode="json"),
                "aggregate_program": AGGREGATE_PROGRAM,
                "readback": readback,
            },
            receive=frames.append,
        )
    except NativeSourceAuthorizationDenied:
        raise OdooRuntimeReadError(
            "authorization_denied", "The provider refused the qualified copy aggregate read.", 403
        ) from None
    except NativeProgramFailed as error:
        raise OdooRuntimeReadError(
            error.code, "The copy aggregate qualification or query did not complete.", 503
        ) from None
    assert_selection_current(store, selection)
    current = read_current_testing_generation(
        store, product=product, context=lane.context, instance="testing", read_only_inspection=True
    )
    if current != generation or store.read_product_profile_record(product) != profile:
        raise ValueError("Aggregate copy authority changed during inspection.")
    if len(frames) != 1 or set(frames[0]) != {"type", "counts"} or frames[0]["type"] != "complete":
        raise ValueError("Aggregate copy qualification or query did not complete.")
    return AggregateEvidence(
        product=product,
        context=lane.context,
        generation_sha256=generation_binding(generation),
        snapshot_id=snapshot.snapshot_id,
        snapshot_database_sha256=snapshot.database_sha256,
        snapshot_captured_at=snapshot.captured_at,
        source_mode=generation.source_mode,
        source_binding_sha256=generation.source_binding_sha256,
        operation_id=generation.operation_id,
        observed_at=datetime.now(timezone.utc).isoformat(),
        counts=AggregateCounts.model_validate(frames[0]["counts"]),
    )
