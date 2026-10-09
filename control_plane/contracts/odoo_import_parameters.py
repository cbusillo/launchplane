"""Non-secret import settings whose values are owned by the lane runtime records."""

ODOO_IMPORT_PARAMETER_KEYS = frozenset(
    {"cm_data.db.user", "repairshopr.sync_db.user", "repairshopr.sync_db.host"}
)


def import_parameter_runtime_key(key: str) -> str:
    if key not in ODOO_IMPORT_PARAMETER_KEYS:
        raise ValueError("Unsupported non-secret Odoo import parameter.")
    return "ENV_OVERRIDE_CONFIG_PARAM__" + key.upper().replace(".", "__")
