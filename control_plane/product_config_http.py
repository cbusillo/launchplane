from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.dokploy_target_record import DokployTargetRecord


class ProductConfigApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: str
    product: str
    context: str = ""
    instance: str = ""
    source_label: str = "product-config-api"
    reason: str = ""
    runtime_env: dict[str, object] | None = None
    runtime_environment: dict[str, object] | None = None
    secrets: list[dict[str, object]] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized_value = value.strip().lower()
        if normalized_value not in {"dry-run", "apply"}:
            raise ValueError("Product config mode must be 'dry-run' or 'apply'.")
        return normalized_value

    @model_validator(mode="after")
    def _validate_product(self) -> "ProductConfigApplyEnvelope":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.source_label = self.source_label.strip() or "product-config-api"
        self.reason = self.reason.strip()
        if not self.product:
            raise ValueError("Product config apply requires product.")
        return self

    def product_config_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "product": self.product,
            "context": self.context,
            "instance": self.instance,
            "secrets": self.secrets,
        }
        if self.runtime_env is not None:
            payload["runtime_env"] = self.runtime_env
        if self.runtime_environment is not None:
            payload["runtime_environment"] = self.runtime_environment
        return payload


def product_config_live_target_next_actions(
    *,
    request: ProductConfigApplyEnvelope,
    driver_result: dict[str, object] | None,
    tracked_targets: tuple[DokployTargetRecord, ...],
) -> list[dict[str, object]]:
    if request.mode != "apply" or not driver_result:
        return []
    runtime_environment = driver_result.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        return []
    changed_keys = runtime_environment.get("changed_keys")
    if not isinstance(changed_keys, list) or not changed_keys:
        return []
    context_name = str(runtime_environment.get("context") or "")
    instance_name = str(runtime_environment.get("instance") or "")
    target = next(
        (
            record
            for record in tracked_targets
            if record.context == context_name and record.instance == instance_name
        ),
        None,
    )
    if target is None:
        return []
    return [
        {
            "kind": "live_target_runtime_apply",
            "required": True,
            "status": "live_sync_required",
            "target": {
                "context": context_name,
                "instance": instance_name,
                "target_type": target.target_type,
                "target_name": target.target_name,
            },
            "changed_keys": sorted(str(key) for key in changed_keys),
            "dry_run": {
                "method": "POST",
                "endpoint": "/v1/live-target-runtime/apply",
                "mode": "dry-run",
            },
            "apply": {
                "method": "POST",
                "endpoint": "/v1/live-target-runtime/apply",
                "mode": "apply",
            },
            "instruction": (
                "Run live-target-runtime dry-run, then apply with a concrete reason. "
                "Redeploying the same app image does not sync the live Dokploy target environment."
            ),
        }
    ]
