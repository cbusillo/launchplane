"""Activate only new testing resources; retain the old recipient and unknown effects."""

import base64
from collections.abc import Callable
from dataclasses import dataclass
import hmac
import json
from pathlib import Path
import re
from typing import Any, Literal, cast

import yaml

from control_plane import runtime_environments
from control_plane.build_provenance import GitHubBuildProvenanceTransport
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.testing_activation import (
    TestingActivationPlan,
    TestingActivationRefusal,
    TestingActivationRefusalCode,
    TestingActivationRecord,
    TestingActivationRequest,
    TestingActivationResult,
)
from control_plane.disposable_lanes import disposable_lane_configuration
from control_plane.disposable_lanes import LaneConfigurationRecord
from control_plane.contracts.secret_record import SecretRecord, SecretBinding
from control_plane.dokploy import api, compose, post_deploy, source
from control_plane.dokploy.network_capacity import (
    BridgeNetworkCapacityRefused,
    compose_bridge_network_requirement,
    require_bridge_network_capacity,
)
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.generic_web_testing_exclusion import (
    _bind,
    bind_testing_recipient,
    _target_reservations,
    inspect_generic_web_testing_exclusion,
)
from control_plane.github_app_identity import (
    mint_source_control_read_installation_token,
)
from control_plane.integration_allowances import target_record_sha256
from control_plane.live_target_runtime import (
    LiveTargetRuntimeError,
    evaluate_runtime_key_safety_for_live_target_sync,
)
from control_plane.odoo_testing_resources import (
    GenericWebTestingResources,
    ApplicationTestingResources,
    OdooTestingResources,
    resource_readback_passes,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.runtime_platform_credentials import looks_like_credential
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.testing_activation_compose import (
    interpolate,
    qualify_compose,
    read_compose_source,
)
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.testing_separation_resources import (
    ROUTE as PREPARATION_ROUTE,
    SeparationResourcesResult,
    preparation_binding,
)
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = "/v1/products/{product}/testing-activation"


def activation_adapter(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: TestingActivationRequest,
    actor: str,
    scope: str,
    trace_id: str,
) -> "TestingActivationAdapter":
    if request.application is not None:
        from control_plane.testing_application_activation import ApplicationActivationAdapter

        return ApplicationActivationAdapter(root, store, product, request, actor, scope, trace_id)
    return TestingActivationAdapter(root, store, product, request, actor, scope, trace_id)


def read_product_compose(store: PostgresRecordStore, product: str, commit: str, path: str) -> str:
    from control_plane.product_reconcile import _merge_train_app_identity

    profile = store.read_product_profile_record(product)
    identity, repository_id = _merge_train_app_identity(
        store, profile, purpose="testing-compose-source"
    )
    token = mint_source_control_read_installation_token(
        identity=identity,
        repository=profile.repository,
        repository_id=repository_id,
    )
    return read_compose_source(
        repository=profile.repository,
        commit=commit,
        path=path,
        read=GitHubBuildProvenanceTransport(token=token.token).get_json,
    )


def verified_product_image(store: PostgresRecordStore, product: str, commit: str) -> str:
    from control_plane.build_provenance import verify_generic_web_build
    from control_plane.product_reconcile import (
        product_repository_identity,
        resolve_build_provenance_transport,
    )

    profile = store.read_product_profile_record(product)
    identity = product_repository_identity(store, profile)
    return verify_generic_web_build(
        transport=resolve_build_provenance_transport(store, profile),
        repository=profile.repository,
        repository_id=identity.repository_id,
        commit=commit,
        purpose="release",
        image_repository=profile.image.repository,
    ).image_reference


def activation_record(
    store: PostgresRecordStore,
    product: str,
    context: str,
    *,
    scope: str = "",
    preparation_key: str = "",
    plan_sha256: str = "",
) -> TestingActivationRecord | None:
    records = [
        TestingActivationRecord.model_validate(payload)
        for payload in store.list_testing_application_activations(product, context)
    ]
    if scope or preparation_key:
        matches = [
            row
            for row in records
            if (row.scope, row.request.preparation_key) == (scope, preparation_key)
        ]
    elif plan_sha256:
        matches = [row for row in records if row.plan_sha256 == plan_sha256]
    elif records:
        target = store.read_dokploy_target_id_record(context_name=context, instance_name="testing")
        matches = [row for row in records if row.published and row.target_id == target.target_id]
    else:
        matches = []
    if len(matches) > 1:
        raise TestingActivationRefusal("Activation operation custody is ambiguous.")
    if matches:
        return matches[0]
    if scope or preparation_key:
        return None
    payload = store.read_testing_activation(product, context)
    legacy = TestingActivationRecord.model_validate(payload) if payload is not None else None
    return (
        legacy
        if legacy is not None and (not plan_sha256 or legacy.plan_sha256 == plan_sha256)
        else None
    )


def configuration_digest(
    store: PostgresRecordStore,
    product: str,
    context: str,
    *,
    version: Literal[1, 2, 3] = 3,
    generated: ProductAuthorityBundle | None = None,
) -> str:
    profile = store.read_product_profile_record(product)
    lane = next(
        item for item in profile.lanes if (item.context, item.instance) == (context, "testing")
    )
    configuration: list[LaneConfigurationRecord] = list(
        disposable_lane_configuration(store, context, "testing")
    )
    if generated is not None:
        replaced_records = {record.secret_id for record in generated.secret_records}
        replaced_bindings = {binding.binding_id for binding in generated.secret_bindings}
        configuration = [
            row
            for row in configuration
            if not (
                isinstance(row, SecretRecord)
                and row.secret_id in replaced_records
                or isinstance(row, SecretBinding)
                and row.binding_id in replaced_bindings
            )
        ]
        configuration.extend(
            row for row in generated.secret_records if row.integration == "runtime_environment"
        )
        configuration.extend(
            row for row in generated.secret_bindings if row.integration == "runtime_environment"
        )
    if version == 3:
        configuration.sort(key=lambda row: row.model_dump_json())
    keys = {item.binding_key for item in configuration if hasattr(item, "binding_key")}
    return canonical_json_sha256(
        {
            "profile": {
                "product": profile.product,
                "repository": profile.repository,
                "driver_id": profile.driver_id,
                **({"public_website": profile.public_website} if version >= 2 else {}),
                **(
                    {"expected_config": profile.expected_config.model_dump(mode="json")}
                    if version == 3
                    else {}
                ),
                "is_active": profile.is_active,
                "image": profile.image.model_dump(mode="json"),
                "lane": lane.model_dump(mode="json"),
            },
            "configuration": [
                row.model_dump(mode="json")
                for row in configuration
                if not hasattr(row, "target_name")
            ],
            "key_policy": [
                {
                    "rules": [
                        rule.model_dump(mode="json", exclude={"description"})
                        for rule in row.rules
                        if rule.binding_key in keys
                    ],
                    "integration_key_markers": row.integration_key_markers,
                }
                for row in store.list_runtime_key_safety_policy_records(status="active", limit=1)
            ],
        }
    )


def preparation_resources(
    store: PostgresRecordStore, product: str, request: TestingActivationRequest, scope: str
) -> tuple[OdooTestingResources, str]:
    row = store.read_idempotency_record(
        scope=scope,
        route_path=PREPARATION_ROUTE.format(product=product),
        idempotency_key=request.preparation_key,
    )
    if row is None or row.state != "completed":
        raise TestingActivationRefusal("Use the original caller's completed allocation key.")
    receipt = SeparationResourcesResult.model_validate(row.response_payload.get("result"))
    if (receipt.product, receipt.context) != (product, request.context):
        raise TestingActivationRefusal("Allocation belongs to another testing lane.")
    old_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name="testing"
    )
    if receipt.application != request.application:
        raise TestingActivationRefusal(
            "Activation must use the original reviewed resource specification."
        )
    existing = activation_record(
        store,
        product,
        request.context,
        **(
            {"scope": scope, "preparation_key": request.preparation_key}
            if request.application is not None
            else {}
        ),
    )
    values = dict(
        product=product,
        context=request.context,
        operation_id=receipt.plan_sha256,
        target_id=receipt.retained_target_id
        or (existing.retained_target_id.target_id if existing else old_id.target_id),
        namespace=f"lp-testing-{receipt.plan_sha256[:32]}",
    )
    resources = (
        ApplicationTestingResources.model_validate(
            {**values, "state_kind": request.application.state.kind}
        )
        if request.application is not None
        else GenericWebTestingResources.model_validate(values)
    )
    if (
        receipt.volumes != resources.volumes
        or receipt.resources_binding_sha256 != resources.binding_sha256
    ):
        raise TestingActivationRefusal(
            "Allocation provenance does not agree with its original placement."
        )
    return resources, row.record_id


def runtime_values(
    store: PostgresRecordStore,
    root: Path,
    record: TestingActivationRecord,
    resources: OdooTestingResources,
) -> dict[str, str]:
    environment = runtime_environments.resolve_site_runtime_environment(
        control_plane_root=root,
        context_name=record.context,
        instance_name="testing",
        database_url=store.database_url,
        target_environment_override={},
    )
    values = dict(environment.values)
    if set(record.request.volume_keys.values()) & environment.retired_keys:
        raise TestingActivationRefusal(
            "Choose non-retired lane settings for the allocated volumes."
        )
    if (
        set((*record.request.volume_keys.values(), *record.request.port_keys))
        & environment.secret_keys
    ):
        raise TestingActivationRefusal("A resource setting cannot be secret-backed.")
    for role, key in record.request.volume_keys.items():
        values[key] = resources.volumes[role]
    try:
        safety = evaluate_runtime_key_safety_for_live_target_sync(
            record_store=store,
            context_name=record.context,
            instance_name="testing",
            required_binding_keys=tuple(sorted(environment.secret_keys)),
        )
    except LiveTargetRuntimeError as error:
        raise TestingActivationRefusal(
            "Carried-key safety remains unproved.",
            code=TestingActivationRefusalCode.CARRIED_KEYS_UNVERIFIED,
        ) from error
    if safety.get("status") not in {"pass", "skipped"}:
        raise TestingActivationRefusal(
            "Carried-key safety remains unproved.",
            code=TestingActivationRefusalCode.CARRIED_KEYS_UNVERIFIED,
        )
    bindings = runtime_environments.site_delivered_secret_bindings_from_store(
        record_store=store, context_name=record.context, instance_name="testing"
    )
    shared: set[str] = set()
    for key in environment.secret_keys:
        binding = bindings.get(key)
        if binding is None:
            raise TestingActivationRefusal("A carried secret has no managed binding.")
        secret = store.read_secret_record(binding.secret_id)
        if (secret.scope, secret.context, secret.instance) != (
            "context_instance",
            record.context,
            "testing",
        ):
            raise TestingActivationRefusal("Testing credentials must have lane-specific bindings.")
        if (
            binding.declared_secret_class == "shared_safe"
            and binding.sharing_reason
            and binding.sharing_reason.kind == "read_only_source"
        ):
            shared.add(key)
        elif binding.declared_secret_class not in {"testing", "non_prod"}:
            raise TestingActivationRefusal("A carried key can reach a live writable system.")
    if any(
        looks_like_credential(key, value) and key not in environment.secret_keys
        for key, value in values.items()
    ):
        raise TestingActivationRefusal("Application credentials require safe managed bindings.")
    # Existing production env is read only inside the service. Names and classes
    # alone cannot prove that two credentials have different values.
    host, token = source.read_dokploy_config(control_plane_root=root)
    profile = store.read_product_profile_record(record.product)
    for lane in profile.lanes:
        if lane.instance != "prod":
            continue
        provider = store.read_provider_target_record(
            context_name=lane.context, instance_name="prod"
        )
        payload = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=provider.provider_target_type,
            target_id=provider.target_id,
        )
        production = api.parse_dokploy_env_text(str(payload.get("env") or ""))
        if not production:
            raise TestingActivationRefusal(
                "Opaque production credential comparison is unavailable."
            )
        for key in environment.secret_keys - shared:
            if any(
                value and hmac.compare_digest(values[key].encode(), value.encode())
                for value in production.values()
            ):
                raise TestingActivationRefusal("A testing credential overlaps production.")
    values["DOCKER_IMAGE_REFERENCE"] = record.image_reference
    return values


def ports_for(record: TestingActivationRecord, values: dict[str, str]) -> set[int]:
    raw = [values.get(key, "") for key in record.request.port_keys]
    if (
        not raw
        or any(not value.isdecimal() or not 1024 <= int(value) <= 65535 for value in raw)
        or len(set(raw)) != len(raw)
    ):
        raise TestingActivationRefusal(
            "Independent testing ports require distinct existing lane settings."
        )
    return {int(value) for value in raw}


def rendered_source(
    record: TestingActivationRecord, values: dict[str, str], resources: OdooTestingResources
) -> dict[str, Any]:
    rendered = interpolate(json.loads(record.compose_source), values)
    credential_values = {
        value for key, value in values.items() if looks_like_credential(key, value)
    }
    qualify_compose(
        rendered,
        volumes=set(resources.volumes.values()),
        ports=ports_for(record, values),
        credential_values=credential_values,
    )
    if not any(
        service.get("image") == record.image_reference for service in rendered["services"].values()
    ):
        raise TestingActivationRefusal(
            "The reviewed source must consume the verified product image."
        )
    return cast(dict[str, Any], rendered)


def target_without_hold(target: Any) -> dict[str, Any]:
    payload = target.model_dump(mode="json", exclude={"updated_at", "source_label"})
    payload["policies"].pop("staff_testing_hold", None)
    return cast(dict[str, Any], payload)


def same_reservation_intent(current: object, original: object) -> bool:
    return current is not None and all(
        getattr(current, key, None) == getattr(original, key)
        for key in (
            "record_id",
            "scope",
            "route_path",
            "idempotency_key",
            "request_fingerprint",
            "provider_target_key",
            "reconciliation_key",
        )
    )


def retained_fence_is_separated(store: object, reservation: object) -> bool:
    """Only exact retained fences of a published qualified recipient change lane attribution."""
    from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
    from control_plane.workflows.generic_web_deploy_provider import (
        decode_generic_web_provider_reconciliation_target,
    )

    if not isinstance(store, PostgresRecordStore) or not isinstance(
        reservation, LaunchplaneIdempotencyRecord
    ):
        return False
    try:
        snapshot = decode_generic_web_provider_reconciliation_target(reservation.reconciliation_key)
        if snapshot.instance != "testing" or not snapshot.product:
            return False
        record = activation_record(store, snapshot.product, snapshot.context)
        if record is not None and record.request.application is not None:
            _profile, current, target_id, target = bind_testing_recipient(
                store, record.product, record.context, "testing"
            )
            seen: set[str] = set()
            while record.target_id not in seen:
                seen.add(record.target_id)
                if not completed_application_activation(store, record):
                    return False
                if any(
                    same_reservation_intent(reservation, original)
                    for original in record.retained_reservations
                ):
                    return (
                        target.target_id == target_id.target_id
                        and current.target_type == target.provider_target_type == "application"
                        and target_id.target_id != snapshot.target_id
                    )
                candidates = [
                    TestingActivationRecord.model_validate(value)
                    for value in store.list_testing_application_activations(
                        record.product, record.context
                    )
                ]
                predecessors = [
                    value
                    for value in candidates
                    if value.published and value.target_id == record.retained_target_id.target_id
                ]
                if len(predecessors) != 1:
                    return False
                record = predecessors[0]
            return False
        if (
            record is None
            or not record.published
            or not any(
                same_reservation_intent(reservation, original)
                for original in record.retained_reservations
            )
        ):
            return False
        _profile, current, target_id, target = _bind(
            store, record.product, record.context, "testing"
        )
        return (
            target.target_id == record.target_id
            and target_id.target_id == record.target_id
            and target.provider_target_type == "compose"
            and target.target_id != record.retained_target_id.target_id
            and record.published_target is not None
            and current.target_type == "compose"
            and all(
                current.env.get(key) == record.published_target.env.get(key)
                for key in (*record.request.volume_keys.values(), *record.request.port_keys)
            )
        )
    except (ValueError, FileNotFoundError):
        return False


def completed_application_activation(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> bool:
    """Only the real original parent's completed publication permits continuation."""
    if (
        record.request.application is None
        or not record.published
        or record.published_target is None
    ):
        return False
    parent = store.read_testing_application_activation_operation(
        record.product, record.context, record.scope, record.request.preparation_key
    )
    if (
        parent is None
        or parent.state != "completed"
        or parent.reconciliation_key != record.plan_sha256
    ):
        return False
    result = parent.response_payload.get("result")
    return (
        isinstance(result, dict)
        and result.get("plan_sha256") == record.plan_sha256
        and result.get("separation_verified") is True
    )


def completed_application_predecessor(
    store: object,
    *,
    product: str,
    context: str,
    target_id: str,
    inventory: object,
    deployment: object,
) -> bool:
    """An exact captured predecessor can be left behind once, without adopting its originals."""
    if not isinstance(store, PostgresRecordStore):
        return False
    record = activation_record(store, product, context)
    return (
        record is not None
        and record.target_id == target_id
        and record.retained_inventory == inventory
        and record.retained_deployment == deployment
        and completed_application_activation(store, record)
    )


def require_activation_hold_lift(
    store: object, request: object, actor: str, root: Path, *, inspect_provider: bool = True
) -> bool:
    """Explicit same-caller lift of the unchanged interlock after fresh provider proof."""
    from control_plane.testing_lane_hold import TestingHoldApplyRequest

    if (
        not isinstance(store, PostgresRecordStore)
        or not isinstance(request, TestingHoldApplyRequest)
        or not request.activation_plan_sha256
        or request.instance != "testing"
    ):
        raise TestingActivationRefusal(
            "Activation continuation requires service-owned testing records."
        )
    record = activation_record(
        store, request.product, request.context, plan_sha256=request.activation_plan_sha256
    )
    if (
        record is None
        or not record.published
        or record.actor != actor
        or record.plan_sha256 != request.activation_plan_sha256
        or request.hold
    ):
        raise TestingActivationRefusal(
            "The activation does not authorize this interlock continuation."
        )
    adapter = activation_adapter(
        root,
        store,
        record.product,
        record.request,
        record.actor,
        record.scope,
        "activation_hold_preflight",
    )
    adapter.require_unchanged(record)
    protection = store.read_disposable_lane_safety(record.product, record.context, "testing")
    if protection and protection.usage == "in_use":
        raise TestingActivationRefusal("Known staff use protects the testing recipient.")
    target = store.read_dokploy_target_record(context_name=record.context, instance_name="testing")
    if target.policies.staff_testing_hold not in (
        None,
        record.retained_target.policies.staff_testing_hold,
    ):
        raise TestingActivationRefusal("A later genuine staff hold remains protected.")
    if not inspect_provider or target.policies.staff_testing_hold is None:
        # A lost post-clear receipt must still reach the writer's no-op/reconcile
        # recovery; the now-running recipient need not remain empty.
        return True
    adapter.verify_provider(record)
    resources, _ = preparation_resources(store, record.product, record.request, record.scope)
    if record.request.application is None:
        inspect_new_resources(
            root,
            resources,
            ports_for(record, runtime_values(store, root, record, resources)),
            lambda _phase: None,
        )
    return True


def build_activation(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: TestingActivationRequest,
    actor: str,
    scope: str,
) -> TestingActivationRecord:
    existing = activation_record(store, product, request.context)
    if existing:
        if (
            existing.actor,
            existing.scope,
            existing.request.model_dump(exclude={"mode", "reviewed_plan_sha256"}),
        ) != (actor, scope, request.model_dump(exclude={"mode", "reviewed_plan_sha256"})):
            raise TestingActivationRefusal("Retain the original activation request and caller.")
        return existing
    profile, target, target_id, provider = _bind(store, product, request.context, "testing")
    if not profile.is_active or target_record_sha256(target) != request.interlock_record_sha256:
        raise TestingActivationRefusal(
            "Explicit staging-interlock identification no longer matches."
        )
    if (
        target.public_hosts
        or target.pending_public_host_effects
        or (target.domains and profile.public_website != "none")
    ):
        raise TestingActivationRefusal(
            "Existing routes require their own accepted cutover.",
            code=TestingActivationRefusalCode.ROUTES_REQUIRE_CUTOVER,
        )
    hold = target.policies.staff_testing_hold
    if hold is None or hold.recorded_by != actor:
        raise TestingActivationRefusal(
            "An absent, foreign or unacknowledged staff hold is protected."
        )
    protection = store.read_disposable_lane_safety(product, request.context, "testing")
    if protection and protection.usage == "in_use":
        raise TestingActivationRefusal("Known staff use protects the old testing recipient.")
    resources, receipt_id = preparation_resources(store, product, request, scope)
    binding, old_key = preparation_binding(store, product, request.context, actor, root)
    if (
        resources.operation_id != binding
        or set(request.volume_keys) != set(resources.volumes)
        or len(set(request.volume_keys.values())) != len(resources.volumes)
    ):
        raise TestingActivationRefusal("Original allocation or its role mapping changed.")
    if any(
        not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
        for key in (*request.volume_keys.values(), *request.port_keys)
    ) or set(request.volume_keys.values()).intersection(request.port_keys):
        raise TestingActivationRefusal("Resource settings require distinct explicit key names.")
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id.target_id
    )
    path = target.compose_path or str(payload.get("composePath") or "")
    document = read_product_compose(store, product, request.source_commit, path)
    image_reference = verified_product_image(store, product, request.source_commit)
    digest = configuration_digest(store, product, request.context)
    record = TestingActivationRecord(
        product=product,
        context=request.context,
        actor=actor,
        scope=scope,
        request=request.model_copy(update={"mode": "dry-run", "reviewed_plan_sha256": ""}),
        plan_sha256="",
        namespace=resources.namespace + "-recipient",
        preparation_record_id=receipt_id,
        retained_target=target,
        retained_target_id=target_id,
        retained_provider=provider,
        retained_reservations=_target_reservations(
            store,
            product=product,
            context=request.context,
            target_key=old_key,
            target_id=target_id.target_id,
        ),
        configuration_sha256=digest,
        configuration_version=3,
        compose_source=document,
        image_reference=image_reference,
    )
    values = runtime_values(store, root, record, resources)
    rendered_source(record, values, resources)
    record.host_ports = tuple(sorted(ports_for(record, values)))
    record.plan_sha256 = canonical_json_sha256(record.model_dump(exclude={"plan_sha256"}))
    return record


def plan(record: TestingActivationRecord) -> dict[str, Any]:
    return TestingActivationPlan(
        product=record.product,
        context=record.context,
        plan_sha256=record.plan_sha256,
        source_commit=record.request.source_commit,
        resource_namespace=record.namespace,
        image_reference=record.image_reference,
        host_ports=record.host_ports,
    ).model_dump(mode="json")


def inspect_new_resources(
    root: Path,
    resources: OdooTestingResources,
    ports: set[int],
    before_effect: Callable[[str], None],
) -> None:
    """Fixed host program: read every mount, requalify empty storage, bind only candidate ports."""
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=resources.target_id
    )
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=resources.target_id,
        compose_name=resources.context,
        target_payload=payload,
    )
    encoded = base64.b64encode(json.dumps(sorted(ports)).encode()).decode()
    program = (
        resources.script("readback")
        + "\npython3 - "
        + encoded
        + " <<'LP_PORTS'\n"
        + PORT_INSPECTION_PROGRAM
        + "\nLP_PORTS\n"
    )
    evidence = post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=resources.context, instance="testing", target_id=resources.target_id
        ),
        schedule_lookup_id=lookup,
        schedule_type=kind,
        schedule_name="Launchplane testing activation inspection",
        schedule_app_name=resources.namespace + "-activation",
        schedule_payload={
            "name": "Launchplane testing activation inspection",
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "appName": resources.namespace + "-activation",
            "shellType": "bash",
            "scheduleType": kind,
            "command": "control-plane testing activation inspection",
            "script": program,
            "serverId": server,
            "userId": lookup if kind == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=before_effect,
    )
    if (
        not resource_readback_passes(evidence, resources)
        or evidence.get("testing_activation_ports_ok") != "true"
    ):
        raise TestingActivationRefusal("Actual empty storage or independent ports are unproved.")


PORT_INSPECTION_PROGRAM = r"""
import base64, json, socket, subprocess, sys
sockets = []
try:
    ports = json.loads(base64.b64decode(sys.argv[1], validate=True))
    def docker(*args):
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout
    for identifier in docker("ps", "-aq").splitlines():
        item = json.loads(docker("inspect", identifier))[0]
        bindings = item.get("HostConfig", {}).get("PortBindings")
        if bindings is not None and not isinstance(bindings, dict):
            raise ValueError()
        for entries in (bindings or {}).values():
            for entry in entries or []:
                if int(entry.get("HostPort", "0")) in ports:
                    raise ValueError()
    for port in ports:
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append(listener)
        listener.bind(("0.0.0.0", port))
    for listener in sockets:
        listener.close()
    print("testing_activation_ports_ok=true")
except Exception:
    print("testing_activation_ports_ok=false")
    sys.exit(1)
finally:
    for listener in sockets:
        listener.close()
"""


@dataclass
class TestingActivationAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: TestingActivationRequest
    actor: str
    scope: str
    trace_id: str

    def target_key(self) -> str:
        return f"testing-activation:{self.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def current(self) -> TestingActivationRecord:
        return build_activation(
            self.root, self.store, self.product, self.request, self.actor, self.scope
        )

    def bindings(self) -> tuple[Any, Any, Any, Any]:
        return _bind(self.store, self.product, self.request.context, "testing")

    def require_unchanged(self, record: TestingActivationRecord) -> None:
        if not record.published:
            protection = self.store.read_disposable_lane_safety(
                record.product, record.context, "testing"
            )
            if protection is not None and protection.usage == "in_use":
                raise TestingActivationRefusal("Known staff use protects the retained recipient.")
        if configuration_digest(
            self.store, self.product, record.context, version=record.configuration_version
        ) != (record.generated_configuration_sha256 or record.configuration_sha256):
            raise TestingActivationRefusal(
                "Testing configuration or key policy changed.",
                code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
            )
        _, target, target_id, provider = self.bindings()
        if record.published:
            if (
                target_id.target_id != record.target_id
                or provider.target_id != record.target_id
                or record.published_target is None
                or target_without_hold(target) != target_without_hold(record.published_target)
            ):
                raise TestingActivationRefusal(
                    "Activated testing recipient changed.",
                    code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
                )
        elif (target, target_id, provider) != (
            record.retained_target,
            record.retained_target_id,
            record.retained_provider,
        ):
            raise TestingActivationRefusal(
                "Retained testing authority changed.",
                code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
            )
        for original in record.retained_reservations:
            current = self.store.read_idempotency_record(
                scope=original.scope,
                route_path=original.route_path,
                idempotency_key=original.idempotency_key,
            )
            if not same_reservation_intent(current, original):
                raise TestingActivationRefusal(
                    "The retained original reservation changed.",
                    code=TestingActivationRefusalCode.AUTHORITY_CHANGED,
                )

    def save(self, record: TestingActivationRecord, **updates: Any) -> TestingActivationRecord:
        replacement = record.model_copy(update=updates)
        self.store.write_testing_activation(
            expected=record.model_dump(mode="json"), replacement=replacement.model_dump(mode="json")
        )
        return replacement

    def verify_provider(self, record: TestingActivationRecord) -> None:
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        values = runtime_values(self.store, self.root, record, resources)
        expected = json.loads(record.compose_source)
        for volume in expected["volumes"].values():
            volume["external"] = True
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.target_id
        )
        if (
            payload.get("composeId") != record.target_id
            or payload.get("appName") != record.app_name
        ):
            raise TestingActivationRefusal(
                "Fresh recipient identity changed.",
                code=TestingActivationRefusalCode.RECIPIENT_IDENTITY_UNVERIFIED,
            )
        if (
            payload.get("sourceType") != "raw"
            or payload.get("autoDeploy") is not False
            or payload.get("mounts")
            or payload.get("serviceNetworks")
            or payload.get("randomize")
            or payload.get("isolatedDeployment")
        ):
            raise TestingActivationRefusal(
                "Fresh recipient options remain unverified.",
                code=TestingActivationRefusalCode.RECIPIENT_OPTIONS_UNVERIFIED,
            )
        try:
            configured_source = json.loads(str(payload.get("composeFile") or "null"))
        except ValueError as error:
            raise TestingActivationRefusal(
                "Fresh recipient source remains unverified.",
                code=TestingActivationRefusalCode.RECIPIENT_SOURCE_UNVERIFIED,
            ) from error
        if configured_source != expected:
            raise TestingActivationRefusal(
                "Fresh recipient source differs from the reviewed source.",
                code=TestingActivationRefusalCode.RECIPIENT_SOURCE_UNVERIFIED,
            )
        if api.parse_dokploy_env_text(str(payload.get("env") or "")) != values:
            raise TestingActivationRefusal(
                "Fresh provider environment differs from safe managed inputs.",
                code=TestingActivationRefusalCode.RECIPIENT_ENVIRONMENT_UNVERIFIED,
            )
        try:
            actual = yaml.safe_load(
                compose.fetch_dokploy_converted_compose_file(
                    host=host, token=token, compose_id=record.target_id
                )
            )
        except yaml.YAMLError as error:
            raise TestingActivationRefusal(
                "The actual provider render is malformed.",
                code=TestingActivationRefusalCode.RECIPIENT_RENDER_UNVERIFIED,
            ) from error
        if actual is None:
            raise TestingActivationRefusal(
                "The configured raw source has not been materialized.",
                code=TestingActivationRefusalCode.RECIPIENT_SOURCE_UNMATERIALIZED,
            )
        if isinstance(actual, dict) and isinstance(actual.get("services"), dict):
            for service_name, service in actual["services"].items():
                if not isinstance(service, dict):
                    raise TestingActivationRefusal(
                        "The actual provider service is malformed.",
                        code=TestingActivationRefusalCode.RECIPIENT_RENDER_UNVERIFIED,
                    )
                labels = service.pop("labels", {})
                if isinstance(labels, list):
                    try:
                        labels = dict(item.split("=", 1) for item in labels)
                    except (AttributeError, TypeError, ValueError) as error:
                        raise TestingActivationRefusal(
                            "The provider added unverified labels.",
                            code=TestingActivationRefusalCode.RECIPIENT_LABELS_UNVERIFIED,
                        ) from error
                if (
                    not isinstance(labels, dict)
                    or set(labels) - compose.DOKPLOY_COMPOSE_METADATA_LABELS
                ):
                    raise TestingActivationRefusal(
                        "The provider added an unqualified routing label.",
                        code=TestingActivationRefusalCode.RECIPIENT_LABELS_UNVERIFIED,
                    )
                if labels and (
                    labels.get("dokploy.application.id") != record.target_id
                    or labels.get("dokploy.service") != service_name
                ):
                    raise TestingActivationRefusal(
                        "Provider metadata labels identify another recipient.",
                        code=TestingActivationRefusalCode.RECIPIENT_LABELS_UNVERIFIED,
                    )
        try:
            actual = interpolate(actual, values)
            # Dokploy adds this root declaration even without domains. Ignore only
            # that exact unused declaration; attachments and aliases remain fenced.
            if isinstance(actual, dict) and isinstance(actual.get("services"), dict):
                networks = actual.get("networks")
                provider_network = "dokploy-network"
                if (
                    not payload.get("domains")
                    and isinstance(networks, dict)
                    and networks.get(provider_network) == {"external": True}
                    and provider_network not in expected.get("networks", {})
                    and all(
                        isinstance(service.get("networks", []), (list, dict))
                        and provider_network not in service.get("networks", [])
                        for service in actual["services"].values()
                    )
                ):
                    networks.pop(provider_network)
                    if not networks and "networks" not in expected:
                        actual.pop("networks")
            qualify_compose(
                actual,
                volumes=set(resources.volumes.values()),
                ports=ports_for(record, values),
                credential_values={
                    value for key, value in values.items() if looks_like_credential(key, value)
                },
            )
        except (ValueError, TypeError, AttributeError) as error:
            raise TestingActivationRefusal(
                "The actual provider render has unqualified resources.",
                code=TestingActivationRefusalCode.RECIPIENT_RENDER_UNVERIFIED,
            ) from error
        # Semantic equality prevents a provider conversion from replacing safe env,
        # command or health values while retaining only the storage settings.
        expected_render = interpolate(expected, values)
        if isinstance(actual, dict):
            actual.pop("name", None)
        if actual != expected_render:
            raise TestingActivationRefusal(
                "Actual provider render differs from the reviewed source and values.",
                code=TestingActivationRefusalCode.RECIPIENT_RENDER_CHANGED,
            )

    def materialize_source(
        self, record: TestingActivationRecord, checkpoint: Callable[[str], None]
    ) -> TestingActivationRecord:
        # Dokploy converts the host file, not its configured raw-source record.
        # Each original-key apply may fetch an absent file into this exact
        # unused recipient's code directory. A present render is never rewritten.
        self.unused_recipient(record)
        try:
            self.verify_provider(record)
        except TestingActivationRefusal as error:
            if error.code != TestingActivationRefusalCode.RECIPIENT_SOURCE_UNMATERIALIZED:
                raise
            checkpoint("materialize_new_recipient")
            record = self.save(record, phase="materialize_new_recipient")
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/compose.fetchSourceType",
                method="POST",
                payload={"composeId": record.target_id},
            )
            self.verify_provider(record)
        return record

    def unused_recipient(self, record: TestingActivationRecord) -> dict[str, Any]:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.target_id
        )
        old = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=record.retained_target_id.target_id,
        )
        app_name = payload.get("appName")
        if (
            payload.get("composeId") != record.target_id
            or payload.get("environmentId") != record.environment_id
            or payload.get("serverId") != old.get("serverId")
            or not isinstance(app_name, str)
            or not re.fullmatch(re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?", app_name)
            or payload.get("deployments") != []
            or payload.get("composeStatus") not in {None, "idle"}
            or payload.get("domains")
            or payload.get("mounts")
            or payload.get("serviceNetworks")
        ):
            raise TestingActivationRefusal("The original fresh recipient is not provably unused.")
        return cast(dict[str, Any], payload)

    @staticmethod
    def recipient_configuration_sha256(payload: dict[str, Any]) -> str:
        return canonical_json_sha256(
            {
                key: payload.get(key)
                for key in (
                    "composeId",
                    "environmentId",
                    "serverId",
                    "appName",
                    "sourceType",
                    "composeFile",
                    "composePath",
                    "autoDeploy",
                    "env",
                    "mounts",
                    "domains",
                    "serviceNetworks",
                    "randomize",
                    "isolatedDeployment",
                )
            }
        )

    def recover_placement(self, record: TestingActivationRecord) -> TestingActivationRecord:
        """Adopt only observed IDs in this original namespace; never repeat an unknown create."""
        if record.phase == "prepared":
            return record
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        matches = [
            item
            for item in api.fetch_dokploy_projects(host=host, token=token)
            if item.get("name") == record.namespace
        ]
        if len(matches) != 1:
            raise TestingActivationRefusal(
                "Original placement is absent or ambiguous; retain its original key."
            )
        project = cast(dict[str, Any], matches[0])
        project_id = project.get("projectId")
        if (
            not isinstance(project_id, str)
            or not project_id
            or (record.project_id and record.project_id != project_id)
        ):
            raise TestingActivationRefusal("Original project identity changed.")
        if not record.project_id:
            if record.phase != "create_project":
                raise TestingActivationRefusal("Unrequested project placement.")
            record = self.save(record, project_id=project_id, phase="project_ready")
        environments = project.get("environments")
        if not isinstance(environments, list) or not all(
            isinstance(item, dict) for item in environments
        ):
            raise TestingActivationRefusal("Original environment inventory is incomplete.")
        candidates = [item for item in environments if item.get("name") == record.namespace]
        if not candidates and record.phase == "project_ready":
            return record
        if len(candidates) != 1:
            raise TestingActivationRefusal("Original environment is absent or ambiguous.")
        environment = candidates[0]
        environment_id = environment.get("environmentId")
        if (
            not isinstance(environment_id, str)
            or not environment_id
            or (record.environment_id and record.environment_id != environment_id)
        ):
            raise TestingActivationRefusal("Original environment identity changed.")
        if not record.environment_id:
            if record.phase != "create_environment":
                raise TestingActivationRefusal("Unrequested environment placement.")
            record = self.save(record, environment_id=environment_id, phase="environment_ready")
        recipients = environment.get("compose")
        if not isinstance(recipients, list) or not all(
            isinstance(item, dict) for item in recipients
        ):
            raise TestingActivationRefusal("Original recipient inventory is incomplete.")
        candidates = [item for item in recipients if item.get("name") == record.namespace]
        if not candidates and record.phase == "environment_ready":
            return record
        if len(candidates) != 1:
            raise TestingActivationRefusal("Original recipient is absent or ambiguous.")
        target_id = candidates[0].get("composeId")
        if (
            not isinstance(target_id, str)
            or not target_id
            or (record.target_id and record.target_id != target_id)
        ):
            raise TestingActivationRefusal("Original recipient identity changed.")
        if not record.target_id:
            if record.phase != "create_compose":
                raise TestingActivationRefusal("Unrequested recipient placement.")
            record = self.save(record, target_id=target_id, phase="recipient_ready")
        self.unused_recipient(record)
        return record

    def result(self, record: TestingActivationRecord) -> dict[str, Any]:
        return TestingActivationResult(
            product=self.product,
            context=record.context,
            plan_sha256=record.plan_sha256,
            source_commit=record.request.source_commit,
            resource_namespace=record.namespace,
            image_reference=record.image_reference,
            host_ports=record.host_ports,
            qualified_at=record.qualified_at,
        ).model_dump(mode="json")

    def publish(
        self,
        record: TestingActivationRecord,
        resources: OdooTestingResources,
        checkpoint: Callable[[str], None],
    ) -> TestingActivationRecord:
        values = runtime_values(self.store, self.root, record, resources)
        with self.store.serialize_disposable_testing_resources():
            checkpoint("publish_new_recipient")
            stamp = utc_now_timestamp()
            new_target = record.retained_target.model_copy(
                update={
                    "project_name": record.namespace,
                    "target_name": record.namespace,
                    "source_type": "raw",
                    "compose_path": "docker-compose.yml",
                    "custom_git_url": "",
                    "custom_git_branch": "",
                    # A private product may retain origin domains on its old
                    # recipient. They stay in custody, never on the replacement.
                    "domains": (),
                    "public_hosts": (),
                    "pending_public_host_effects": (),
                    "env": {
                        **{
                            key: resources.volumes[role]
                            for role, key in record.request.volume_keys.items()
                        },
                        **{key: values[key] for key in record.request.port_keys},
                    },
                    "updated_at": stamp,
                    "source_label": "service:testing-activation",
                }
            )
            new_id = DokployTargetIdRecord(
                context=record.context,
                instance="testing",
                target_id=record.target_id,
                updated_at=stamp,
                source_label="service:testing-activation",
            )
            new_provider = ProviderTargetRecord.from_dokploy_records(
                target_record=new_target, target_id_record=new_id
            )
            published = record.model_copy(
                update={
                    "published": True,
                    "qualified_at": stamp,
                    "phase": "qualified",
                    "published_target": new_target,
                }
            )
            self.store.write_testing_activation(
                expected=record.model_dump(mode="json"),
                replacement=published.model_dump(mode="json"),
                bundle=ProductAuthorityBundle(
                    required_context_owner=(self.product, record.context),
                    expected_product_profiles=(
                        self.store.read_product_profile_record(self.product),
                    ),
                    dokploy_targets=(new_target,),
                    dokploy_target_ids=(new_id,),
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=new_provider, expected_record=record.retained_provider
                        ),
                    ),
                ),
            )
        return published

    def prepare_credentials(self, record: TestingActivationRecord) -> TestingActivationRecord:
        from control_plane.testing_local_credentials import generation_bundle

        if record.generated_configuration_sha256:
            return record
        profile = self.store.read_product_profile_record(self.product)
        bundle = generation_bundle(
            self.store,
            profile,
            record.context,
            actor="service:testing-activation",
            generation=record.preparation_record_id,
        )
        if not bundle.secret_records:
            return record
        if record.configuration_version != 3:
            raise TestingActivationRefusal(
                "Legacy activation cannot introduce newly declared local credentials."
            )
        self.require_unchanged(record)
        replacement = record.model_copy(
            update={
                "generated_configuration_sha256": configuration_digest(
                    self.store, self.product, record.context, version=3, generated=bundle
                ),
            }
        )
        self.store.write_testing_activation(
            expected=record.model_dump(mode="json"),
            replacement=replacement.model_dump(mode="json"),
            bundle=bundle.model_copy(
                update={
                    "expected_product_profiles": (profile,),
                    "required_context_owner": (self.product, record.context),
                }
            ),
        )
        self.require_unchanged(replacement)
        return replacement

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            if activation_record(self.store, self.product, self.request.context) is None:
                # Custody is persisted before the first placement creation. Its
                # absence proves an interrupted inspection created no recipient.
                return ProviderObservation("absent", retry_safe=True)
            record = self.current()
            if record.plan_sha256 != reconciliation_key:
                return ProviderObservation("unknown")
            if record.phase == "prepared" and not (
                record.project_id or record.environment_id or record.target_id
            ):
                return ProviderObservation("absent", retry_safe=True)
            old_key = generic_web_provider_target_key(
                provider_id="dokploy",
                context=record.context,
                instance="testing",
                target_type="compose",
                target_id=record.retained_target_id.target_id,
            )
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(old_key)
            ) as old_check:
                if old_check is None:
                    return ProviderObservation("unknown")
                self.require_unchanged(record)
                if not record.published:
                    record = self.recover_placement(record)
                    if record.phase not in {"configure_new_recipient", "materialize_new_recipient"}:
                        return ProviderObservation("absent", retry_safe=True)
                    try:
                        self.verify_provider(record)
                    except ValueError as error:
                        fresh = self.unused_recipient(record)
                        if (
                            isinstance(error, TestingActivationRefusal)
                            and error.code
                            == TestingActivationRefusalCode.RECIPIENT_SOURCE_UNMATERIALIZED
                        ):
                            return ProviderObservation("absent", retry_safe=True)
                        if record.phase == "materialize_new_recipient":
                            return ProviderObservation("unknown")
                        if (
                            self.recipient_configuration_sha256(fresh)
                            != record.before_configuration_sha256
                        ):
                            return ProviderObservation("unknown")
                        return ProviderObservation("absent", retry_safe=True)
                    resources, _ = preparation_resources(
                        self.store, self.product, self.request, self.scope
                    )

                    def checkpoint(_phase: str) -> None:
                        old_check()
                        self.require_unchanged(record)

                    inspect_new_resources(
                        self.root,
                        resources,
                        ports_for(record, runtime_values(self.store, self.root, record, resources)),
                        checkpoint,
                    )
                    record = self.publish(record, resources, checkpoint)
                else:
                    self.verify_provider(record)
            return ProviderObservation(
                "present",
                response_status_code=202,
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=self.result(record)
                ),
            )
        except Exception:
            return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            record = self.current()
            if record.plan_sha256 != self.request.reviewed_plan_sha256:
                raise TestingActivationRefusal("Review the changed activation plan.")
            existing = activation_record(self.store, self.product, record.context)
            if existing is not None and existing.phase not in {
                "prepared",
                "project_ready",
                "environment_ready",
                "recipient_ready",
                "configure_new_recipient",
                "materialize_new_recipient",
            }:
                raise TestingActivationRefusal(
                    "Continue the original activation by readback, never redispatch creation."
                )
            old_key = generic_web_provider_target_key(
                provider_id="dokploy",
                context=record.context,
                instance="testing",
                target_type="compose",
                target_id=record.retained_target_id.target_id,
            )
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(old_key)
            ) as old_check:
                if old_check is None:
                    raise TestingActivationRefusal("The retained executor is active.")
                excluded = inspect_generic_web_testing_exclusion(
                    store=self.store,
                    root=self.root,
                    product=self.product,
                    context=record.context,
                    instance="testing",
                    trace_id=self.trace_id,
                    held_execution_check=old_check,
                )
                if excluded.status != "excluded":
                    raise TestingActivationRefusal("The retained recipient is not fully excluded.")
                self.require_unchanged(record)
                resources, _ = preparation_resources(
                    self.store, self.product, self.request, self.scope
                )

                def checkpoint(phase: str) -> None:
                    nonlocal started
                    old_check()
                    lease.assert_current()
                    self.require_unchanged(record)
                    lease.checkpoint_effect(phase)
                    started = True

                host, token = source.read_dokploy_config(control_plane_root=self.root)
                old = api.fetch_dokploy_target_payload(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=record.retained_target_id.target_id,
                )
                if any(
                    item.get("name") == record.namespace
                    and item.get("projectId") != record.project_id
                    for item in api.fetch_dokploy_projects(host=host, token=token)
                ):
                    raise TestingActivationRefusal(
                        "Preserve an existing activation placement for original-key inspection."
                    )
                if existing is None:
                    require_bridge_network_capacity(
                        host=host,
                        token=token,
                        server_id=str(old.get("serverId") or ""),
                        required_networks=compose_bridge_network_requirement(
                            json.loads(record.compose_source)
                        ),
                    )
                inspect_new_resources(
                    self.root,
                    resources,
                    ports_for(record, runtime_values(self.store, self.root, record, resources)),
                    checkpoint,
                )
                # A refused/transient inspection must not strand a permanent
                # custody row. Persist immediately before creation, after all
                # prerequisites; every subsequent uncertain write stays fenced.
                checkpoint("prepare_new_recipient")
                if existing is None:
                    self.store.write_testing_activation(
                        expected=None, replacement=record.model_dump(mode="json")
                    )
                for phase, path, body, field in (
                    (
                        "create_project",
                        "/api/project.create",
                        {"name": record.namespace, "description": "Independent testing recipient"},
                        "projectId",
                    ),
                    ("create_environment", "/api/environment.create", {}, "environmentId"),
                    ("create_compose", "/api/compose.create", {}, "composeId"),
                ):
                    storage_field = {
                        "projectId": "project_id",
                        "environmentId": "environment_id",
                        "composeId": "target_id",
                    }[field]
                    if getattr(record, storage_field):
                        continue
                    if phase == "create_environment":
                        body = {"name": record.namespace, "projectId": record.project_id}
                    if phase == "create_compose":
                        body = {
                            "name": record.namespace,
                            "appName": record.namespace,
                            "environmentId": record.environment_id,
                            "composeType": "docker-compose",
                        }
                        if old.get("serverId"):
                            body["serverId"] = str(old["serverId"])
                    checkpoint(phase)
                    record = self.save(record, phase=phase)
                    response = api.dokploy_request(
                        host=host,
                        token=token,
                        path=path,
                        method="POST",
                        payload=cast(api.JsonObject, body),
                    )
                    # Current Dokploy returns {project, environment}; older
                    # supported servers returned the project directly.
                    if (
                        field == "projectId"
                        and isinstance(response, dict)
                        and isinstance(response.get("project"), dict)
                    ):
                        response = response["project"]
                    if (
                        not isinstance(response, dict)
                        or not isinstance(response.get(field), str)
                        or not response[field]
                    ):
                        raise TestingActivationRefusal(
                            "Creation outcome is unknown; retain its original key."
                        )
                    record = self.save(
                        record,
                        **{
                            storage_field: response[field],
                            "phase": {
                                "projectId": "project_ready",
                                "environmentId": "environment_ready",
                                "composeId": "recipient_ready",
                            }[field],
                        },
                    )
                fresh = self.unused_recipient(record)
                app_name = fresh.get("appName")
                if (
                    not isinstance(app_name, str)
                    or not re.fullmatch(
                        re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?", app_name
                    )
                    or fresh.get("environmentId") != record.environment_id
                ):
                    raise TestingActivationRefusal(
                        "Fresh recipient has unexpected identity or environment."
                    )
                configured = False
                if record.phase in {"configure_new_recipient", "materialize_new_recipient"}:
                    if (
                        record.phase == "materialize_new_recipient"
                        or self.recipient_configuration_sha256(fresh)
                        != record.before_configuration_sha256
                    ):
                        try:
                            self.verify_provider(record)
                        except TestingActivationRefusal as error:
                            if (
                                error.code
                                != TestingActivationRefusalCode.RECIPIENT_SOURCE_UNMATERIALIZED
                            ):
                                raise
                        configured = True
                else:
                    if fresh.get("env") not in {None, ""}:
                        raise TestingActivationRefusal(
                            "Fresh recipient has an unexpected environment."
                        )
                    record = self.save(
                        record,
                        app_name=app_name,
                        before_configuration_sha256=self.recipient_configuration_sha256(fresh),
                    )
                record = self.prepare_credentials(record)
                values = runtime_values(self.store, self.root, record, resources)
                if not configured:
                    rendered_source(record, values, resources)
                    template = json.loads(record.compose_source)
                    for volume in template["volumes"].values():
                        volume["external"] = True
                    env_text = api.serialize_dokploy_env_text(values, target_type="compose")
                    record = self.save(record, phase="configure_new_recipient")
                    checkpoint(record.phase)
                    api.dokploy_request(
                        host=host,
                        token=token,
                        path="/api/compose.update",
                        method="POST",
                        payload={
                            "composeId": record.target_id,
                            "name": record.namespace,
                            "environmentId": record.environment_id,
                            "sourceType": "raw",
                            "composePath": "docker-compose.yml",
                            "autoDeploy": False,
                            "composeFile": json.dumps(template),
                            "env": env_text,
                        },
                    )
                record = self.materialize_source(record, checkpoint)
                inspect_new_resources(self.root, resources, ports_for(record, values), checkpoint)
                published = self.publish(record, resources, checkpoint)
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=self.result(published)
                    ),
                )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    error
                    if isinstance(
                        error,
                        (
                            TestingActivationRefusal,
                            BridgeNetworkCapacityRefused,
                            NativeSourceAuthorizationDenied,
                        ),
                    )
                    else ValueError("Activation refused before provider effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Activation outcome is unknown; retain the original key, placement, old hold and fences."
            ) from error
