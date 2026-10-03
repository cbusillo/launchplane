"""Why an Odoo target replacement stopped before writing the provider env.

Each check that runs before the provider env write has its own code and a fixed
description written here, by Launchplane. Records carry the code, this
description and env-key names; never the check's message, which can carry
provider text that names targets, hosts and databases.
"""

DEPLOY_BLOCKED_PREFIX = "deploy_blocked."
# Any other failure inside the deploy step, after the checks passed.
DEPLOY_FAILED_CODE = "deploy_failed"
_DEPLOY_FAILED_DESCRIPTION = "The deploy step failed."

DEPLOY_BLOCKED_DESCRIPTIONS: dict[str, str] = {
    "provider_target_unreadable": (
        "Launchplane could not read the lane's Dokploy target before writing its settings."
    ),
    "site_environment_unresolved": (
        "Launchplane could not resolve the site's runtime settings from its records."
    ),
    "platform_credential_refused": (
        "A setting the site's records would carry is a platform credential, which never "
        "belongs in an app runtime."
    ),
    "lane_profile_unresolved": (
        "Launchplane could not read the lane's settings from the product profile."
    ),
    "retirement_changed": (
        "The lane's retired provider settings changed between the plan and the deploy."
    ),
    "retirement_conflict": (
        "A retired provider setting is also a declared or driver-owned setting of the lane."
    ),
    "runtime_secret_values_missing": "A secret the deploy carries has no stored value.",
    "runtime_key_safety_refused": (
        "Runtime key safety refused a secret the deploy would carry to this lane."
    ),
    "runtime_key_safety_unavailable": (
        "Launchplane could not evaluate runtime key safety for the lane's secrets."
    ),
    "runtime_settings_unavailable": (
        "Launchplane could not check the lane's runtime settings before writing them."
    ),
    "compose_keys_missing": (
        "The compose template requires settings that neither the site's records nor the "
        "target provide."
    ),
    "upstream_restore_blocked": (
        "The lane's upstream-restore settings are missing or invalid for this deploy."
    ),
    "provider_only_keys": (
        "The target holds settings that no Launchplane record for the site holds; record "
        "them for the site or retire them."
    ),
    "unportable_values": (
        "A setting's value cannot be carried intact in the provider env, such as a multiline value."
    ),
    "override_secret_keys_missing": (
        "The lane's setting overrides need secrets that the deploy does not carry."
    ),
}

# LiveTargetRuntimeError codes, mapped onto the checks above.
_RUNTIME_ERROR_CHECKS: dict[str, str] = {
    "runtime_retirement_changed": "retirement_changed",
    "runtime_retirement_conflict": "retirement_conflict",
    "runtime_secret_values_missing": "runtime_secret_values_missing",
    "runtime_key_safety_failed": "runtime_key_safety_refused",
    "runtime_key_safety_unavailable": "runtime_key_safety_unavailable",
    "product_lane_not_found": "lane_profile_unresolved",
    "runtime_environment_empty": "site_environment_unresolved",
    "runtime_environment_unavailable": "site_environment_unresolved",
}


def deploy_blocked_code(check: str) -> str:
    """The error code for ``check``, which must be one of the described checks."""
    if check not in DEPLOY_BLOCKED_DESCRIPTIONS:
        raise ValueError(f"Undescribed deploy check: {check}")
    return DEPLOY_BLOCKED_PREFIX + check


def deploy_blocked_code_for_runtime_error(runtime_error_code: str) -> str:
    """The deploy check a live-target runtime error code belongs to."""
    return deploy_blocked_code(
        _RUNTIME_ERROR_CHECKS.get(runtime_error_code, "runtime_settings_unavailable")
    )


def deploy_failure_description(code: str) -> str:
    """The fixed description of ``code``, or "" when Launchplane does not describe it here."""
    if code == DEPLOY_FAILED_CODE:
        return _DEPLOY_FAILED_DESCRIPTION
    if not code.startswith(DEPLOY_BLOCKED_PREFIX):
        return ""
    return DEPLOY_BLOCKED_DESCRIPTIONS.get(code.removeprefix(DEPLOY_BLOCKED_PREFIX), "")
