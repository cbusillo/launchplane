"""Record a product profile's immutable GitHub repository identity from inventory.

GitHub events are mapped to a product profile by its recorded ``repository_id``.
This module plans the one bounded change that records that identity: it copies
``repository_id`` and ``repository_owner_id`` from Launchplane's current tracked
repository inventory record for the profile's ``repository`` and never accepts
ids from the caller or overwrites an identity that is already recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.contracts.repository_inventory import (
    RepositoryInventoryRecord,
    normalize_repository,
)


ProductRepositoryIdentityMode = Literal["dry-run", "apply"]
ProductRepositoryIdentityOperation = Literal["record", "unchanged"]
ProductRepositoryIdentityRefusalCode = Literal[
    "repository_identity_profile_repository_missing",
    "repository_identity_inventory_missing",
    "repository_identity_inventory_ambiguous",
    "repository_identity_claimed_by_other_product",
    "repository_identity_conflict",
]
PRODUCT_REPOSITORY_IDENTITY_SOURCE: Literal["service:product-repository-identity"] = (
    "service:product-repository-identity"
)


class ProductRepositoryIdentityRefusal(ValueError):
    def __init__(self, code: ProductRepositoryIdentityRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code: ProductRepositoryIdentityRefusalCode = code


class ProductRepositoryIdentityApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    mode: ProductRepositoryIdentityMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductRepositoryIdentityApplyRequest":
        self.product = self.product.strip()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if not self.product:
            raise ValueError("Product repository identity request requires product.")
        if not self.reason:
            raise ValueError("Product repository identity request requires reason.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Product repository identity dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply" and not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
            raise ValueError(
                "Product repository identity apply requires a reviewed 64-character plan SHA-256."
            )
        return self


class ProductRepositoryIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: str = ""
    repository_owner_id: str = ""


class ProductRepositoryIdentityPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductRepositoryIdentityMode
    product: str
    repository: str
    operation: ProductRepositoryIdentityOperation
    identity_before: ProductRepositoryIdentity
    identity_after: ProductRepositoryIdentity
    inventory_record_id: str
    inventory_revision: int
    inventory_digest: str
    changed: bool
    applied: bool = False
    reason: str
    source_label: Literal["service:product-repository-identity"] = (
        PRODUCT_REPOSITORY_IDENTITY_SOURCE
    )
    profile_record_sha256_before: str
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    plan_sha256: str
    read_back: ProductRepositoryIdentity | None = None
    read_back_matches: bool | None = None


def current_tracked_inventory_record(
    *,
    repository: str,
    inventory_records: Iterable[RepositoryInventoryRecord],
) -> RepositoryInventoryRecord:
    """Return the one current tracked inventory record whose repository matches."""

    streams: dict[str, list[RepositoryInventoryRecord]] = {}
    for record in inventory_records:
        streams.setdefault(record.repository_id, []).append(record)
    matches: list[RepositoryInventoryRecord] = []
    for stream in streams.values():
        highest_revision = max(record.inventory_revision for record in stream)
        current = [record for record in stream if record.inventory_revision == highest_revision]
        if not any(record.repository == repository for record in current):
            continue
        if len(current) != 1:
            raise ProductRepositoryIdentityRefusal(
                "repository_identity_inventory_ambiguous",
                f"Repository inventory for {repository} has an ambiguous current revision.",
            )
        if current[0].inventory_state == "tracked":
            matches.append(current[0])
    if len(matches) > 1:
        raise ProductRepositoryIdentityRefusal(
            "repository_identity_inventory_ambiguous",
            f"More than one current tracked repository inventory record names {repository}.",
        )
    if not matches:
        raise ProductRepositoryIdentityRefusal(
            "repository_identity_inventory_missing",
            f"No current tracked repository inventory record names {repository}.",
        )
    return matches[0]


def build_product_repository_identity_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductRepositoryIdentityApplyRequest,
    all_profiles: Iterable[LaunchplaneProductProfileRecord],
    inventory_records: Iterable[RepositoryInventoryRecord],
) -> ProductRepositoryIdentityPlan:
    if profile.product != request.product:
        raise ValueError("Product repository identity target does not match the loaded profile.")
    try:
        repository = normalize_repository(profile.repository, "repository")
    except ValueError as error:
        raise ProductRepositoryIdentityRefusal(
            "repository_identity_profile_repository_missing",
            f"Product profile {profile.product} has no usable GitHub owner/name repository.",
        ) from error
    inventory = current_tracked_inventory_record(
        repository=repository, inventory_records=inventory_records
    )
    claimed_by = sorted(
        other.product
        for other in all_profiles
        if other.product != profile.product and other.repository_id == inventory.repository_id
    )
    if claimed_by:
        raise ProductRepositoryIdentityRefusal(
            "repository_identity_claimed_by_other_product",
            f"Repository id {inventory.repository_id} is already recorded by product profile "
            + ", ".join(claimed_by)
            + ".",
        )
    identity_before = ProductRepositoryIdentity(
        repository_id=profile.repository_id,
        repository_owner_id=profile.repository_owner_id,
    )
    identity_after = ProductRepositoryIdentity(
        repository_id=inventory.repository_id,
        repository_owner_id=inventory.repository_owner_id,
    )
    if identity_before.repository_id and identity_before != identity_after:
        raise ProductRepositoryIdentityRefusal(
            "repository_identity_conflict",
            f"Product profile {profile.product} already records a different repository "
            "identity; this route never overwrites a recorded identity.",
        )
    changed = identity_before != identity_after
    profile_sha256 = product_profile_record_sha256(profile)
    plan_evidence = {
        "schema_version": 1,
        "product": profile.product,
        "repository": repository,
        "identity_before": identity_before.model_dump(mode="json"),
        "identity_after": identity_after.model_dump(mode="json"),
        "inventory_record_id": inventory.record_id,
        "inventory_digest": inventory.inventory_digest,
        "profile_record_sha256": profile_sha256,
        "source_label": PRODUCT_REPOSITORY_IDENTITY_SOURCE,
        "reason": request.reason,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(plan_evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ProductRepositoryIdentityPlan(
        mode=request.mode,
        product=profile.product,
        repository=repository,
        operation="record" if changed else "unchanged",
        identity_before=identity_before,
        identity_after=identity_after,
        inventory_record_id=inventory.record_id,
        inventory_revision=inventory.inventory_revision,
        inventory_digest=inventory.inventory_digest,
        changed=changed,
        reason=request.reason,
        profile_record_sha256_before=profile_sha256,
        profile_updated_at_before=profile.updated_at,
        plan_sha256=plan_sha256,
    )


def updated_product_repository_identity_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    plan: ProductRepositoryIdentityPlan,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    updated_profile = profile.model_copy(
        update={
            "repository_id": plan.identity_after.repository_id,
            "repository_owner_id": plan.identity_after.repository_owner_id,
            "updated_at": updated_at,
            "source": PRODUCT_REPOSITORY_IDENTITY_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(
        updated_profile.model_dump(mode="json")
    ).validate_write_contract()


def read_back_identity(profile: LaunchplaneProductProfileRecord) -> ProductRepositoryIdentity:
    return ProductRepositoryIdentity(
        repository_id=profile.repository_id,
        repository_owner_id=profile.repository_owner_id,
    )
