import configparser
import importlib
import json
import os
import re
import sys

from control_plane.odoo_instance_overrides import shopify_store_handle
from control_plane.odoo_shopify_identity_probe import read_identity as read_shopify_identity


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
            keys = keys + ["shopify.api_token", "shopify.api_version"]
            cursor.execute(
                "SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)", (keys,)
            )
            rows = cursor.fetchmany(len(keys) + 1)
        if not rows or len(rows) > len(keys):
            raise ValueError("Store identity unavailable")
        domains = set()
        values: dict[str, str] = {}
        for key, value in rows:
            if key not in keys or not isinstance(value, str) or not value.strip():
                raise ValueError("Store identity unavailable")
            if key in values:
                raise ValueError("Store identity unavailable")
            values[key] = value
            if key in {"shopify.api_token", "shopify.api_version"}:
                continue
            domain = shopify_store_handle(value) + ".myshopify.com"
            if (
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com", domain)
                is None
            ):
                raise ValueError("Store identity unavailable")
            domains.add(domain)
        if len(domains) != 1:
            raise ValueError("Store identity unavailable")
        result = read_shopify_identity(
            {
                "domain": next(iter(domains)),
                "api_version": values.get("shopify.api_version", ""),
                "token": values.get("shopify.api_token", ""),
            },
            require_domain_match=False,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)", (keys,)
            )
            after = cursor.fetchmany(len(keys) + 1)
        if sorted(after) != sorted(rows):
            raise ValueError("Production settings changed")
        return result

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
