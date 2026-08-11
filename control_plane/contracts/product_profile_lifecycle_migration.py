from __future__ import annotations

from collections.abc import Mapping


def migrate_product_profile_lifecycle_payload(payload: object) -> object:
    if not isinstance(payload, Mapping):
        return payload
    migrated = dict(payload)
    migrated.setdefault("lifecycle_state", "active")
    return migrated
