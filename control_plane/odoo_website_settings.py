"""Prepare website intent and resolve only an artifact's accepted settings."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.public_hosts import resolve_public_base_url
from control_plane.odoo_post_deploy_http import resolve_odoo_post_deploy_product_route
from control_plane.storage.product_authority_bundle import LaneProductConfigWriteRequirements

ODOO_WEBSITE_SETTINGS_ROUTE = "/v1/product-config/odoo-website-settings/apply"


class OdooWebsiteSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: Literal["prod"] = "prod"
    website_settings: OdooWebsiteSettingsIntent | None
    mode: Literal["dry-run", "apply"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> OdooWebsiteSettingsRequest:
        if (
            self.website_settings is not None
            and self.website_settings.settings.operation != "apply"
        ):
            raise ValueError(
                "Preparation accepts full website settings; collection-only rollback is driver-owned."
            )
        if self.mode == "apply" and (
            len(self.reviewed_plan_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.reviewed_plan_sha256)
        ):
            raise ValueError("Website settings preparation requires the reviewed plan digest.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry-run must not supply a reviewed plan digest.")
        return self


class OdooWebsiteSettingsStale(ValueError):
    pass


def _existing(store: Any, context: str, instance: str) -> OdooInstanceOverrideRecord | None:
    try:
        return cast(
            OdooInstanceOverrideRecord,
            store.read_odoo_instance_override_record(context_name=context, instance_name=instance),
        )
    except FileNotFoundError:
        return None


def plan_website_settings(store: Any, request: OdooWebsiteSettingsRequest) -> dict[str, object]:
    profile = resolve_odoo_post_deploy_product_route(
        record_store=store,
        product=request.product,
        context=request.context,
        instance=request.instance,
    )
    production = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
    if request.website_settings is not None:
        _validate_candidate(store, request, profile, production)
    existing = _existing(store, request.context, request.instance)
    before = existing.model_dump(mode="json") if existing else None
    desired = request.website_settings.model_dump(mode="json") if request.website_settings else None
    digest = hashlib.sha256(
        json.dumps(
            {
                "product": request.product,
                "context": request.context,
                "instance": request.instance,
                "before": before,
                "desired": desired,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "product": request.product,
        "context": request.context,
        "instance": request.instance,
        "record_sha256_before": hashlib.sha256(
            json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "mode": request.mode,
        "plan_sha256": digest,
        "before": existing.website_settings.model_dump(mode="json")
        if existing and existing.website_settings
        else None,
        "after": desired,
        "changed": (existing.website_settings if existing else None) != request.website_settings,
        "applied": False,
        "read_back_matches": False,
        "activation": "Client acceptance of the exact release applies these settings; preparation changes no Odoo database.",
    }


def _validate_candidate(
    store: Any,
    request: OdooWebsiteSettingsRequest,
    profile: LaunchplaneProductProfileRecord,
    production: ProductLaneProfile | None,
) -> None:
    assert request.website_settings is not None
    if production is None:
        raise ValueError("Website settings require the product's production lane.")
    try:
        target = store.read_dokploy_target_record(
            context_name=production.context, instance_name=production.instance
        )
        public_hosts = target.public_hosts
    except FileNotFoundError:
        public_hosts = ()
    public_origin = resolve_public_base_url(
        instance=production.instance, public_hosts=public_hosts
    ) or production.base_url.rstrip("/")
    if request.website_settings.settings.public_origin.rstrip("/") != public_origin:
        raise ValueError(
            "Website settings origin must match the product's resolved production URL."
        )
    testing = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
    if testing is None:
        raise ValueError("Website settings require the product's testing candidate.")
    try:
        candidate = store.read_release_tuple_record(
            context_name=testing.context, channel_name="testing"
        )
    except FileNotFoundError as error:
        raise ValueError("Website settings require the current testing release record.") from error
    if candidate.artifact_id != request.website_settings.artifact_id:
        raise ValueError("Website settings must name this product's current testing artifact.")


def prepare_website_settings(
    store: Any,
    request: OdooWebsiteSettingsRequest,
    *,
    required_context_owner: tuple[str, str] | None = None,
    required_product_config_target: tuple[str, str, str] | None = None,
) -> dict[str, object]:
    plan = plan_website_settings(store, request)
    if plan["plan_sha256"] != request.reviewed_plan_sha256:
        raise OdooWebsiteSettingsStale("Website settings changed. Run a new dry-run.")
    existing = _existing(store, request.context, request.instance)
    current_digest = hashlib.sha256(
        json.dumps(
            existing.model_dump(mode="json") if existing else None,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if current_digest != plan["record_sha256_before"]:
        raise OdooWebsiteSettingsStale(
            "Website settings changed during preparation. Run a new dry-run."
        )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    replacement = (
        existing.model_copy(
            update={"website_settings": request.website_settings, "updated_at": now}
        )
        if existing
        else OdooInstanceOverrideRecord(
            context=request.context,
            instance=request.instance,
            website_settings=request.website_settings,
            updated_at=now,
            source_label="service:odoo-website-settings",
        )
    )
    if plan["changed"]:
        store.write_odoo_instance_override_record(
            replacement,
            expected_record=existing,
            require_absent=existing is None,
            **LaneProductConfigWriteRequirements(
                required_context_owner=required_context_owner,
                required_product_config_target=required_product_config_target,
            ),
        )
    read_back = _existing(store, request.context, request.instance)
    if (read_back.website_settings if read_back else None) != request.website_settings:
        raise ValueError("Website settings preparation readback failed.")
    return {**plan, "applied": bool(plan["changed"]), "read_back_matches": True}
