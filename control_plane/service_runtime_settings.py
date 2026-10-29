import hashlib
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.delivery_apply_receipt import apply_reviewed_service_bundle
from control_plane.contracts.merge_train_stall import (
    STALL_WORKFLOW_REF_KEY,
    STALL_WORKFLOW_REPOSITORY_KEY,
    parse_merge_train_stall_ref,
    parse_merge_train_stall_repository,
)
from control_plane.discussion_reminders import (
    SETTINGS_KEY as DISCUSSION_REMINDERS_SETTING,
    DiscussionReminderSettings,
)
from control_plane.dokploy.memory_capacity import (
    PREVIEW_MEMORY_FLOORS_SETTING,
    parse_preview_memory_floors,
)
from control_plane.product_config import plan_product_config_authority_bundle
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256

SERVICE_RUNTIME_SETTINGS_ROUTE = "/v1/service/runtime-settings"
SERVICE_CONTEXT = "launchplane"
SERVICE_RUNTIME_SETTINGS_PLAN_ACTION = "launchplane_service.runtime_settings.plan"
SERVICE_RUNTIME_SETTINGS_APPLY_ACTION = "launchplane_service.runtime_settings.apply"
SETTING_VALUE_LIMIT = 65536

DECLARED_SERVICE_SETTINGS: Mapping[str, Callable[[str], object]] = MappingProxyType(
    {
        PREVIEW_MEMORY_FLOORS_SETTING: parse_preview_memory_floors,
        DISCUSSION_REMINDERS_SETTING: DiscussionReminderSettings.model_validate_json,
        STALL_WORKFLOW_REPOSITORY_KEY: parse_merge_train_stall_repository,
        STALL_WORKFLOW_REF_KEY: parse_merge_train_stall_ref,
    }
)


class ServiceRuntimeSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    key: str
    value: str = Field(max_length=SETTING_VALUE_LIMIT)
    reason: str = Field(min_length=1, max_length=2000)
    expected_plan_digest: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="after")
    def validate_declared_setting(self) -> "ServiceRuntimeSettingRequest":
        if not self.reason.strip():
            raise ValueError("Service setting changes require a reason.")
        if (self.mode == "apply") != bool(self.expected_plan_digest):
            raise ValueError("Apply requires the reviewed plan digest; dry-run accepts none.")
        validate_value = DECLARED_SERVICE_SETTINGS.get(self.key)
        if validate_value is None:
            raise ValueError("Only declared non-secret service settings can be set here.")
        validate_value(self.value)
        return self


def plan_service_runtime_setting(
    *,
    store: PostgresRecordStore,
    request: ServiceRuntimeSettingRequest,
    actor: str,
) -> tuple[dict[str, object], ProductAuthorityBundle]:
    result, bundle = plan_product_config_authority_bundle(
        record_store=store,
        payload={
            "product": SERVICE_CONTEXT,
            "context": SERVICE_CONTEXT,
            "runtime_env": {"scope": "context", "env": {request.key: request.value}},
        },
        mode="apply",
        actor=actor,
        source_label="service:runtime-settings",
    )
    baseline = [
        write.expected_record.model_dump(mode="json") if write.expected_record else None
        for write in bundle.runtime_environment_writes
    ]
    value_sha256 = hashlib.sha256(request.value.encode()).hexdigest()
    digest = nonvalidating_compact_sorted_json_sha256(
        {
            "key": request.key,
            "value_sha256": value_sha256,
            "reason": request.reason,
            "actor": actor,
            "baseline": baseline,
        }
    )
    summary: dict[str, object] = {
        "status": "ok",
        "actor": actor,
        "reason": request.reason,
        "mode": request.mode,
        "key": request.key,
        "value_sha256": value_sha256,
        "plan_digest": digest,
        "runtime_environment": result["runtime_environment"],
    }
    return summary, bundle


def apply_service_runtime_setting(
    *,
    store: PostgresRecordStore,
    request: ServiceRuntimeSettingRequest,
    actor: str,
    trace_id: str,
    idempotency_key: str,
) -> dict[str, object]:
    return apply_reviewed_service_bundle(
        store=store,
        request=request,
        actor=actor,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        scope=f"service-runtime-settings:{actor}",
        route_path=SERVICE_RUNTIME_SETTINGS_ROUTE,
        plan=lambda: plan_service_runtime_setting(store=store, request=request, actor=actor),
    )
