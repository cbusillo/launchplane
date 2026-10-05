"""Metadata-only service controls; Director confirmation retires selected PAT bindings."""

from datetime import UTC, datetime
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding, SecretRecord
from control_plane.github_app_configuration import ADVISORY_GITHUB_APP_ID_ENV_KEY
from control_plane.github_delivery_configuration import _key_binding
from control_plane.launchplane_github_delivery import (
    DELIVERY_GITHUB_APP_ID_KEY,
    DELIVERY_GITHUB_APP_INTEGRATION_KEY,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    RuntimeEnvironmentSetExpectation,
    SecretBindingSetExpectation,
    SecretCopySourceExpectation,
)

SERVICE_GITHUB_DELIVERY_ROUTE = "/v1/service/github-delivery"
SERVICE_TOKEN_RETIREMENT_ROUTE = SERVICE_GITHUB_DELIVERY_ROUTE + "/token-retirement"
_SERVICE_INTEGRATION = "launchplane_service"


class ExistingDeliveryKey(BaseModel):
    integration: str
    secret_id: str
    binding_id: str
    context: str


class ObsoleteServiceToken(BaseModel):
    secret_id: str
    scope: Literal["global", "context"]
    context: str
    status: Literal["configured", "disabled"]
    binding_ids: tuple[str, ...]


class ServiceGitHubDeliveryStatus(BaseModel):
    status: Literal["ok"] = "ok"
    trace_id: str
    app_id: str
    integration: str
    advisory_app_id: str
    existing_keys: tuple[ExistingDeliveryKey, ...]
    obsolete_tokens: tuple[ObsoleteServiceToken, ...]


class ServiceTokenRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    secret_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)
    # These are Director attestations, not automatically verified GitHub receipts.
    advisory_check_url: str = Field(pattern=r"^https://github\.com/[^/]+/[^/]+/.*", max_length=1000)
    delivery_comment_url: str = Field(
        pattern=r"^https://github\.com/[^/]+/[^/]+/(pull|issues)/[0-9]+#issuecomment-[0-9]+$",
        max_length=1000,
    )
    delivery_release_issue_url: str = Field(
        pattern=r"^https://github\.com/[^/]+/[^/]+/issues/[0-9]+$", max_length=1000
    )
    consumer_check_evidence: str = Field(min_length=1, max_length=2000)
    director_confirmed: bool = False
    expected_plan_digest: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "ServiceTokenRetirementRequest":
        if len(set(self.secret_ids)) != len(self.secret_ids) or any(
            not item.strip() for item in self.secret_ids
        ):
            raise ValueError("Select distinct, named service-token records.")
        if not self.reason.strip() or not self.consumer_check_evidence.strip():
            raise ValueError("Retirement requires a reason and remaining-consumer evidence.")
        if self.mode == "apply":
            if not self.director_confirmed:
                raise ValueError("The Director must confirm receipt identities and retirement.")
            if len(self.expected_plan_digest) != 64 or any(
                item not in "0123456789abcdef" for item in self.expected_plan_digest
            ):
                raise ValueError("Retirement requires the reviewed plan digest.")
        elif self.expected_plan_digest or self.director_confirmed:
            raise ValueError("Review the dry-run before confirming retirement.")
        return self


class ServiceTokenRetirementResponse(BaseModel):
    status: Literal["ok"] = "ok"
    trace_id: str
    mode: Literal["dry-run", "apply"]
    plan_digest: str
    tokens: tuple[ObsoleteServiceToken, ...]
    app_id: str
    advisory_app_id: str
    actor: str
    reason: str


def _app_values(records: tuple[RuntimeEnvironmentRecord, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for scope in ("global", "context"):
        for record in records:
            if record.scope == scope and (scope == "global" or record.context == "launchplane"):
                for key in (
                    DELIVERY_GITHUB_APP_ID_KEY,
                    DELIVERY_GITHUB_APP_INTEGRATION_KEY,
                    ADVISORY_GITHUB_APP_ID_ENV_KEY,
                ):
                    if key in record.env:
                        values[key] = str(record.env[key])
    return values


def _token_bindings(
    record: SecretRecord, bindings: tuple[SecretBinding, ...]
) -> tuple[SecretBinding, ...]:
    consumers = tuple(binding for binding in bindings if binding.secret_id == record.secret_id)
    if (
        record.integration != _SERVICE_INTEGRATION
        or record.scope not in {"global", "context"}
        or record.instance
        or (record.scope == "global" and record.context)
        or not consumers
        or any(
            binding.integration != _SERVICE_INTEGRATION
            or binding.binding_key != "GITHUB_TOKEN"
            or binding.context != record.context
            or binding.instance
            for binding in consumers
        )
    ):
        raise ValueError(
            "Only exact service GITHUB_TOKEN records with no other consumers may retire."
        )
    return tuple(sorted(consumers, key=lambda item: item.binding_id))


def _token_summary(
    record: SecretRecord, bindings: tuple[SecretBinding, ...]
) -> ObsoleteServiceToken:
    return ObsoleteServiceToken(
        secret_id=record.secret_id,
        scope="global" if record.scope == "global" else "context",
        context=record.context,
        status=record.status,
        binding_ids=tuple(binding.binding_id for binding in bindings),
    )


def read_service_github_delivery(
    *, store: PostgresRecordStore, trace_id: str
) -> ServiceGitHubDeliveryStatus:
    values = _app_values(store.list_runtime_environment_records())
    keys: list[ExistingDeliveryKey] = []
    for integration in sorted(
        {binding.integration for binding in store.list_secret_bindings(context_name="launchplane")}
    ):
        try:
            record, binding = _key_binding(store, integration)
        except (ValueError, FileNotFoundError):
            continue
        keys.append(
            ExistingDeliveryKey(
                integration=integration,
                secret_id=record.secret_id,
                binding_id=binding.binding_id,
                context=binding.context,
            )
        )
    bindings = store.list_secret_bindings()
    tokens: list[ObsoleteServiceToken] = []
    for record in store.list_secret_records(integration=_SERVICE_INTEGRATION):
        try:
            consumers = _token_bindings(record, bindings)
        except ValueError:
            continue
        tokens.append(_token_summary(record, consumers))
    return ServiceGitHubDeliveryStatus(
        trace_id=trace_id,
        app_id=values.get(DELIVERY_GITHUB_APP_ID_KEY, ""),
        integration=values.get(DELIVERY_GITHUB_APP_INTEGRATION_KEY, ""),
        advisory_app_id=values.get(ADVISORY_GITHUB_APP_ID_ENV_KEY, ""),
        existing_keys=tuple(keys),
        obsolete_tokens=tuple(sorted(tokens, key=lambda item: item.secret_id)),
    )


def plan_service_token_retirement(
    *, store: PostgresRecordStore, request: ServiceTokenRetirementRequest, actor: str, trace_id: str
) -> tuple[ServiceTokenRetirementResponse, ProductAuthorityBundle]:
    runtime = tuple(
        record
        for record in store.list_runtime_environment_records()
        if record.scope == "global"
        or (record.scope == "context" and record.context == "launchplane")
    )
    values = _app_values(runtime)
    app_id = values.get(DELIVERY_GITHUB_APP_ID_KEY, "")
    advisory_id = values.get(ADVISORY_GITHUB_APP_ID_ENV_KEY, "")
    if any(not value.isdecimal() or int(value) < 1 for value in (app_id, advisory_id)):
        raise ValueError("Configure Delivery and Advisory Apps before retiring service tokens.")
    key_record, key_binding = _key_binding(
        store, values.get(DELIVERY_GITHUB_APP_INTEGRATION_KEY, "")
    )
    all_bindings = store.list_secret_bindings()
    records = tuple(store.read_secret_record(secret_id) for secret_id in sorted(request.secret_ids))
    binding_sets = tuple(_token_bindings(record, all_bindings) for record in records)
    if any(record.status != "configured" for record in records):
        raise ValueError("Selected service-token records are already disabled.")
    reviewed_request = request.model_dump(
        mode="json", exclude={"mode", "expected_plan_digest", "director_confirmed"}
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "actor": actor,
                "request": reviewed_request,
                "records": [record.model_dump(mode="json") for record in records],
                "bindings": [
                    [binding.model_dump(mode="json") for binding in items] for items in binding_sets
                ],
                "runtime": [record.model_dump(mode="json") for record in runtime],
                "key": key_record.model_dump(mode="json"),
                "key_binding": key_binding.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    now = datetime.now(UTC).isoformat()
    bundle = ProductAuthorityBundle(
        runtime_environment_read_sets=(
            RuntimeEnvironmentSetExpectation(
                contexts=("launchplane",), include_global=True, records=runtime
            ),
        ),
        secret_records=tuple(
            record.model_copy(update={"status": "disabled", "updated_at": now, "updated_by": actor})
            for record in records
        ),
        secret_bindings=tuple(
            binding.model_copy(update={"status": "disabled", "updated_at": now})
            for items in binding_sets
            for binding in items
        ),
        secret_copy_sources=(SecretCopySourceExpectation(record=key_record, binding=key_binding),)
        + tuple(
            SecretCopySourceExpectation(record=record, binding=binding)
            for record, items in zip(records, binding_sets, strict=True)
            for binding in items
        ),
        secret_binding_sets=tuple(
            SecretBindingSetExpectation(secret_id=record.secret_id, bindings=items)
            for record, items in zip(records, binding_sets, strict=True)
        ),
        secret_audit_events=tuple(
            SecretAuditEvent(
                event_id=f"service-token-retirement-{trace_id}-{index}",
                secret_id=record.secret_id,
                event_type="disabled",
                recorded_at=now,
                actor=actor,
                detail=request.reason,
                metadata={
                    "plan_digest": digest,
                    "delivery_app_id": app_id,
                    "advisory_app_id": advisory_id,
                    "advisory_check_url": request.advisory_check_url,
                    "delivery_comment_url": request.delivery_comment_url,
                    "delivery_release_issue_url": request.delivery_release_issue_url,
                    "consumer_check_evidence": request.consumer_check_evidence,
                    "director_confirmed": "true",
                },
            )
            for index, record in enumerate(records)
        ),
    )
    return ServiceTokenRetirementResponse(
        trace_id=trace_id,
        mode=request.mode,
        plan_digest=digest,
        tokens=tuple(
            _token_summary(record, items)
            for record, items in zip(records, binding_sets, strict=True)
        ),
        app_id=app_id,
        advisory_app_id=advisory_id,
        actor=actor,
        reason=request.reason,
    ), bundle


def apply_service_token_retirement(
    *,
    store: PostgresRecordStore,
    request: ServiceTokenRetirementRequest,
    actor: str,
    trace_id: str,
    idempotency_key: str,
) -> ServiceTokenRetirementResponse:
    scope = f"service-token-retirement:{actor}"
    fingerprint = hashlib.sha256(
        json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    if request.mode == "apply":
        if not idempotency_key.strip():
            raise ValueError("Service-token retirement requires Idempotency-Key.")
        previous = store.read_idempotency_record(
            scope=scope,
            route_path=SERVICE_TOKEN_RETIREMENT_ROUTE,
            idempotency_key=idempotency_key,
        )
        if previous is not None:
            if previous.state != "completed" or previous.request_fingerprint != fingerprint:
                raise ValueError("Service-token retirement idempotency conflict.")
            return ServiceTokenRetirementResponse.model_validate(previous.response_payload)
    result, bundle = plan_service_token_retirement(
        store=store, request=request, actor=actor, trace_id=trace_id
    )
    if request.mode == "dry-run":
        return result
    if request.expected_plan_digest != result.plan_digest:
        raise ValueError("Service-token retirement changed since the reviewed dry-run.")
    receipt = LaunchplaneIdempotencyRecord(
        record_id=f"idempotency-{trace_id}",
        scope=scope,
        route_path=SERVICE_TOKEN_RETIREMENT_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        response_status_code=200,
        response_trace_id=trace_id,
        recorded_at=datetime.now(UTC).isoformat(),
        response_payload=result.model_dump(mode="json"),
    )
    store.write_product_authority_bundle(bundle.model_copy(update={"idempotency_record": receipt}))
    return result
