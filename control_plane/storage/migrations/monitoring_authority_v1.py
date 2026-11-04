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
    Column("product", String, nullable=False),
    Column("context", String, nullable=False),
    Column("instance", String, nullable=False),
    Column("operation", String, nullable=False),
    Column("payload", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    sqlite_autoincrement=True,
    info={"product_scope_authority": False},
)
Index("monitoring_authority_scope_idx", history.c.product, history.c.sequence)
SOURCES = {
    "launchplane_product_profiles": ("product", "''", "''"),
    "launchplane_route_bindings": ("product", "context", "instance"),
    "launchplane_private_health_endpoints": ("product", "context", "instance"),
}


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
            product="",
            context="",
            instance="",
            operation="epoch",
            payload={"sources": list(SOURCES), "earlier_authority": "unknown"},
        )
    )
    for source, keys in SOURCES.items():
        connection.execute(
            text(
                f"INSERT INTO {TABLE_NAME} "
                "(recorded_at, source, product, context, instance, operation, payload) "
                f"SELECT :recorded_at, :source, {', '.join(keys)}, 'seed', payload FROM {source}"
            ),
            {"recorded_at": recorded_at, "source": source},
        )
    if postgres:
        connection.execute(
            text(f"""
            CREATE FUNCTION launchplane_monitoring_authority_capture_v1() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE captured jsonb;
            BEGIN
                IF TG_OP = 'DELETE' THEN captured := to_jsonb(OLD);
                ELSE captured := to_jsonb(NEW); END IF;
                IF TG_OP = 'UPDATE' AND OLD.payload::jsonb = NEW.payload::jsonb THEN
                    RETURN NULL;
                END IF;
                INSERT INTO {TABLE_NAME}
                    (recorded_at, source, product, context, instance, operation, payload)
                VALUES ({timestamp}, TG_TABLE_NAME, captured->>'product',
                        coalesce(captured->>'context', ''), coalesce(captured->>'instance', ''),
                        lower(TG_OP), captured->'payload');
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
            connection.execute(
                text(f"""
                CREATE TRIGGER {trigger}_{operation} AFTER {operation} ON {source}{condition}
                BEGIN
                    INSERT INTO {TABLE_NAME}
                        (recorded_at, source, product, context, instance, operation, payload)
                    VALUES ({timestamp}, '{source}', {identifiers}, '{operation}', {row}.payload);
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
