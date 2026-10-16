"""Service custody: one human-reviewed encrypted delivery, never a secret read API."""

import base64
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.private_health_endpoint_record import (
    PrivateHealthEndpointRecord,
    private_health_endpoint_record_sha256,
)
from control_plane.contracts.secret_record import SecretVersion
from control_plane.live_target_runtime import evaluate_runtime_key_safety_for_live_target_sync
from control_plane.local_consumer_contract import (
    BRIDGE_BINDING,
    DELIVERY_ROUTE,
    DELIVERY_SCOPE,
    DELIVERY_LIFETIME_SECONDS,
    ConsumerPackage,
    ConsumerPlan,
    ConsumerRequest,
    canonical,
    digest,
    https_origin,
)
from control_plane.product_secret_copy import (
    copy_source_value,
    product_secret_bindings,
    require_product_lanes,
)
from control_plane.storage.postgres import PostgresRecordStore


class ConsumerCredentialUnavailable(ValueError):
    """Destination is valid, but no isolated deliverable credential exists yet."""


def validate_destination(store: PostgresRecordStore, request: ConsumerRequest) -> tuple[str, str]:
    profile = require_product_lanes(
        store, product=request.product, routes=((request.context, request.environment),)
    )
    if not any(
        item.binding_key == BRIDGE_BINDING
        and item.integration == "runtime_environment"
        and item.context == request.context
        and item.instance == request.environment
        for item in profile.expected_config.managed_secret_bindings
    ):
        raise ValueError("Declare the exact testing bridge field before local delivery.")
    endpoint = store.read_private_health_endpoint_record(request.endpoint_key)
    _require_endpoint(endpoint, request)
    return digest(profile), private_health_endpoint_record_sha256(endpoint)


def plan_delivery(
    store: PostgresRecordStore, request: ConsumerRequest, *, expires_at: int | None = None
) -> ConsumerPlan:
    profile_digest, endpoint_digest = validate_destination(store, request)
    candidates = [
        (binding, record)
        for binding, record in product_secret_bindings(store, product=request.product)
        if binding.binding_key == BRIDGE_BINDING
        and record.context == request.context
        and record.instance == request.environment
        and record.scope == "context_instance"
        and record.status == "configured"
        and binding.status == "configured"
        and binding.context == request.context
        and binding.instance == request.environment
        and binding.declared_secret_class == "testing"
        and binding.sharing_reason is None
    ]
    if len(candidates) != 1:
        raise ConsumerCredentialUnavailable("One isolated testing bridge credential is required.")
    binding, record = candidates[0]
    if any(
        other.secret_id == record.secret_id and other.binding_id != binding.binding_id
        for other in store.list_secret_bindings(limit=None)
    ):
        raise ValueError("Shared credentials cannot be delivered to a local testing client.")
    evaluate_runtime_key_safety_for_live_target_sync(
        record_store=store,
        context_name=request.context,
        instance_name=request.environment,
        required_binding_keys=(BRIDGE_BINDING,),
    )
    return ConsumerPlan(
        request_fingerprint=digest(request),
        secret_id=record.secret_id,
        version_id=record.current_version_id,
        profile_digest=profile_digest,
        endpoint_digest=endpoint_digest,
        expires_at=expires_at
        if expires_at is not None
        else int(time.time()) + DELIVERY_LIFETIME_SECONDS,
    )


def _require_endpoint(endpoint: PrivateHealthEndpointRecord, request: ConsumerRequest) -> None:
    server = urlsplit(request.server_url)
    health = urlsplit(endpoint.url)
    if (
        (endpoint.product, endpoint.context, endpoint.instance, endpoint.status)
        != (request.product, request.context, request.environment, "active")
        or health.scheme != "https"
        or https_origin(f"https://{health.netloc}") != https_origin(f"https://{server.netloc}")
        or health.path != "/health"
        or health.username
        or health.password
        or health.query
        or health.fragment
    ):
        raise ValueError("The bridge must match the exact active testing HTTPS health endpoint.")


def _record(
    store: PostgresRecordStore, request: ConsumerRequest, *, planning: bool = False
) -> LaunchplaneIdempotencyRecord | None:
    return store.read_idempotency_record(
        scope=DELIVERY_SCOPE,
        route_path=DELIVERY_ROUTE + ("/plan" if planning else ""),
        idempotency_key=request.request_id,
    )


def _save_once(
    store: PostgresRecordStore,
    request: ConsumerRequest,
    *,
    actor: str,
    trace_id: str,
    payload: dict[str, object],
    planning: bool = False,
) -> LaunchplaneIdempotencyRecord:
    route = DELIVERY_ROUTE + ("/plan" if planning else "")
    fingerprint = digest({"request": request.model_dump(mode="json"), "actor": actor})
    now = datetime.now(timezone.utc).isoformat()
    record = LaunchplaneIdempotencyRecord(
        record_id=build_launchplane_mutation_reservation_id(
            scope=DELIVERY_SCOPE, route_path=route, idempotency_key=request.request_id
        ),
        scope=DELIVERY_SCOPE,
        route_path=route,
        idempotency_key=request.request_id,
        request_fingerprint=fingerprint,
        response_status_code=200,
        response_trace_id=trace_id,
        recorded_at=now,
        response_payload=payload,
    )
    store.write_idempotency_record_once(record)
    saved = _record(store, request, planning=planning)
    if saved is None or saved.request_fingerprint != fingerprint:
        raise ValueError("The original delivery belongs to a different request or administrator.")
    return saved


def prepare_delivery(
    store: PostgresRecordStore, request: ConsumerRequest, *, actor: str, trace_id: str
) -> ConsumerPlan:
    existing = _record(store, request, planning=True)
    deadline = (
        ConsumerPlan.model_validate(existing.response_payload).expires_at if existing else None
    )
    plan = plan_delivery(store, request, expires_at=deadline)
    plan.require_fresh()
    saved = _save_once(
        store,
        request,
        actor=actor,
        trace_id=trace_id,
        payload=plan.model_dump(mode="json"),
        planning=True,
    )
    if saved.response_payload != plan.model_dump(mode="json"):
        raise ValueError("The source changed; prepare and review a new testing destination.")
    return plan


def issue_delivery(
    store: PostgresRecordStore,
    request: ConsumerRequest,
    *,
    actor: str,
    trace_id: str,
    reviewed_plan_digest: str,
) -> ConsumerPackage:
    saved_plan = _record(store, request, planning=True)
    if saved_plan is None:
        raise ValueError("A matching original dry run is required.")
    stored_plan = ConsumerPlan.model_validate(saved_plan.response_payload)
    stored_plan.require_fresh()
    plan = plan_delivery(store, request, expires_at=stored_plan.expires_at)
    fingerprint = digest({"request": request.model_dump(mode="json"), "actor": actor})
    if (
        saved_plan.request_fingerprint != fingerprint
        or saved_plan.response_payload != plan.model_dump(mode="json")
        or reviewed_plan_digest != digest(plan)
    ):
        raise ValueError("A matching original dry run and reviewed digest are required.")
    existing = _record(store, request)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ValueError("The original delivery request changed.")
        package = ConsumerPackage.model_validate(existing.response_payload)
        verify_delivery(store, package)
        return package
    version = store.read_secret_version(plan.version_id)
    if version.secret_id != plan.secret_id:
        raise ValueError("The bridge version no longer belongs to this credential.")
    package = _encrypt(request, plan, version, actor=actor)
    # Only encrypted bytes are persisted. A race with rotation/profile changes is
    # rejected by online verification before any client can decrypt/install them.
    saved = _save_once(
        store,
        request,
        actor=actor,
        trace_id=trace_id,
        payload=package.model_dump(mode="json"),
    )
    package = ConsumerPackage.model_validate(saved.response_payload)
    verify_delivery(store, package)
    return package


def _encrypt(
    request: ConsumerRequest, plan: ConsumerPlan, version: SecretVersion, *, actor: str
) -> ConsumerPackage:
    public_key = serialization.load_pem_public_key(request.public_key.encode())
    assert isinstance(public_key, rsa.RSAPublicKey)
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    value = copy_source_value(version)
    if not value or len(value.encode()) > 8192 or "\r" in value or "\n" in value:
        raise ValueError("The bridge credential has an unsupported format.")
    ciphertext = AESGCM(key).encrypt(nonce, value.encode(), canonical(plan))
    wrapped = public_key.encrypt(
        key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return ConsumerPackage(
        request=request,
        plan=plan,
        issued_by=actor,
        wrapped_key=base64.b64encode(wrapped).decode(),
        nonce=base64.b64encode(nonce).decode(),
        ciphertext=base64.b64encode(ciphertext).decode(),
    )


def verify_delivery(store: PostgresRecordStore, package: ConsumerPackage) -> str:
    """Possession checks only ciphertext metadata; never resolve a value here."""
    package.plan.require_fresh()
    saved = _record(store, package.request)
    if (
        saved is None
        or digest(saved.response_payload) != digest(package)
        or plan_delivery(store, package.request, expires_at=package.plan.expires_at) != package.plan
    ):
        raise ValueError("The original delivery is unavailable, revoked or changed.")
    return digest(package)
