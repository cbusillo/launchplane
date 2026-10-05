"""Non-secret service App selectors from runtime records, without secret overlays."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from control_plane.runtime_environments import RuntimeEnvironmentDefinition

ADVISORY_GITHUB_APP_ID_ENV_KEY = "LAUNCHPLANE_ADVISORY_GITHUB_APP_ID"


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
