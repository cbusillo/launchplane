from __future__ import annotations

from collections.abc import Mapping
from control_plane.contracts.product_application_lifecycle_migration import (
    migrate_product_application_lifecycle_payload,
)


def migrate_product_profile_lifecycle_payload(payload: object) -> object:
    if not isinstance(payload, Mapping):
        return payload
    migrated = dict(payload)
    migrated.setdefault("lifecycle_state", "active")
    return migrate_product_application_lifecycle_payload(migrated)
