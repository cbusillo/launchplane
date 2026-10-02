"""Record a lane's provider-only settings from the provider itself.

A product-config request can name keys to adopt from the lane's current provider
env. The service reads the values and records them on the lane, so a value never
passes through the person or agent who asked. Results carry key names and a
disposition per key, never a value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from control_plane import runtime_key_safety
from control_plane import runtime_platform_credentials

ProviderKeyDisposition = Literal[
    "adopted",
    "template_default",
    "already_recorded",
    "refused_credential",
    "missing",
]

# A request that still holds one of these cannot be applied.
REFUSED_DISPOSITIONS: frozenset[ProviderKeyDisposition] = frozenset(
    ("refused_credential", "missing")
)

_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ProviderKeyAdoptionError(ValueError):
    """The request names keys the service will not adopt; names only, never values."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LaneProviderEnv:
    """The lane's current provider env, read by the service, and what decides each key.

    ``recorded_keys`` are keys other site records already supply for the lane, such as
    the tracked target's env and managed secret bindings. ``unretirable_keys`` are
    declared application or driver settings, which a template default never retires.
    """

    env: Mapping[str, str]
    template_defaults: Mapping[str, str]
    recorded_keys: frozenset[str] = frozenset()
    unretirable_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProviderKeyAdoptionPlan:
    adopted_env: dict[str, str]
    template_default_keys: tuple[str, ...]
    dispositions: tuple[tuple[str, ProviderKeyDisposition], ...]

    @property
    def refused_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key, disposition in self.dispositions if disposition in REFUSED_DISPOSITIONS
        )

    def summary(self) -> list[dict[str, str]]:
        return [{"key": key, "disposition": disposition} for key, disposition in self.dispositions]


def normalize_adopt_provider_keys(raw_keys: object) -> tuple[str, ...]:
    if not isinstance(raw_keys, (list, tuple)):
        raise ProviderKeyAdoptionError(
            "Provider key adoption requires a list of key names.", code="invalid_request"
        )
    keys: list[str] = []
    for raw_key in raw_keys:
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ProviderKeyAdoptionError(
                "Provider key adoption names must be env key names.", code="invalid_request"
            )
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ProviderKeyAdoptionError(
            "Provider key adoption names must be unique.", code="invalid_request"
        )
    if not keys:
        raise ProviderKeyAdoptionError(
            "Provider key adoption requires at least one key name.", code="invalid_request"
        )
    return tuple(sorted(keys))


def looks_like_credential(key: str, value: str) -> bool:
    """Whether a setting must go through managed secrets instead, by its name or value."""
    return (
        runtime_key_safety.is_secret_shaped_runtime_key(key)
        or runtime_key_safety.is_credential_runtime_value(key, value)
        or runtime_platform_credentials.plain_setting_looks_like_credential(key, value)
    )


def plan_provider_key_adoption(
    *,
    keys: tuple[str, ...],
    provider: LaneProviderEnv,
    recorded_keys: frozenset[str],
) -> ProviderKeyAdoptionPlan:
    """Decide each named key's disposition from the provider env.

    A key a site record already supplies is left alone: the record is the authority
    and the deploy delivers it. A value equal to the compose template default is
    retired, so the provider copy goes away and the template supplies the same value.
    """
    adopted_env: dict[str, str] = {}
    template_default_keys: list[str] = []
    dispositions: list[tuple[str, ProviderKeyDisposition]] = []
    for key in keys:
        disposition: ProviderKeyDisposition
        if key in recorded_keys or key in provider.recorded_keys:
            disposition = "already_recorded"
        elif key not in provider.env:
            disposition = "missing"
        elif looks_like_credential(key, provider.env[key]):
            disposition = "refused_credential"
        elif (
            key in provider.template_defaults
            and key not in provider.unretirable_keys
            and provider.env[key].strip() == provider.template_defaults[key]
        ):
            disposition = "template_default"
            template_default_keys.append(key)
        else:
            disposition = "adopted"
            adopted_env[key] = provider.env[key]
        dispositions.append((key, disposition))
    return ProviderKeyAdoptionPlan(
        adopted_env=adopted_env,
        template_default_keys=tuple(template_default_keys),
        dispositions=tuple(dispositions),
    )
