import configparser
import importlib
import json
import os
import re
import sys

from control_plane.odoo_instance_overrides import shopify_store_handle
from control_plane.odoo_shopify_identity_probe import read_identity as read_shopify_identity


class IdentityUnavailable(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def read_identity(spec: dict[str, object]) -> dict[str, str]:
    config = configparser.ConfigParser(interpolation=None)
    path = str(spec["config_path"])
    try:
        if config.read(path) != [path]:
            raise IdentityUnavailable("configuration_unavailable")
        options = config["options"]
    except (OSError, configparser.Error, KeyError):
        raise IdentityUnavailable("configuration_unavailable") from None
    database = os.environ.get("ODOO_DB_NAME")
    if not database or options.get("db_name") != database or options.get("db_host") != "database":
        raise IdentityUnavailable("database_identity_unavailable")
    if not options.get("db_user") or not options.get("db_password"):
        raise IdentityUnavailable("configuration_unavailable")
    try:
        driver = importlib.import_module("psycopg2")
    except ImportError:
        raise IdentityUnavailable("database_driver_unavailable") from None
    try:
        connection = driver.connect(
            host="database",
            port=options.get("db_port") or "5432",
            user=options["db_user"],
            password=options["db_password"],
            dbname=database,
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=3000",
        )
    except Exception:
        raise IdentityUnavailable("database_unavailable") from None
    try:
        with connection.cursor() as cursor:
            keys = spec["store_keys"]
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                raise IdentityUnavailable("settings_unavailable")
            keys = keys + ["shopify.api_token", "shopify.api_version"]
            cursor.execute(
                "SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)", (keys,)
            )
            rows = cursor.fetchmany(len(keys) + 1)
        if not rows or len(rows) > len(keys):
            raise IdentityUnavailable("settings_unavailable")
        domains = set()
        values: dict[str, str] = {}
        for key, value in rows:
            if key not in keys or not isinstance(value, str) or not value.strip():
                raise IdentityUnavailable("settings_unavailable")
            if key in values:
                raise IdentityUnavailable("settings_unavailable")
            values[key] = value
            if key in {"shopify.api_token", "shopify.api_version"}:
                continue
            domain = shopify_store_handle(value) + ".myshopify.com"
            if (
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com", domain)
                is None
            ):
                raise IdentityUnavailable("settings_unavailable")
            domains.add(domain)
        if len(domains) != 1:
            raise IdentityUnavailable("settings_unavailable")
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
            raise IdentityUnavailable("settings_changed")
        return result
    except IdentityUnavailable:
        raise
    except Exception:
        raise IdentityUnavailable("database_unavailable") from None
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def main() -> None:
    try:
        spec = json.loads(sys.stdin.read(4097))
        if not isinstance(spec, dict) or not isinstance(spec.get("config_path"), str):
            raise ValueError()
    except (ValueError, TypeError):
        print(json.dumps({"status": "unavailable", "reason": "request_invalid"}))
        return
    try:
        result = read_identity(spec)
    except IdentityUnavailable as error:
        result = {"status": "unavailable", "reason": error.reason}
    except Exception:
        result = {"status": "unavailable", "reason": "probe_failed"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
