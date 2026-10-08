"""Plan the service's Delivery App selector without copying or minting credentials."""

from datetime import UTC, datetime
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.launchplane_github_delivery import (
    DELIVERY_GITHUB_APP_ID_KEY,
    DELIVERY_GITHUB_APP_INTEGRATION_KEY,
)
from control_plane.product_config import plan_product_config_authority_bundle
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    SecretCopySourceExpectation,
)

GITHUB_DELIVERY_CONFIGURATION_ROUTE = "/v1/service/github-delivery/configuration"


class DeliveryGitHubAppConfigurationResponse(BaseModel):
    status: Literal["ok"] = "ok"
    actor: str
    reason: str
    mode: Literal["dry-run", "apply"]
    app_id: int
    integration: str
    plan_digest: str
    trace_id: str
    runtime_environment: dict[str, object] | None


class DeliveryGitHubAppConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    app_id: int = Field(strict=True, gt=0)
    integration: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    reason: str = Field(min_length=1, max_length=2000)
    expected_plan_digest: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "DeliveryGitHubAppConfigurationRequest":
        if not self.reason.strip():
            raise ValueError("Delivery App configuration requires a reason.")
        if self.mode == "apply" and (
            len(self.expected_plan_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.expected_plan_digest)
        ):
            raise ValueError("Delivery App apply requires the reviewed plan digest.")
        if self.mode == "dry-run" and self.expected_plan_digest:
            raise ValueError("Delivery App dry-run does not accept a reviewed plan digest.")
        return self


def _key_binding(
    store: PostgresRecordStore, integration: str
) -> tuple[SecretRecord, SecretBinding]:
    bindings = tuple(
        binding
        for binding in store.list_secret_bindings(
            integration=integration, context_name="launchplane", limit=None
        )
        if binding.integration == integration
        and binding.context == "launchplane"
        and not binding.instance
        and binding.binding_key == "private_key"
        and binding.status == "configured"
    )
    if len(bindings) != 1:
        raise ValueError(
            "Delivery App requires one configured service-context private_key binding."
        )
    binding = bindings[0]
    record = store.read_secret_record(binding.secret_id)
    if (
        record.secret_id != binding.secret_id
        or record.scope != "context"
        or record.context != "launchplane"
        or record.instance
        or record.integration != integration
        or record.status != "configured"
        or record.policy != "write_only"
    ):
        raise ValueError("Delivery App key is not an exact configured service-context secret.")
    return record, binding


def plan_delivery_github_configuration(
    *,
    store: PostgresRecordStore,
    request: DeliveryGitHubAppConfigurationRequest,
    actor: str,
) -> tuple[dict[str, object], ProductAuthorityBundle]:
    record, binding = _key_binding(store, request.integration)
    result, bundle = plan_product_config_authority_bundle(
        record_store=store,
        payload={
            "product": "launchplane",
            "context": "launchplane",
            "runtime_env": {
                "scope": "context",
                "env": {
                    DELIVERY_GITHUB_APP_ID_KEY: str(request.app_id),
                    DELIVERY_GITHUB_APP_INTEGRATION_KEY: request.integration,
                },
            },
        },
        mode="apply",
        actor=actor,
        source_label="service:github-delivery-configuration",
    )
    # Only expectations are hashed: newly planned record timestamps are deliberately absent.
    baseline = [
        write.expected_record.model_dump(mode="json") if write.expected_record else None
        for write in bundle.runtime_environment_writes
    ]
    digest = hashlib.sha256(
        json.dumps(
            {
                "app_id": request.app_id,
                "integration": request.integration,
                "reason": request.reason,
                "actor": actor,
                "baseline": baseline,
                "secret_record": record.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    summary = {
        "status": "ok",
        "actor": actor,
        "reason": request.reason,
        "mode": request.mode,
        "app_id": request.app_id,
        "integration": request.integration,
        "plan_digest": digest,
        "runtime_environment": result["runtime_environment"],
    }
    return summary, bundle.model_copy(
        update={
            "secret_copy_sources": (SecretCopySourceExpectation(record=record, binding=binding),),
        }
    )


def apply_delivery_github_configuration(
    *,
    store: PostgresRecordStore,
    request: DeliveryGitHubAppConfigurationRequest,
    actor: str,
    trace_id: str,
    idempotency_key: str,
) -> dict[str, object]:
    scope = f"github-delivery-configuration:{actor}"
    fingerprint = hashlib.sha256(
        json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    if request.mode == "apply":
        if not idempotency_key.strip():
            raise ValueError("Delivery App apply requires Idempotency-Key.")
        previous = store.read_idempotency_record(
            scope=scope,
            route_path=GITHUB_DELIVERY_CONFIGURATION_ROUTE,
            idempotency_key=idempotency_key,
        )
        if previous is not None:
            if previous.state != "completed" or previous.request_fingerprint != fingerprint:
                raise ValueError("Delivery App configuration idempotency conflict.")
            return dict(previous.response_payload)
    summary, bundle = plan_delivery_github_configuration(store=store, request=request, actor=actor)
    summary["trace_id"] = trace_id
    if request.mode == "dry-run":
        return summary
    if request.expected_plan_digest != summary["plan_digest"]:
        raise ValueError("Delivery App configuration changed since the reviewed dry-run.")
    now = datetime.now(UTC).isoformat()
    receipt = LaunchplaneIdempotencyRecord(
        record_id=f"idempotency-{trace_id}",
        scope=scope,
        route_path=GITHUB_DELIVERY_CONFIGURATION_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        response_status_code=200,
        response_trace_id=trace_id,
        recorded_at=now,
        response_payload=summary,
    )
    store.write_product_authority_bundle(bundle.model_copy(update={"idempotency_record": receipt}))
    return summary
