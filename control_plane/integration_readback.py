"""Check a non-production Odoo lane's integration settings before web starts.

A restored production copy carries the production integration settings with it:
store keys, API tokens, mail servers, payment providers. On a non-production lane
each known integration must be empty, or the lane must hold an allowance for it
(``policies.integration_allowances`` on its Dokploy target record). The check runs
inside the lane's script-runner container while web is stopped. The comparison
happens in PostgreSQL, so setting values never leave the database, and the output
names only the integration, the setting and the verdict.

The Shopify protected-store-key list stays as an extra check on every lane that
declares one, production included.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.dokploy_target_record import DokployTargetPolicies
from control_plane.odoo_instance_overrides import shopify_store_handle
from control_plane.runtime_key_safety import runtime_key_safety_environment_class

IntegrationReadbackWorkflowMode = Literal["maintenance", "bootstrap", "restore"]

INTEGRATION_READBACK_OK_MARKER = "integration_readback_ok"
INTEGRATION_READBACK_CHECKED_MARKER = "integration_readback_checked"
INTEGRATION_READBACK_REFUSED_MARKER = "integration_readback_refused"
INTEGRATION_READBACK_ALLOWED_MARKER = "integration_readback_allowed"
INTEGRATION_READBACK_NAME_LIST_MARKERS = frozenset(
    {INTEGRATION_READBACK_REFUSED_MARKER, INTEGRATION_READBACK_ALLOWED_MARKER}
)
_NAME_LIST_PATTERN = re.compile(r"[a-z0-9_.:/,-]{1,2000}")
_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}")
_CONFIG_PARAMETER_PATTERN = re.compile(r"[a-z][a-z0-9_.]{0,127}")


@dataclass(frozen=True)
class IntegrationTableSetting:
    """Rows in ``table`` that match ``configured_when`` are a live setting.

    ``configured_when`` is a fixed SQL predicate over columns that exist in every
    supported Odoo version. A table that doesn't exist (module not installed) holds
    no setting.
    """

    table: str
    configured_when: str


@dataclass(frozen=True)
class IntegrationFamily:
    """One integration's settings in an Odoo database.

    ``config_parameters`` are ``ir_config_parameter`` keys whose non-empty value
    reaches a live service. ``restore_only`` marks settings Odoo generates for itself
    on first use: they are checked right after a restore, when any value present came
    from the source database, and not on later deploys, when the lane's own
    generated value is expected.
    """

    integration: str
    config_parameters: tuple[str, ...] = ()
    tables: tuple[IntegrationTableSetting, ...] = ()
    restore_only: bool = False


def _database_connection(prefix: str) -> tuple[str, ...]:
    return (f"{prefix}.host", f"{prefix}.user", f"{prefix}.password")


INTEGRATION_FAMILIES: tuple[IntegrationFamily, ...] = (
    IntegrationFamily(
        integration="shopify",
        config_parameters=(
            "shopify.shop_url_key",
            "shopify.shop_url",
            "shopify.store_url",
            "shopify.api_token",
            "shopify.webhook_key",
        ),
    ),
    IntegrationFamily(integration="printnode", config_parameters=("printnode.api_key",)),
    IntegrationFamily(integration="fishbowl", config_parameters=_database_connection("fishbowl")),
    IntegrationFamily(
        integration="repairshopr",
        config_parameters=_database_connection("repairshopr.sync_db"),
    ),
    IntegrationFamily(integration="cm_data", config_parameters=_database_connection("cm_data.db")),
    IntegrationFamily(
        integration="outgoing_mail",
        tables=(
            # A server whose host is the reserved name "invalid" can never deliver;
            # devkit's restore leaves one active to block outgoing mail.
            IntegrationTableSetting(
                table="ir_mail_server",
                configured_when=(
                    "active AND btrim(coalesce(smtp_host, '')) <> ''"
                    " AND lower(btrim(smtp_host)) <> 'invalid'"
                    " AND lower(btrim(smtp_host)) NOT LIKE '%.invalid'"
                ),
            ),
        ),
    ),
    IntegrationFamily(
        integration="incoming_mail",
        tables=(
            IntegrationTableSetting(
                table="fetchmail_server",
                configured_when="active AND btrim(coalesce(server, '')) <> ''",
            ),
        ),
    ),
    IntegrationFamily(
        integration="payment",
        tables=(
            # "test" still reaches the provider's sandbox with stored credentials;
            # only providers with no remote service are exempt.
            IntegrationTableSetting(
                table="payment_provider",
                configured_when="state <> 'disabled' AND code NOT IN ('none', 'custom', 'demo')",
            ),
        ),
    ),
    IntegrationFamily(integration="mapbox", config_parameters=("web_map.token_map_box",)),
    IntegrationFamily(integration="unsplash", config_parameters=("unsplash.access_key",)),
    IntegrationFamily(integration="tenor", config_parameters=("discuss.tenor_api_key",)),
    IntegrationFamily(
        integration="web_push",
        config_parameters=(
            "mail.web_push_vapid_private_key",
            "mail.web_push_vapid_public_key",
        ),
        restore_only=True,
    ),
)


@dataclass(frozen=True)
class IntegrationReadbackPolicy:
    """What the read-back checks on one lane for one workflow run."""

    families: tuple[IntegrationFamily, ...]
    allowed_integrations: tuple[str, ...]
    protected_shopify_store_handles: tuple[str, ...]

    @property
    def required(self) -> bool:
        return bool(self.families or self.protected_shopify_store_handles)

    def encoded_spec(self) -> str:
        spec = {
            "families": [
                {
                    "integration": family.integration,
                    "config_parameters": list(family.config_parameters),
                    "tables": [
                        {"table": table.table, "configured_when": table.configured_when}
                        for table in family.tables
                    ],
                }
                for family in self.families
            ],
            "allowed_integrations": list(self.allowed_integrations),
            "protected_shopify_store_handles": list(self.protected_shopify_store_handles),
        }
        return base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()


def integration_readback_policy(
    *,
    instance_name: str,
    policies: DokployTargetPolicies,
    workflow_mode: IntegrationReadbackWorkflowMode,
) -> IntegrationReadbackPolicy:
    """Build the read-back for a lane from its class and its target record policies.

    Production lanes hold real settings by design, so only the protected-store-key
    check applies there. Every other lane, including a preview and one whose instance
    name has no recognized class, gets the full check. A preview's target definition
    carries no allowances, so it never inherits its template lane's.
    """
    families: tuple[IntegrationFamily, ...] = ()
    if runtime_key_safety_environment_class(instance_name) != "prod":
        families = tuple(
            family
            for family in INTEGRATION_FAMILIES
            if workflow_mode == "restore" or not family.restore_only
        )
    return IntegrationReadbackPolicy(
        families=families,
        allowed_integrations=tuple(
            allowance.integration for allowance in policies.integration_allowances
        ),
        protected_shopify_store_handles=tuple(
            sorted(
                {
                    shopify_store_handle(raw_key)
                    for raw_key in policies.shopify.protected_store_keys
                    if raw_key.strip()
                }
            )
        ),
    )


def integration_readback_marker_is_safe(key: str, value: str) -> bool:
    return key in INTEGRATION_READBACK_NAME_LIST_MARKERS and bool(
        _NAME_LIST_PATTERN.fullmatch(value)
    )


def integration_readback_refusal_detail(evidence: Mapping[str, str]) -> str:
    refused = evidence.get(INTEGRATION_READBACK_REFUSED_MARKER, "")
    if not refused:
        return "no refused setting was reported"
    return "refused " + ", ".join(refused.split(","))


def _validate_catalog(families: tuple[IntegrationFamily, ...]) -> None:
    for family in families:
        if not _IDENTIFIER_PATTERN.fullmatch(family.integration):
            raise ValueError(f"Invalid integration name {family.integration!r}.")
        for key in family.config_parameters:
            if not _CONFIG_PARAMETER_PATTERN.fullmatch(key):
                raise ValueError(f"Invalid config parameter {key!r}.")
        for table in family.tables:
            if not _IDENTIFIER_PATTERN.fullmatch(table.table):
                raise ValueError(f"Invalid table name {table.table!r}.")


_validate_catalog(INTEGRATION_FAMILIES)


# Runs in the script-runner container with the lane's database credentials. Every
# query returns only key names, counts or booleans: values are compared inside
# PostgreSQL. Table names and predicates come from the fixed catalog above, never
# from records or operator input.
INTEGRATION_READBACK_PROGRAM = """import base64
import json
import os
import sys

import psycopg2

database_name = sys.argv[1]
spec = json.loads(base64.b64decode(sys.argv[2]))
allowed_integrations = set(spec["allowed_integrations"])

# Trims every kind of whitespace, as Python's str.strip() does; btrim() trims only spaces.
TRIMMED_VALUE_SQL = "regexp_replace(coalesce(value, ''), '^[[:space:]]+|[[:space:]]+$', '', 'g')"
STORE_KEY_HANDLE_SQL = (
    "rtrim(split_part(regexp_replace(lower(" + TRIMMED_VALUE_SQL + "), '^[a-z]+://', ''), '/', 1), '.')"
)


def present_config_parameters(cursor, keys):
    cursor.execute(
        "SELECT key FROM ir_config_parameter WHERE key = ANY(%s) AND "
        + TRIMMED_VALUE_SQL
        + " <> ''",
        (list(keys),),
    )
    return sorted(row[0] for row in cursor.fetchall())


def table_is_configured(cursor, table):
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table["table"],))
    if not cursor.fetchone()[0]:
        return False
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM " + table["table"] + " WHERE " + table["configured_when"] + ")"
    )
    return bool(cursor.fetchone()[0])


def protected_store_key_present(cursor, handles):
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM ir_config_parameter"
        " WHERE key = 'shopify.shop_url_key' AND regexp_replace("
        + STORE_KEY_HANDLE_SQL
        + ", '[.]myshopify[.]com$', '') = ANY(%s))",
        (list(handles),),
    )
    return bool(cursor.fetchone()[0])


refused = []
allowed = []
checked = 0
connection = psycopg2.connect(
    host=(os.environ.get("ODOO_DB_HOST") or "database").strip(),
    port=(os.environ.get("ODOO_DB_PORT") or "5432").strip(),
    user=(os.environ.get("ODOO_DB_USER") or "odoo").strip(),
    password=os.environ.get("ODOO_DB_PASSWORD") or "",
    dbname=database_name,
)
try:
    with connection.cursor() as cursor:
        handles = spec["protected_shopify_store_handles"]
        if handles:
            checked += 1
            if protected_store_key_present(cursor, handles):
                refused.append("shopify/shopify.shop_url_key:protected")
        for family in spec["families"]:
            integration = family["integration"]
            settings = []
            if family["config_parameters"]:
                checked += len(family["config_parameters"])
                settings.extend(present_config_parameters(cursor, family["config_parameters"]))
            for table in family["tables"]:
                checked += 1
                if table_is_configured(cursor, table):
                    settings.append(table["table"])
            for setting in settings:
                entry = integration + "/" + setting
                if integration in allowed_integrations:
                    allowed.append(entry)
                else:
                    refused.append(entry)
finally:
    connection.close()

print("integration_readback_checked=" + str(checked))
if allowed:
    print("integration_readback_allowed=" + ",".join(allowed))
if refused:
    print("integration_readback_refused=" + ",".join(refused))
    print("integration_readback_ok=false")
    for entry in refused:
        integration, _, setting = entry.partition("/")
        print(
            "Integration read-back refused: integration=" + integration + " setting=" + setting
            + " db=" + database_name,
            file=sys.stderr,
        )
    raise SystemExit(
        "Non-production lane holds integration settings with no allowance: " + ", ".join(refused)
    )
print("integration_readback_ok=true")
"""
