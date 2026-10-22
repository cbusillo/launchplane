import configparser
import hashlib
import importlib
import json
import os
import re
import sys
from urllib.parse import urlsplit


def read_identity(spec: dict[str, object]) -> dict[str, str]:
    config = configparser.ConfigParser(interpolation=None)
    path = str(spec["config_path"])
    if config.read(path) != [path]:
        raise ValueError("Configuration unavailable")
    options = config["options"]
    database = os.environ["ODOO_DB_NAME"]
    if options.get("db_name") != database or options.get("db_host") != "database":
        raise ValueError("Database identity unavailable")
    connection = importlib.import_module("psycopg2").connect(
        host="database",
        port=options.get("db_port") or "5432",
        user=options["db_user"],
        password=options["db_password"],
        dbname=database,
        connect_timeout=3,
        options="-c default_transaction_read_only=on -c statement_timeout=3000",
    )
    try:
        with connection.cursor() as cursor:
            keys = spec["store_keys"]
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                raise ValueError("Store keys unavailable")
            cursor.execute(
                "SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)", (keys,)
            )
            rows = cursor.fetchmany(4)
        if not rows or len(rows) > 3:
            raise ValueError("Store identity unavailable")
        domains = set()
        for key, value in rows:
            if key not in keys or not isinstance(value, str) or not value.strip():
                raise ValueError("Store identity unavailable")
            raw = value.strip().lower()
            parsed = urlsplit(raw if "://" in raw else "https://" + raw)
            domain = (parsed.hostname or "").rstrip(".")
            parts = parsed.path.strip("/").split("/")
            if domain == "admin.shopify.com" and len(parts) == 2 and parts[0] == "store":
                domain = parts[1]
            if not domain.endswith(".myshopify.com"):
                domain += ".myshopify.com"
            if (
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com", domain)
                is None
            ):
                raise ValueError("Store identity unavailable")
            domains.add(domain)
        if len(domains) != 1:
            raise ValueError("Store identity unavailable")
        return {
            "status": "verified",
            "domain_sha256": hashlib.sha256(next(iter(domains)).encode()).hexdigest(),
        }
    finally:
        connection.rollback()
        connection.close()


def main() -> None:
    try:
        result = read_identity(json.loads(sys.stdin.read(4097)))
    except Exception:
        result = {"status": "unavailable"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
