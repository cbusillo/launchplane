from sqlalchemy import Column, Index, Integer, JSON, MetaData, String, Table, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

metadata = MetaData()
TABLE_NAME = "launchplane_monitoring_authority_history"
history = Table(
    TABLE_NAME,
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("recorded_at", String, nullable=False),
    Column("source", String, nullable=False),
    Column("source_key", String, nullable=False),
    Column("transaction_id", String, nullable=False),
    Column("product", String, nullable=False),
    Column("context", String, nullable=False),
    Column("instance", String, nullable=False),
    Column("operation", String, nullable=False),
    Column("payload", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
)
Index("monitoring_authority_scope_idx", history.c.product, history.c.sequence)
Index("monitoring_authority_operation_idx", history.c.operation, history.c.sequence)
Index(
    "monitoring_authority_source_idx",
    history.c.source,
    history.c.source_key,
    history.c.sequence,
    postgresql_where=text("source <> ''"),
)
INVALID_CONFIRMATION = text(
    "operation = 'confirmation' AND (payload->>'capture_valid') IS DISTINCT FROM 'true'"
)
Index("monitoring_authority_invalid_idx", history.c.sequence, postgresql_where=INVALID_CONFIRMATION)
SOURCES = {
    "launchplane_product_profiles": ("product",),
    "launchplane_route_bindings": ("product", "context", "instance"),
    "launchplane_private_health_endpoints": ("endpoint_key",),
}


def initialize(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('launchplane:product-authority-bundle', 0))"
        )
    )
    connection.execute(text("LOCK TABLE " + ", ".join(SOURCES) + " IN SHARE ROW EXCLUSIVE MODE"))
    metadata.create_all(connection, checkfirst=False)
    connection.execute(
        text("""
        CREATE FUNCTION launchplane_monitoring_projection_v1(source text, payload jsonb)
        RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
        SELECT CASE source
        WHEN 'launchplane_product_profiles' THEN jsonb_build_object(
            'schema_version', payload->'schema_version',
            'lifecycle_state', payload->'lifecycle_state',
            'production_use', payload->'production_use',
            'public_website', payload->'public_website',
            'driver_id', payload->'driver_id', 'health_path', payload->'health_path',
            'lanes', coalesce((SELECT jsonb_agg(jsonb_build_object(
                'context', lane->'context', 'instance', lane->'instance',
                'base_url', lane->'base_url', 'health_url', lane->'health_url',
                'health_monitoring', lane->'health_monitoring'))
                FROM jsonb_array_elements(payload->'lanes') lane), '[]'::jsonb))
        WHEN 'launchplane_route_bindings' THEN jsonb_build_object(
            'status', payload->'status', 'domains', payload->'domains',
            'external_ingress', payload->'external_ingress')
        WHEN 'launchplane_private_health_endpoints' THEN jsonb_build_object(
            'endpoint_key', payload->'endpoint_key', 'product', payload->'product',
            'context', payload->'context', 'instance', payload->'instance',
            'status', payload->'status', 'url', payload->'url',
            'tls_ca_pem', payload->'tls_ca_pem')
        END $$
    """)
    )
    timestamp = (
        "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
    )
    recorded_at = connection.scalar(text("SELECT " + timestamp))
    transaction_id = connection.scalar(text("SELECT pg_current_xact_id()::text"))
    connection.execute(
        history.insert().values(
            recorded_at=recorded_at,
            source="",
            source_key="",
            transaction_id=transaction_id,
            product="",
            context="",
            instance="",
            operation="epoch",
            payload={
                "sources": list(SOURCES),
                "earlier_authority": "unknown",
                "cluster_id": str(
                    connection.scalar(text("SELECT system_identifier FROM pg_control_system()"))
                ),
            },
        )
    )
    key_cases = []
    for source, keys in SOURCES.items():
        scope = (
            "product, '', ''"
            if source == "launchplane_product_profiles"
            else "product, context, instance"
        )
        key = "jsonb_build_array(" + ", ".join(keys) + ")::text"
        connection.execute(
            text(
                f"INSERT INTO {TABLE_NAME} "
                "(recorded_at, source, source_key, transaction_id, product, context, instance, operation, payload) "
                f"SELECT :recorded_at, :source, {key}, :transaction_id, {scope}, 'seed', "
                f"launchplane_monitoring_projection_v1('{source}', payload::jsonb) FROM {source}"
            ),
            {"recorded_at": recorded_at, "source": source, "transaction_id": transaction_id},
        )
        values = ", ".join(f"captured->>'{key}'" for key in keys)
        key_cases.append(f"WHEN '{source}' THEN jsonb_build_array({values})::text")
    source_key = "CASE TG_TABLE_NAME " + " ".join(key_cases) + " END"
    connection.execute(
        text(f"""
        CREATE FUNCTION launchplane_monitoring_authority_capture_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE captured jsonb; projected jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN captured := to_jsonb(OLD);
            ELSE captured := to_jsonb(NEW); END IF;
            projected := launchplane_monitoring_projection_v1(TG_TABLE_NAME, captured->'payload');
            IF TG_OP = 'UPDATE'
                AND projected IS NOT DISTINCT FROM launchplane_monitoring_projection_v1(TG_TABLE_NAME, OLD.payload::jsonb)
                AND (to_jsonb(OLD)->>'product', to_jsonb(OLD)->>'context', to_jsonb(OLD)->>'instance')
                    IS NOT DISTINCT FROM (captured->>'product', captured->>'context', captured->>'instance')
            THEN RETURN NULL; END IF;
            INSERT INTO {TABLE_NAME}
                (recorded_at, source, source_key, transaction_id, product, context, instance, operation, payload)
            VALUES ({timestamp}, TG_TABLE_NAME, {source_key}, pg_current_xact_id()::text,
                captured->>'product', coalesce(captured->>'context', ''), coalesce(captured->>'instance', ''),
                lower(TG_OP), projected);
            RETURN NULL;
        END $$
    """)
    )
    for source in SOURCES:
        connection.execute(
            text(
                f"CREATE TRIGGER {source}_monitoring_capture AFTER INSERT OR UPDATE OR DELETE ON {source} "
                "FOR EACH ROW EXECUTE FUNCTION launchplane_monitoring_authority_capture_v1()"
            )
        )
    connection.execute(
        text("""
        CREATE FUNCTION launchplane_monitoring_history_immutable_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'Monitoring authority history is append-only';
        END $$
    """)
    )
    connection.execute(
        text(
            f"CREATE TRIGGER monitoring_history_immutable BEFORE UPDATE OR DELETE ON {TABLE_NAME} "
            "FOR EACH ROW EXECUTE FUNCTION launchplane_monitoring_history_immutable_v1()"
        )
    )
