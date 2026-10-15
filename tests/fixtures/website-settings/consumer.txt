"""Apply Launchplane's accepted native website settings before web serving."""

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

PRODUCTION_INSTANCE_NAMES = frozenset({"prod", "production"})
INDEXING_PARAMETER = "cm_website.public_indexing_enabled"
SETTINGS_FIELDS = frozenset(
    {
        "operation",
        "website_id",
        "public_origin",
        "public_indexing_enabled",
        "google_analytics_key",
        "cookies_bar",
        "block_third_party_domains",
    }
)
NATIVE_FIELDS = ("google_analytics_key", "cookies_bar", "block_third_party_domains")


def exclude_collection(websites: Any, parameters: Any) -> None:
    for website in websites.search([]):
        if "google_analytics_key" not in website._fields:
            raise RuntimeError("Native website analytics field is unavailable.")
        website.write({"google_analytics_key": False})
        website.flush_recordset(["google_analytics_key"])
        website.invalidate_recordset(["google_analytics_key"])
        if website.google_analytics_key:
            raise RuntimeError("Nonproduction website analytics exclusion readback failed.")
    parameters.set_param(INDEXING_PARAMETER, "0")
    if str(parameters.get_param(INDEXING_PARAMETER)).lower() not in {"0", "false"}:
        raise RuntimeError("Nonproduction website indexing exclusion readback failed.")


def apply_website_settings(env: Any, payload: dict[str, object] | None, *, instance: str) -> str | None:
    """Call in the existing environment-overrides transaction; caller commits.

    Empty/unknown instances are nonproduction. Clearing every copied website is
    mandatory and does not depend on the optional restore sanitization flag.
    """
    raw = payload.get("website_settings") if payload else None
    production = instance.strip().lower() in PRODUCTION_INSTANCE_NAMES
    if "website" not in env.registry:
        if raw is not None:
            raise RuntimeError("Website settings require the native website model.")
        return
    websites = env["website"].sudo()
    parameters = env["ir.config_parameter"].sudo()
    if not production:
        exclude_collection(websites, parameters)
        print("native_ga4_and_cm_indexing_excluded=true")
        return
    if raw is None:
        return
    if not payload or payload.get("instance") not in PRODUCTION_INSTANCE_NAMES:
        raise RuntimeError("Website settings require a matching production payload.")
    if not isinstance(raw, dict) or set(raw) != SETTINGS_FIELDS:
        raise RuntimeError("Invalid typed website settings fields.")
    website_id = raw["website_id"]
    if type(website_id) is not int or website_id < 1:
        raise RuntimeError("Website settings require one native website ID.")
    operation = raw["operation"]
    if operation not in {"apply", "disable_collection"} or type(raw["public_indexing_enabled"]) is not bool:
        raise RuntimeError("Website settings require a supported operation and indexing choice.")
    if operation == "apply":
        for key in ("cookies_bar", "block_third_party_domains"):
            if type(raw[key]) is not bool:
                raise RuntimeError("Website settings require boolean choices.")
    elif (
        raw["google_analytics_key"]
        or raw["public_indexing_enabled"]
        or raw["cookies_bar"] is not None
        or raw["block_third_party_domains"] is not None
    ):
        raise RuntimeError("Collection-only rollback must preserve consent controls.")
    analytics = raw["google_analytics_key"]
    if not isinstance(analytics, str) or analytics and not re.fullmatch(r"G-[A-Z0-9]+", analytics):
        raise RuntimeError("Website settings require a GA4 Measurement ID or an empty key.")
    if analytics and not (raw["cookies_bar"] and raw["block_third_party_domains"]):
        raise RuntimeError("Website analytics requires the cookie bar and third-party blocking.")
    origin = raw["public_origin"]
    if not isinstance(origin, str):
        raise RuntimeError("Website settings require a public HTTPS origin.")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Website settings require a public HTTPS origin.")
    if operation == "disable_collection":
        # Historical recovery has no verified selector. Disable collection across
        # the database while preserving every website's consent/embedding choices.
        exclude_collection(websites, parameters)
        return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    website = websites.browse(website_id).exists()
    if not website or len(website) != 1:
        raise RuntimeError("Website settings selector did not find exactly one website.")
    if str(website.domain or "").rstrip("/") != origin.rstrip("/"):
        raise RuntimeError("Website settings origin does not match the native website domain.")
    if any(field not in website._fields for field in NATIVE_FIELDS):
        raise RuntimeError("Website settings require the native analytics and cookie fields.")
    values = {field: raw[field] for field in NATIVE_FIELDS} if operation == "apply" else {}
    values["google_analytics_key"] = analytics or False
    website.write(values)
    website.flush_recordset(list(NATIVE_FIELDS))
    website.invalidate_recordset(list(NATIVE_FIELDS))
    for field, expected in values.items():
        if getattr(website, field) != expected:
            raise RuntimeError("Native website settings readback failed.")
    indexing = "1" if raw["public_indexing_enabled"] else "0"
    parameters.set_param(INDEXING_PARAMETER, indexing)
    if parameters.get_param(INDEXING_PARAMETER) != indexing:
        raise RuntimeError("Website indexing readback failed.")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest
