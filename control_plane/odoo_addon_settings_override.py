"""Supported, audited writes for Odoo instance-override addon settings.

Shopify is the first addon. The operator declares a lane's store key, API version
and ``test_store`` flag as literals, and points the API token and webhook key at
existing managed secret bindings. Plaintext secret values never enter this path.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideApplyPhase,
    OdooOverrideApplyResult,
    OdooOverrideValue,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.odoo_instance_overrides import (
    DEFAULT_SHOPIFY_PRODUCTION_INDICATORS,
    SHOPIFY_ACTION_APPLY,
    SHOPIFY_ACTION_SETTING,
    SHOPIFY_ADDON_NAME,
    _resolve_shopify_payload_settings,
    addon_setting_secret_env_key,
    odoo_instance_is_production,
    shopify_store_key_is_protected,
)

ODOO_ADDON_SETTINGS_APPLY_ROUTE = "/v1/product-config/odoo-addon-settings/apply"
ODOO_ADDON_SETTINGS_SOURCE_LABEL: Literal["service:odoo-addon-settings"] = (
    "service:odoo-addon-settings"
)

OdooAddonSettingsMode = Literal["dry-run", "apply"]
OdooAddonSettingsRefusalCode = Literal[
    "protected_store_key",
    "production_like_store_key",
    "production_test_store",
    "secret_binding_missing",
    "secret_binding_invalid",
    "target_policy_missing",
    "render_refused",
]

SHOPIFY_NON_SECRET_SETTINGS = frozenset(
    {"shop_url_key", "api_version", "test_store", "allow_production", "production_indicators"}
)
SHOPIFY_SECRET_BINDING_SETTINGS = ("api_token", "webhook_key")
_SHOPIFY_API_VERSION_PATTERN = re.compile(r"^(?:[0-9]{4}-[0-9]{2}|unstable)$")
_SHOPIFY_STORE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,254}$")
# Shopify credential prefixes. A value with one of these in a literal or binding field is
# a pasted secret, so refuse it without echoing it back.
_SHOPIFY_CREDENTIAL_PREFIXES = ("shpat_", "shpss_", "shpca_", "shppa_", "shpua_")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class OdooAddonSettingsRefusal(ValueError):
    def __init__(self, code: OdooAddonSettingsRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class OdooAddonSettingsStale(ValueError):
    pass


class OdooAddonSettingsStore(Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> object: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...

    def read_secret_record(self, secret_id: str) -> SecretRecord: ...


def _looks_like_shopify_credential(value: str) -> bool:
    return value.strip().lower().startswith(_SHOPIFY_CREDENTIAL_PREFIXES)


class OdooShopifyAddonSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_url_key: str
    api_version: str
    api_token_secret_binding_id: str
    webhook_key_secret_binding_id: str
    test_store: StrictBool

    @model_validator(mode="after")
    def _validate_input(self) -> OdooShopifyAddonSettingsInput:
        self.shop_url_key = self.shop_url_key.strip().lower()
        self.api_version = self.api_version.strip()
        self.api_token_secret_binding_id = self.api_token_secret_binding_id.strip()
        self.webhook_key_secret_binding_id = self.webhook_key_secret_binding_id.strip()
        for field_name in (
            "shop_url_key",
            "api_token_secret_binding_id",
            "webhook_key_secret_binding_id",
        ):
            if _looks_like_shopify_credential(getattr(self, field_name)):
                raise ValueError(
                    f"Shopify {field_name} looks like a credential. Store credentials as "
                    "managed secrets and reference their binding ids."
                )
        if not _SHOPIFY_STORE_KEY_PATTERN.fullmatch(self.shop_url_key):
            raise ValueError(
                "Shopify shop_url_key must be a store handle or myshopify domain without "
                "a scheme, path or whitespace."
            )
        if not _SHOPIFY_API_VERSION_PATTERN.fullmatch(self.api_version):
            raise ValueError("Shopify api_version must look like YYYY-MM or be 'unstable'.")
        if not self.api_token_secret_binding_id:
            raise ValueError("Shopify settings require api_token_secret_binding_id.")
        if not self.webhook_key_secret_binding_id:
            raise ValueError("Shopify settings require webhook_key_secret_binding_id.")
        if self.api_token_secret_binding_id == self.webhook_key_secret_binding_id:
            raise ValueError("Shopify api_token and webhook_key must use different bindings.")
        return self

    def secret_binding_ids(self) -> dict[str, str]:
        return {
            "api_token": self.api_token_secret_binding_id,
            "webhook_key": self.webhook_key_secret_binding_id,
        }


class OdooAddonSettingsApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    addon: Literal["shopify"] = "shopify"
    mode: OdooAddonSettingsMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""
    shopify: OdooShopifyAddonSettingsInput

    @model_validator(mode="after")
    def _validate_request(self) -> OdooAddonSettingsApplyRequest:
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        for field_name in ("product", "context", "instance", "reason"):
            if not getattr(self, field_name):
                raise ValueError(f"Odoo addon settings request requires {field_name}.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Odoo addon settings dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply" and not _SHA256_PATTERN.fullmatch(self.reviewed_plan_sha256):
            raise ValueError(
                "Odoo addon settings apply requires the reviewed 64-character plan SHA-256."
            )
        return self


class OdooAddonSettingEvidence(BaseModel):
    """Redacted view of one addon setting: names, presence and non-secret values only."""

    model_config = ConfigDict(extra="forbid")

    setting: str
    source: Literal["literal", "secret_binding"]
    value: str | bool | None = None
    value_present: bool
    secret_binding_id: str = ""
    secret_binding_present: bool | None = None


class OdooAddonSettingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    setting: str
    action: Literal["add", "update", "remove", "unchanged"]
    before: OdooAddonSettingEvidence | None = None
    after: OdooAddonSettingEvidence | None = None


class OdooAddonSettingsPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: OdooAddonSettingsMode
    product: str
    context: str
    instance: str
    addon: Literal["shopify"]
    production_lane: bool
    record_exists: bool
    changed: bool
    applied: bool = False
    rendered_action: Literal["apply"] = "apply"
    changes: tuple[OdooAddonSettingChange, ...]
    read_back: tuple[OdooAddonSettingEvidence, ...] = ()
    read_back_matches: bool | None = None
    reason: str
    source_label: Literal["service:odoo-addon-settings"] = ODOO_ADDON_SETTINGS_SOURCE_LABEL
    record_sha256_before: str
    record_sha256_after: str = ""
    plan_sha256: str
    next_actions: tuple[str, ...] = ()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _record_sha256(record: OdooInstanceOverrideRecord | None) -> str:
    if record is None:
        return ""
    return _canonical_sha256(record.model_dump(mode="json"))


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_existing_record(
    *, record_store: OdooAddonSettingsStore, context: str, instance: str
) -> OdooInstanceOverrideRecord | None:
    try:
        return record_store.read_odoo_instance_override_record(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError:
        return None


def _protected_store_keys(
    *, record_store: OdooAddonSettingsStore, context: str, instance: str
) -> tuple[str, ...]:
    try:
        target = record_store.read_dokploy_target_record(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError as error:
        raise OdooAddonSettingsRefusal(
            "target_policy_missing",
            "Odoo addon settings require the lane's tracked target record so its protected "
            "Shopify store keys can be checked.",
        ) from error
    return target.policies.shopify.protected_store_keys


def _validate_store_key(*, shop_url_key: str, protected_store_keys: tuple[str, ...]) -> None:
    if shopify_store_key_is_protected(shop_url_key, protected_store_keys):
        raise OdooAddonSettingsRefusal(
            "protected_store_key",
            "Shopify shop_url_key is in this lane's protected store keys.",
        )
    for indicator in DEFAULT_SHOPIFY_PRODUCTION_INDICATORS:
        if indicator in shop_url_key:
            raise OdooAddonSettingsRefusal(
                "production_like_store_key",
                f"Shopify shop_url_key looks production-like (matches {indicator!r}).",
            )


def _validated_secret_bindings(
    *,
    record_store: OdooAddonSettingsStore,
    request: OdooAddonSettingsApplyRequest,
) -> dict[str, SecretBinding]:
    lane_bindings = {
        binding.binding_id: binding
        for binding in record_store.list_secret_bindings(
            context_name=request.context, instance_name=request.instance
        )
    }
    validated: dict[str, SecretBinding] = {}
    for setting_name, binding_id in request.shopify.secret_binding_ids().items():
        expected_key = addon_setting_secret_env_key(
            addon_name=SHOPIFY_ADDON_NAME, setting_name=setting_name
        )
        binding = lane_bindings.get(binding_id)
        # Never echo the submitted binding id: a misplaced credential would leak here.
        if binding is None:
            raise OdooAddonSettingsRefusal(
                "secret_binding_missing",
                f"Shopify {setting_name} references a secret binding that does not exist for "
                f"this lane. Create it through product-config with binding key {expected_key}.",
            )
        if binding.context != request.context or binding.instance != request.instance:
            raise OdooAddonSettingsRefusal(
                "secret_binding_invalid",
                f"Shopify {setting_name} secret binding must be scoped to this exact lane.",
            )
        if binding.status != "configured":
            raise OdooAddonSettingsRefusal(
                "secret_binding_invalid",
                f"Shopify {setting_name} secret binding is not configured.",
            )
        if binding.binding_key != expected_key:
            raise OdooAddonSettingsRefusal(
                "secret_binding_invalid",
                f"Shopify {setting_name} secret binding must use binding key {expected_key}.",
            )
        try:
            secret = record_store.read_secret_record(binding.secret_id)
        except FileNotFoundError as error:
            raise OdooAddonSettingsRefusal(
                "secret_binding_missing",
                f"Shopify {setting_name} secret binding has no managed secret.",
            ) from error
        if secret.status != "configured":
            raise OdooAddonSettingsRefusal(
                "secret_binding_invalid",
                f"Shopify {setting_name} managed secret is not configured.",
            )
        validated[setting_name] = binding
    return validated


def _desired_shopify_settings(
    request: OdooAddonSettingsApplyRequest,
) -> tuple[OdooAddonSettingOverride, ...]:
    shopify = request.shopify

    def literal(setting: str, value: str | bool) -> OdooAddonSettingOverride:
        return OdooAddonSettingOverride(
            addon=SHOPIFY_ADDON_NAME,
            setting=setting,
            value=OdooOverrideValue(source="literal", value=value),
        )

    def secret(setting: str, binding_id: str) -> OdooAddonSettingOverride:
        return OdooAddonSettingOverride(
            addon=SHOPIFY_ADDON_NAME,
            setting=setting,
            value=OdooOverrideValue(source="secret_binding", secret_binding_id=binding_id),
        )

    return (
        literal("shop_url_key", shopify.shop_url_key),
        secret("api_token", shopify.api_token_secret_binding_id),
        secret("webhook_key", shopify.webhook_key_secret_binding_id),
        literal("api_version", shopify.api_version),
        literal("test_store", shopify.test_store),
    )


def redacted_addon_setting_evidence(
    override: OdooAddonSettingOverride,
    *,
    present_binding_ids: frozenset[str] | None = None,
) -> OdooAddonSettingEvidence:
    value = override.value
    if value.source == "secret_binding":
        return OdooAddonSettingEvidence(
            setting=override.setting,
            source="secret_binding",
            value_present=True,
            secret_binding_id=value.secret_binding_id,
            secret_binding_present=(
                None
                if present_binding_ids is None
                else value.secret_binding_id in present_binding_ids
            ),
        )
    # Only settings known to be non-secret expose their literal value. A legacy record
    # that stored a credential as a literal shows presence only.
    visible_value: str | bool | None = None
    if override.setting in SHOPIFY_NON_SECRET_SETTINGS and isinstance(value.value, str | bool):
        visible_value = value.value
    return OdooAddonSettingEvidence(
        setting=override.setting,
        source="literal",
        value=visible_value,
        value_present=value.value is not None and str(value.value).strip() != "",
    )


def _setting_changes(
    *,
    before: tuple[OdooAddonSettingOverride, ...],
    after: tuple[OdooAddonSettingOverride, ...],
    present_binding_ids: frozenset[str],
) -> tuple[OdooAddonSettingChange, ...]:
    before_by_setting = {override.setting: override for override in before}
    after_by_setting = {override.setting: override for override in after}
    changes: list[OdooAddonSettingChange] = []
    for setting in sorted(set(before_by_setting) | set(after_by_setting)):
        old = before_by_setting.get(setting)
        new = after_by_setting.get(setting)
        if old is None:
            action: Literal["add", "update", "remove", "unchanged"] = "add"
        elif new is None:
            action = "remove"
        elif old.value == new.value:
            action = "unchanged"
        else:
            action = "update"
        changes.append(
            OdooAddonSettingChange(
                setting=setting,
                action=action,
                before=(
                    redacted_addon_setting_evidence(old, present_binding_ids=present_binding_ids)
                    if old is not None
                    else None
                ),
                after=(
                    redacted_addon_setting_evidence(new, present_binding_ids=present_binding_ids)
                    if new is not None
                    else None
                ),
            )
        )
    return tuple(changes)


def _replacement_record(
    *,
    existing: OdooInstanceOverrideRecord | None,
    request: OdooAddonSettingsApplyRequest,
    desired: tuple[OdooAddonSettingOverride, ...],
    updated_at: str,
) -> OdooInstanceOverrideRecord:
    preserved_addon_settings = tuple(
        override
        for override in (existing.addon_settings if existing is not None else ())
        if override.addon != SHOPIFY_ADDON_NAME
    )
    default_phases: tuple[OdooOverrideApplyPhase, ...] = ("deploy", "promotion")
    apply_on: list[OdooOverrideApplyPhase] = []
    for phase in (*(existing.apply_on if existing is not None else ()), *default_phases):
        if phase not in apply_on:
            apply_on.append(phase)
    return OdooInstanceOverrideRecord(
        context=request.context,
        instance=request.instance,
        apply_on=tuple(apply_on),
        config_parameters=existing.config_parameters if existing is not None else (),
        addon_settings=(*preserved_addon_settings, *desired),
        website_bootstrap=existing.website_bootstrap if existing is not None else None,
        last_apply=existing.last_apply if existing is not None else OdooOverrideApplyResult(),
        updated_at=updated_at,
        source_label=ODOO_ADDON_SETTINGS_SOURCE_LABEL,
    )


def build_odoo_addon_settings_plan(
    *,
    record_store: OdooAddonSettingsStore,
    request: OdooAddonSettingsApplyRequest,
) -> tuple[OdooAddonSettingsPlan, OdooInstanceOverrideRecord | None, OdooInstanceOverrideRecord]:
    """Validate the request against current authority and return a digest-bound plan.

    Returns the plan, the current record (or None) and the replacement record.
    """

    production_lane = odoo_instance_is_production(request.instance)
    if production_lane and request.shopify.test_store:
        raise OdooAddonSettingsRefusal(
            "production_test_store",
            "Shopify test_store cannot be set on a production lane.",
        )
    protected_store_keys = _protected_store_keys(
        record_store=record_store, context=request.context, instance=request.instance
    )
    _validate_store_key(
        shop_url_key=request.shopify.shop_url_key, protected_store_keys=protected_store_keys
    )
    bindings = _validated_secret_bindings(record_store=record_store, request=request)

    existing = _read_existing_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    desired = _desired_shopify_settings(request)
    replacement = _replacement_record(
        existing=existing,
        request=request,
        desired=desired,
        updated_at=existing.updated_at if existing is not None else _utc_now_timestamp(),
    )
    # Prove the post-deploy renderer accepts the replacement and renders an apply action.
    try:
        rendered = _resolve_shopify_payload_settings(
            record=replacement, protected_shopify_store_keys=protected_store_keys
        )
    except click.ClickException as error:
        raise OdooAddonSettingsRefusal("render_refused", error.message) from error
    rendered_action = next(
        (setting.value.value for setting in rendered if setting.setting == SHOPIFY_ACTION_SETTING),
        None,
    )
    if rendered_action != SHOPIFY_ACTION_APPLY:
        raise OdooAddonSettingsRefusal(
            "render_refused",
            "Shopify settings did not render an apply action for post-deploy.",
        )

    existing_shopify = tuple(
        override
        for override in (existing.addon_settings if existing is not None else ())
        if override.addon == SHOPIFY_ADDON_NAME
    )
    present_binding_ids = frozenset(binding.binding_id for binding in bindings.values())
    changes = _setting_changes(
        before=existing_shopify, after=desired, present_binding_ids=present_binding_ids
    )
    changed = any(change.action != "unchanged" for change in changes) or (
        existing is None or tuple(existing.apply_on) != tuple(replacement.apply_on)
    )
    record_sha256_before = _record_sha256(existing)
    plan_sha256 = _canonical_sha256(
        {
            "product": request.product,
            "context": request.context,
            "instance": request.instance,
            "addon": request.addon,
            "record_sha256_before": record_sha256_before,
            "desired": [override.model_dump(mode="json") for override in desired],
            "protected_store_keys_sha256": _canonical_sha256(sorted(protected_store_keys)),
            "bindings": {
                setting: binding.model_dump(mode="json")
                for setting, binding in sorted(bindings.items())
            },
        }
    )
    plan = OdooAddonSettingsPlan(
        mode=request.mode,
        product=request.product,
        context=request.context,
        instance=request.instance,
        addon=request.addon,
        production_lane=production_lane,
        record_exists=existing is not None,
        changed=changed,
        changes=changes,
        reason=request.reason,
        record_sha256_before=record_sha256_before,
        plan_sha256=plan_sha256,
        next_actions=(
            "Run Odoo post-deploy for this lane so its database receives the settings, "
            "then verify the store key and test_store in the lane database.",
        ),
    )
    return plan, existing, replacement


def apply_odoo_addon_settings_plan(
    *,
    record_store: OdooAddonSettingsStore,
    request: OdooAddonSettingsApplyRequest,
) -> OdooAddonSettingsPlan:
    """Re-plan against current authority, require the reviewed digest, write, read back."""

    plan, _existing, replacement = build_odoo_addon_settings_plan(
        record_store=record_store, request=request
    )
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        raise OdooAddonSettingsStale(
            "Reviewed Odoo addon settings plan no longer matches current authority."
        )
    applied = False
    if plan.changed:
        record_store.write_odoo_instance_override_record(
            replacement.model_copy(update={"updated_at": _utc_now_timestamp()})
        )
        applied = True
    stored = _read_existing_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    stored_shopify = tuple(
        override
        for override in (stored.addon_settings if stored is not None else ())
        if override.addon == SHOPIFY_ADDON_NAME
    )
    present_binding_ids = frozenset(
        binding.binding_id
        for binding in record_store.list_secret_bindings(
            context_name=request.context, instance_name=request.instance
        )
        if binding.status == "configured"
    )
    read_back = tuple(
        redacted_addon_setting_evidence(override, present_binding_ids=present_binding_ids)
        for override in stored_shopify
    )
    desired = _desired_shopify_settings(request)
    read_back_matches = sorted(
        (override.setting, override.value.model_dump_json()) for override in stored_shopify
    ) == sorted((override.setting, override.value.model_dump_json()) for override in desired)
    return plan.model_copy(
        update={
            "applied": applied,
            "read_back": read_back,
            "read_back_matches": read_back_matches,
            "record_sha256_after": _record_sha256(stored),
        }
    )
