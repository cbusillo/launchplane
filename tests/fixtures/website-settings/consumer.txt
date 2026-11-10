"""Apply Launchplane's accepted native website settings before web serving."""

import hashlib
import json
import re
from typing import Any
from urllib.parse import SplitResult, urlsplit

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
MANAGED_NOINDEX = '<meta name="robots" content="noindex" data-launchplane-production-use="prelaunch"/>'
INDEXING_FIELDS = frozenset({"website_id", "public_origin", "production_use"})


def indexing_head(head: str, production_use: str) -> str:
    unmanaged = head.removeprefix(MANAGED_NOINDEX)
    if "data-launchplane-production-use" in unmanaged.lower():
        raise RuntimeError("Native website head has an ambiguous managed indexing element.")
    return MANAGED_NOINDEX + unmanaged if production_use == "prelaunch" else unmanaged


def head_digest(head: str) -> str:
    return hashlib.sha256(head.encode()).hexdigest()


def parse_public_origin(origin: object) -> SplitResult:
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
    return parsed


def indexing_website(env: Any, raw: dict[str, Any]) -> Any:
    website_id = raw["website_id"]
    if type(website_id) is not int or website_id < 1:
        raise RuntimeError("Website indexing requires one native website ID.")
    origin = raw["public_origin"]
    if not isinstance(origin, str) or any(character.isspace() for character in origin):
        raise RuntimeError("Website indexing requires an exact public HTTPS origin.")
    try:
        parsed = parse_public_origin(origin)
        valid_port = parsed.port is None or 0 < parsed.port <= 65535
    except ValueError as error:
        raise RuntimeError("Website indexing requires an exact public HTTPS origin.") from error
    if parsed.username is not None or not valid_port:
        raise RuntimeError("Website indexing requires an exact public HTTPS origin.")
    websites = env["website"].sudo()
    host = str(parsed.netloc).lower().encode("idna")
    matches = [
        website for website in websites.search([]) if str(urlsplit(str(website.domain or "")).netloc).lower().encode("idna") == host
    ]
    if len(matches) != 1 or matches[0].id != website_id:
        raise RuntimeError("Website indexing requires an unambiguous native website/origin binding.")
    website = matches[0]
    if str(website.domain or "").rstrip("/") != origin.rstrip("/"):
        raise RuntimeError("Website indexing origin does not match the native website domain.")
    if "custom_code_head" not in website._fields:
        raise RuntimeError("Native website head setting is unavailable.")
    return website


def plan_website_indexing(env: Any, payload: dict[str, object] | None, *, instance: str) -> dict[str, Any] | None:
    raw = payload.get("website_indexing") if payload else None
    if raw is None:
        return None
    if (
        not payload
        or instance.strip().lower() not in PRODUCTION_INSTANCE_NAMES
        or payload.get("instance") not in PRODUCTION_INSTANCE_NAMES
    ):
        raise RuntimeError("Website indexing requires a matching production payload.")
    if not isinstance(raw, dict) or set(raw) != INDEXING_FIELDS or raw["production_use"] not in ("prelaunch", "live"):
        raise RuntimeError("Invalid Launchplane website indexing actuation data.")
    legacy = payload.get("website_settings")
    if legacy is not None and (
        not isinstance(legacy, dict)
        or legacy.get("website_id") != raw["website_id"]
        or str(legacy.get("public_origin", "")).rstrip("/") != str(raw["public_origin"]).rstrip("/")
    ):
        raise RuntimeError("Website settings and indexing must select the same native website and origin.")
    if not website_is_loaded(env):
        raise RuntimeError("Website indexing requires the installed website addon.")
    website = indexing_website(env, raw)
    before = str(website.custom_code_head or "")
    after = indexing_head(before, raw["production_use"])
    return {
        **raw,
        "field": "custom_code_head",
        "before_sha256": head_digest(before),
        "after_sha256": head_digest(after),
        "changed": before != after,
        "managed_noindex_before": before.startswith(MANAGED_NOINDEX),
        "managed_noindex_after": after.startswith(MANAGED_NOINDEX),
    }


def apply_website_settings(env: Any, payload: dict[str, object] | None, *, instance: str) -> str | None:
    plan = plan_website_indexing(env, payload, instance=instance)
    if plan is None:
        return apply_legacy_website_settings(env, payload, instance=instance)
    website = indexing_website(env, plan)
    try:
        with env.cr.savepoint():
            digest = apply_legacy_website_settings(env, payload, instance=instance)
            before = str(website.custom_code_head or "")
            if head_digest(before) != plan["before_sha256"]:
                raise RuntimeError("Native website head changed after indexing planning.")
            after = indexing_head(before, plan["production_use"])
            if before != after:
                website.write({"custom_code_head": after or False})
            website.flush_recordset(["custom_code_head"])
            website.invalidate_recordset(["custom_code_head"])
            if head_digest(str(website.custom_code_head or "")) != plan["after_sha256"]:
                raise RuntimeError("Native website indexing readback failed.")
    except BaseException:
        if clear_cache := getattr(env.registry, "clear_cache", None):
            clear_cache("stable")
        raise
    print("website_indexing=" + json.dumps({**plan, "readback_verified": True}, sort_keys=True, separators=(",", ":")))
    return digest


def read_string_parameter(parameters: Any, key: str) -> str:
    read = getattr(parameters, "get_str", None) or parameters.get_param
    return read(key, default="")


def write_string_parameter(parameters: Any, key: str, value: str) -> None:
    write = getattr(parameters, "set_str", None) or parameters.set_param
    write(key, value)


def website_is_loaded(env: Any) -> bool:
    if "website" not in env.registry:
        return False
    module = (
        env["ir.module.module"]
        .sudo()
        .search(
            [("name", "=", "website"), ("state", "in", ("installed", "to upgrade", "to remove"))],
            limit=1,
        )
    )
    return bool(module)


def exclude_collection(websites: Any, parameters: Any) -> None:
    for website in websites.search([]):
        if "google_analytics_key" not in website._fields:
            raise RuntimeError("Native website analytics field is unavailable.")
        website.write({"google_analytics_key": False})
        website.flush_recordset(["google_analytics_key"])
        website.invalidate_recordset(["google_analytics_key"])
        if website.google_analytics_key:
            raise RuntimeError("Nonproduction website analytics exclusion readback failed.")
    write_string_parameter(parameters, INDEXING_PARAMETER, "0")
    if read_string_parameter(parameters, INDEXING_PARAMETER).lower() not in {"0", "false"}:
        raise RuntimeError("Nonproduction website indexing exclusion readback failed.")


def apply_legacy_website_settings(env: Any, payload: dict[str, object] | None, *, instance: str) -> str | None:
    """Call in the existing environment-overrides transaction; caller commits.

    Empty/unknown instances are nonproduction. Clearing every copied website is
    mandatory and does not depend on the optional restore sanitization flag.
    """
    raw = payload.get("website_settings") if payload else None
    production = instance.strip().lower() in PRODUCTION_INSTANCE_NAMES
    if not website_is_loaded(env):
        if raw is not None:
            raise RuntimeError("Website settings require the installed website addon.")
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
    parse_public_origin(origin)
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
    write_string_parameter(parameters, INDEXING_PARAMETER, indexing)
    if read_string_parameter(parameters, INDEXING_PARAMETER) != indexing:
        raise RuntimeError("Website indexing readback failed.")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest
