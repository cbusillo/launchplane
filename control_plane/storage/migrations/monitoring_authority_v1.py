from sqlalchemy import Column, Index, Integer, JSON, MetaData, String, Table, inspect, text
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
    sqlite_autoincrement=True,
)
Index("monitoring_authority_scope_idx", history.c.product, history.c.sequence)
SOURCES = {
    "launchplane_product_profiles": ("product", "''", "''"),
    "launchplane_route_bindings": ("product", "context", "instance"),
    "launchplane_private_health_endpoints": ("product", "context", "instance"),
}


def _object(postgres: bool, value: str, fields: tuple[str, ...]) -> str:
    function = "jsonb_build_object" if postgres else "json_object"
    return (
        function
        + "("
        + ", ".join(
            f"'{field}', {value}->'{field}'"
            if postgres
            else f"'{field}', json_extract({value}, '$.{field}')"
            for field in fields
        )
        + ")"
    )


def _projection(postgres: bool, source: str, value: str) -> str:
    if source == "launchplane_product_profiles":
        profile = _object(postgres, value, ("lifecycle_state", "production_use", "public_website"))
        lane_fields = ("context", "instance", "base_url", "health_url", "health_monitoring")
        if postgres:
            lanes = _object(True, "lane", lane_fields)
            return (
                profile
                + " || jsonb_build_object('lanes', coalesce((SELECT jsonb_agg("
                + lanes
                + ") FROM jsonb_array_elements("
                + value
                + "->'lanes') lane), '[]'::jsonb))"
            )
        lanes = _object(False, "lane.value", lane_fields)
        return (
            "json_set("
            + profile
            + ", '$.lanes', json((SELECT json_group_array("
            + lanes
            + ") FROM json_each("
            + value
            + ", '$.lanes') lane)))"
        )
    fields = (
        ("status", "domains", "external_ingress")
        if source == "launchplane_route_bindings"
        else ("endpoint_key", "status")
    )
    return _object(postgres, value, fields)


def initialize(connection: Connection) -> None:
    postgres = connection.dialect.name == "postgresql"
    existing = TABLE_NAME in inspect(connection).get_table_names()
    if existing:
        return
    if postgres:
        connection.execute(
            text("LOCK TABLE " + ", ".join(SOURCES) + " IN SHARE ROW EXCLUSIVE MODE")
        )
    metadata.create_all(connection)
    timestamp = (
        "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
        if postgres
        else "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
    )
    recorded_at = connection.scalar(text("SELECT " + timestamp))
    connection.execute(
        history.insert().values(
            recorded_at=recorded_at,
            source="",
            source_key="",
            transaction_id="",
            product="",
            context="",
            instance="",
            operation="epoch",
            payload={"sources": list(SOURCES), "earlier_authority": "unknown"},
        )
    )
    for source, keys in SOURCES.items():
        identity = (
            ("endpoint_key",)
            if source == "launchplane_private_health_endpoints"
            else tuple(key for key in keys if key != "''")
        )
        key = ("jsonb_build_array" if postgres else "json_array") + "(" + ", ".join(identity) + ")"
        if postgres:
            key += "::text"
        projection = _projection(postgres, source, "payload")
        connection.execute(
            text(
                f"INSERT INTO {TABLE_NAME} "
                "(recorded_at, source, source_key, transaction_id, product, context, instance, operation, payload) "
                f"SELECT :recorded_at, :source, {key}, '', {', '.join(keys)}, 'seed', {projection} FROM {source}"
            ),
            {"recorded_at": recorded_at, "source": source},
        )
    if postgres:
        connection.execute(
            text(f"""
            CREATE FUNCTION launchplane_monitoring_authority_capture_v1() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE captured jsonb; projected jsonb;
            BEGIN
                IF TG_OP = 'DELETE' THEN captured := to_jsonb(OLD);
                ELSE captured := to_jsonb(NEW); END IF;
                projected := {_projection(True, "launchplane_product_profiles", "captured->'payload'")};
                IF TG_TABLE_NAME = 'launchplane_route_bindings' THEN
                    projected := {_projection(True, "launchplane_route_bindings", "captured->'payload'")};
                ELSIF TG_TABLE_NAME = 'launchplane_private_health_endpoints' THEN
                    projected := {_projection(True, "launchplane_private_health_endpoints", "captured->'payload'")};
                END IF;
                IF TG_OP = 'UPDATE' AND OLD.payload::jsonb = NEW.payload::jsonb THEN
                    RETURN NULL;
                END IF;
                INSERT INTO {TABLE_NAME}
                    (recorded_at, source, source_key, transaction_id, product, context, instance, operation, payload)
                VALUES ({timestamp}, TG_TABLE_NAME, (CASE WHEN TG_TABLE_NAME = 'launchplane_private_health_endpoints'
                            THEN jsonb_build_array(captured->>'endpoint_key')
                            WHEN TG_TABLE_NAME = 'launchplane_product_profiles'
                            THEN jsonb_build_array(captured->>'product')
                            ELSE jsonb_build_array(captured->>'product', captured->>'context', captured->>'instance') END)::text,
                        pg_current_xact_id()::text, captured->>'product',
                        coalesce(captured->>'context', ''), coalesce(captured->>'instance', ''),
                        lower(TG_OP), projected);
                RETURN NULL;
            END $$
        """)
        )
    for source, keys in SOURCES.items():
        trigger = source + "_monitoring_capture"
        if postgres:
            connection.execute(
                text(
                    f"CREATE TRIGGER {trigger} AFTER INSERT OR UPDATE OR DELETE ON {source} "
                    "FOR EACH ROW EXECUTE FUNCTION launchplane_monitoring_authority_capture_v1()"
                )
            )
            continue
        for operation in ("insert", "update", "delete"):
            row = "OLD" if operation == "delete" else "NEW"
            identifiers = ", ".join(key if key == "''" else f"{row}.{key}" for key in keys)
            condition = " WHEN OLD.payload != NEW.payload" if operation == "update" else ""
            identity = (
                ("endpoint_key",)
                if source == "launchplane_private_health_endpoints"
                else tuple(key for key in keys if key != "''")
            )
            key = "json_array(" + ", ".join(f"{row}.{key}" for key in identity) + ")"
            projection = _projection(False, source, f"{row}.payload")
            connection.execute(
                text(f"""
                CREATE TRIGGER {trigger}_{operation} AFTER {operation} ON {source}{condition}
                BEGIN
                    INSERT INTO {TABLE_NAME}
                        (recorded_at, source, source_key, transaction_id, product, context, instance, operation, payload)
                    VALUES ({timestamp}, '{source}', {key}, '', {identifiers}, '{operation}', {projection});
                END
            """)
            )
    if postgres:
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
    else:
        for operation in ("update", "delete"):
            connection.execute(
                text(f"""
                CREATE TRIGGER monitoring_history_immutable_{operation}
                BEFORE {operation} ON {TABLE_NAME} BEGIN
                    SELECT RAISE(ABORT, 'Monitoring authority history is append-only');
                END
            """)
            )
