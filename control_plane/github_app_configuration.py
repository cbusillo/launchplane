"""Non-secret service App selectors from runtime records, without secret overlays."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from control_plane.runtime_environments import RuntimeEnvironmentDefinition

ADVISORY_GITHUB_APP_ID_ENV_KEY = "LAUNCHPLANE_ADVISORY_GITHUB_APP_ID"
ONBOARDING_GITHUB_APP_CLIENT_ID_ENV_KEY = "LAUNCHPLANE_ONBOARDING_GITHUB_APP_CLIENT_ID"
ONBOARDING_GITHUB_APP_INTEGRATION = "onboarding_github_app"


def validate_onboarding_app_client_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.]{1,128}", value) is None:
        raise ValueError("Onboarding App client id must be a nonempty identifier.")
    return value


def validate_delivery_key_integration(value: str) -> str:
    integration = value.strip()
    if not integration or integration == ONBOARDING_GITHUB_APP_INTEGRATION:
        raise ValueError("Delivery App requires its own key integration.")
    return integration


def service_github_app_values(definition: RuntimeEnvironmentDefinition) -> dict[str, str]:
    context = definition.contexts.get("launchplane")
    if context is None:
        raise ValueError("Launchplane GitHub App runtime context is unavailable.")
    return {
        key: str(value) for key, value in {**definition.shared_env, **context.shared_env}.items()
    }


def advisory_app_id_from_values(values: Mapping[str, str]) -> int | None:
    value = values.get(ADVISORY_GITHUB_APP_ID_ENV_KEY, "").strip()
    return int(value) if value.isdecimal() and int(value) > 0 else None
