"""Versioned change-impact identity, separate from immutable policy provenance."""

import re
from typing import Any, Literal


ChangeImpactBindingHashVersion = Literal[2]
CHANGE_IMPACT_POLICY_PROVENANCE_FIELDS = frozenset(
    {
        "change_impact_policy_record_id",
        "change_impact_policy_revision",
        "change_impact_policy_digest",
    }
)


def validate_change_impact_binding(
    version: ChangeImpactBindingHashVersion | None, digest: str | None
) -> None:
    """Require an explicit v2 identity while leaving omitted legacy fields untouched."""
    if version is None:
        if digest is not None:
            raise ValueError("A scoped change-impact digest requires binding_hash_version=2.")
        return
    if digest is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Version 2 requires a lowercase SHA-256 change-impact decision digest.")


def change_impact_bound_payload(
    payload: dict[str, Any],
    *,
    version: ChangeImpactBindingHashVersion | None,
    domain: str,
) -> dict[str, Any]:
    """Keep legacy hash bytes and separate v2 semantic identity from policy provenance."""
    if version is None:
        return payload
    return {
        key: value
        for key, value in payload.items()
        if key not in CHANGE_IMPACT_POLICY_PROVENANCE_FIELDS
    } | {"binding_hash_domain": domain}
