"""Fixed, value-free Odoo copy repair, shared by maintenance and its readback.

This program runs in the existing lane script-runner, never on the workstation.
It uses Odoo's database.is_neutralized contract for newly created IAP accounts.
It deliberately does not read, compare or retain the source database's secrets.
"""

from typing import Literal
import click

OdooConnectionIsolationMode = Literal["dry-run", "apply", "readback"]


class ConnectionIsolationOutcomeUnknown(RuntimeError):
    """A provider effect started but its terminal outcome is unavailable."""


class ConnectionIsolationPreconditionFailed(click.ClickException):
    def __init__(self, reason: Literal["plan_changed", "workflow_active"]) -> None:
        self.code = "connection_isolation_" + reason
        super().__init__(
            {
                "plan_changed": "The connection isolation plan changed; run a new dry-run.",
                "workflow_active": "A data workflow is active on this lane; wait for it, then run a new dry-run.",
            }[reason]
        )


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
    "usps_api_key",
    "usps_api_secret",
    "usps_access_token",
    "usps_payment_token",
    "usps_eps_account_number",
    "usps_crid",
    "usps_mid",
    "usps_manifest_mid",
    "usps_extra_data_payment_token_request",
    "usps_extra_data_rate_request",
    "usps_extra_data_shipment_request",
    "ups_username",
    "ups_passwd",
    "ups_shipper_number",
    "ups_access_number",
    "ups_client_id",
    "ups_client_secret",
    "ups_access_token",
    "fedex_developer_key",
    "fedex_developer_password",
    "fedex_account_number",
    "fedex_meter_number",
    "fedex_rest_developer_key",
    "fedex_rest_developer_password",
    "fedex_rest_account_number",
    "fedex_rest_access_token",
    "fedex_rest_extra_data_rate_request",
    "fedex_rest_extra_data_return_request",
    "fedex_rest_extra_data_ship_request",
    "dhl_site_id",
    "dhl_password",
    "dhl_account_number",
    "dhl_api_key",
    "dhl_api_secret",
    "dhl_extra_data_rate_request",
    "dhl_extra_data_return_request",
    "dhl_extra_data_ship_request",
    "easypost_test_api_key",
    "easypost_production_api_key",
    "sendcloud_public_key",
    "sendcloud_secret_key",
    "shiprocket_email",
    "shiprocket_password",
    "shiprocket_access_token",
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
    website_columns = isolation_columns(cursor, "website")
    for column in (
        "google_analytics_key", "google_analytics_client_id", "google_analytics_client_secret",
        "google_tag_manager_key", "plausible_shared_key", "plausible_site",
    ):
        if column not in website_columns:
            continue
        if apply:
            cursor.execute('UPDATE website SET "' + column + '" = NULL')
        cursor.execute('SELECT EXISTS (SELECT 1 FROM website WHERE coalesce("' + column + '", \'\') <> \'\')')
        if cursor.fetchone()[0]:
            refused.append("analytics/" + column)
    if apply:
        cursor.execute("DELETE FROM ir_config_parameter WHERE key LIKE 'google_analytics.%%' OR key LIKE 'plausible.%%' OR key LIKE 'google_tag_manager.%%'")
    cursor.execute("SELECT EXISTS (SELECT 1 FROM ir_config_parameter WHERE (key LIKE 'google_analytics.%%' OR key LIKE 'plausible.%%' OR key LIKE 'google_tag_manager.%%') AND coalesce(value, '') <> '')")
    if cursor.fetchone()[0]:
        refused.append("analytics/ir_config_parameter")
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
                print("connection_isolation_ok=false")
                print("integration_readback_refused=" + ",".join(refused))
                raise RuntimeError("Connection isolation did not pass")
            connection.commit()
        else:
            connection.rollback()
    print("connection_isolation_ok=" + ("false" if refused else "true"))
    if refused and mode != "dry-run":
        print("integration_readback_refused=" + ",".join(refused))
    if refused and mode == "readback":
        raise SystemExit(1)
finally:
    connection.close()
"""
)
