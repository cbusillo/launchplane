"""Fixed, value-free Odoo copy repair, shared by maintenance and its readback.

This program runs in the existing lane script-runner, never on the workstation.
It uses Odoo's database.is_neutralized contract for newly created IAP accounts.
It deliberately does not read, compare or retain the source database's secrets.
"""

from typing import Literal

OdooConnectionIsolationMode = Literal["dry-run", "apply", "readback"]

# Invalid URI schemes make requests reject the URL before opening a connection,
# including for an account recreated by an addon or a synchronous SMS call.
CONNECTION_ISOLATION_PARAMETERS = {
    "database.is_neutralized": "true",
    "sms.endpoint": "disabled://sms",
    "iap.endpoint": "disabled://iap",
}

# Discover only these known carrier credential columns. Missing optional carrier
# modules are normal. Unknown columns are not accepted from a caller or record.
CARRIER_CREDENTIAL_COLUMNS = (
    "usps_username",
    "usps_password",
    "usps_account_validated",
    "usps_rest_api_key",
    "usps_rest_api_secret",
    "usps_rest_account_number",
    "ups_username",
    "ups_passwd",
    "ups_shipper_number",
    "ups_access_number",
    "ups_client_id",
    "ups_client_secret",
    "ups_account_number",
    "fedex_developer_key",
    "fedex_developer_password",
    "fedex_account_number",
    "fedex_meter_number",
    "dhl_site_id",
    "dhl_password",
    "dhl_account_number",
    "easypost_api_key",
    "sendcloud_public_key",
    "sendcloud_secret_key",
    "shiprocket_email",
    "shiprocket_password",
    "starshipit_api_key",
    "starshipit_subscription_key",
)

CONNECTION_ISOLATION_LIBRARY = r"""
def isolation_columns(cursor, table):
    cursor.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    )
    return {row[0] for row in cursor.fetchall()}


def connection_isolation(cursor, *, apply):
    parameters = ISOLATION_PARAMETERS
    if apply:
        for key, value in parameters.items():
            cursor.execute(
                "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )
    cursor.execute(
        "SELECT count(*) FROM ir_config_parameter WHERE (key, value) IN "
        "((%s, %s), (%s, %s), (%s, %s))",
        tuple(item for pair in parameters.items() for item in pair),
    )
    refused = []
    if cursor.fetchone()[0] != len(parameters):
        refused.append("customer_sms/isolation_parameters")

    company_columns = isolation_columns(cursor, "res_company")
    if "stock_move_sms_validation" in company_columns:
        if apply:
            cursor.execute("UPDATE res_company SET stock_move_sms_validation = false")
        cursor.execute("SELECT EXISTS (SELECT 1 FROM res_company WHERE stock_move_sms_validation)")
        if cursor.fetchone()[0]:
            refused.append("customer_sms/res_company")

    if "account_token" in isolation_columns(cursor, "iap_account"):
        if apply:
            # Remove the original token, rather than retaining it before a suffix.
            cursor.execute("UPDATE iap_account SET account_token = 'dummy_value+disabled'")
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM iap_account"
            " WHERE coalesce(account_token, '') <> '' AND account_token NOT LIKE '%%+disabled')"
        )
        if cursor.fetchone()[0]:
            refused.append("iap/iap_account")

    columns = isolation_columns(cursor, "delivery_carrier")
    if columns:
        if not {"delivery_type", "active", "prod_environment"} <= columns:
            raise RuntimeError("Unsupported delivery carrier schema")
        if apply:
            cursor.execute("UPDATE delivery_carrier SET prod_environment = false")
            cursor.execute(
                "UPDATE delivery_carrier SET active = false"
                " WHERE delivery_type NOT IN ('fixed', 'base_on_rule')"
            )
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM delivery_carrier WHERE prod_environment"
            " OR (active AND delivery_type NOT IN ('fixed', 'base_on_rule')))"
        )
        if cursor.fetchone()[0]:
            refused.append("carrier/delivery_carrier")
        for column in CARRIER_COLUMNS:
            if column not in columns:
                continue
            if apply:
                cursor.execute('UPDATE delivery_carrier SET "' + column + '" = NULL')
            cursor.execute(
                'SELECT EXISTS (SELECT 1 FROM delivery_carrier WHERE "' + column
                + '" IS NOT NULL AND btrim("' + column + '"::text) NOT IN (\'\', \'false\'))'
            )
            if cursor.fetchone()[0]:
                refused.append("carrier/" + column)
    return refused
"""


def connection_isolation_library() -> str:
    return (
        f"ISOLATION_PARAMETERS = {CONNECTION_ISOLATION_PARAMETERS!r}\n"
        f"CARRIER_COLUMNS = {CARRIER_CREDENTIAL_COLUMNS!r}\n" + CONNECTION_ISOLATION_LIBRARY
    )


CONNECTION_ISOLATION_PROGRAM = (
    """import os
import sys
import psycopg2
"""
    + connection_isolation_library()
    + """
database, instance, mode = sys.argv[1:]
if instance.strip().lower() in {"prod", "production"}:
    raise SystemExit("Connection isolation refuses production")
if os.environ.get("PLATFORM_INSTANCE", "").strip().lower() in {"prod", "production"}:
    raise SystemExit("Connection isolation refuses a production runner")
if os.environ.get("ODOO_DB_NAME") and os.environ["ODOO_DB_NAME"] != database:
    raise SystemExit("Connection isolation database identity mismatch")
if mode not in {"dry-run", "apply", "readback"}:
    raise SystemExit("Unsupported isolation mode")
connection = psycopg2.connect(
    host=os.environ.get("ODOO_DB_HOST") or "database",
    port=os.environ.get("ODOO_DB_PORT") or "5432",
    user=os.environ.get("ODOO_DB_USER") or "odoo",
    password=os.environ.get("ODOO_DB_PASSWORD") or "",
    dbname=database,
)
try:
    with connection.cursor() as cursor:
        if mode != "apply":
            cursor.execute("SET TRANSACTION READ ONLY")
        refused = connection_isolation(cursor, apply=mode == "apply")
        if mode == "apply":
            if refused:
                raise RuntimeError("Connection isolation did not pass")
            connection.commit()
        else:
            connection.rollback()
    print("connection_isolation_ok=" + ("false" if refused else "true"))
    if refused:
        print("integration_readback_refused=" + ",".join(refused))
    if refused and mode == "readback":
        raise SystemExit(1)
finally:
    connection.close()
"""
)
