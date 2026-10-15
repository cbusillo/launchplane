"""Activate only new testing resources; retain the old recipient and unknown effects."""

import base64
from collections.abc import Callable
from dataclasses import dataclass
import hmac
import json
from pathlib import Path
import re
from typing import Any, cast

import yaml

from control_plane import runtime_environments
from control_plane.build_provenance import GitHubBuildProvenanceTransport
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.testing_activation import (
    TestingActivationPlan,
    TestingActivationRecord,
    TestingActivationRequest,
    TestingActivationResult,
)
from control_plane.disposable_lanes import disposable_lane_configuration
from control_plane.dokploy import api, compose, post_deploy, source
from control_plane.generic_web_testing_exclusion import (
    _bind,
    _target_reservations,
    inspect_generic_web_testing_exclusion,
)
from control_plane.github_app_identity import (
    mint_source_control_read_installation_token,
)
from control_plane.integration_allowances import target_record_sha256
from control_plane.live_target_runtime import evaluate_runtime_key_safety_for_live_target_sync
from control_plane.odoo_testing_resources import (
    GenericWebTestingResources,
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
    store: PostgresRecordStore, product: str, context: str
) -> TestingActivationRecord | None:
    payload = store.read_testing_activation(product, context)
    return TestingActivationRecord.model_validate(payload) if payload is not None else None


def configuration_digest(store: PostgresRecordStore, product: str, context: str) -> str:
    return canonical_json_sha256(
        {
            "profile": store.read_product_profile_record(product).model_dump(mode="json"),
            "configuration": [
                row.model_dump(mode="json")
                for row in disposable_lane_configuration(store, context, "testing")
                if not hasattr(row, "target_name")
            ],
            "key_policy": [
                row.model_dump(mode="json")
                for row in store.list_runtime_key_safety_policy_records(status="active", limit=1)
            ],
        }
    )


def preparation_resources(
    store: PostgresRecordStore, product: str, request: TestingActivationRequest, scope: str
) -> tuple[GenericWebTestingResources, str]:
    row = store.read_idempotency_record(
        scope=scope,
        route_path=PREPARATION_ROUTE.format(product=product),
        idempotency_key=request.preparation_key,
    )
    if row is None or row.state != "completed":
        raise ValueError("Use the original caller's completed allocation key.")
    receipt = SeparationResourcesResult.model_validate(row.response_payload.get("result"))
    if (receipt.product, receipt.context) != (product, request.context):
        raise ValueError("Allocation belongs to another testing lane.")
    old_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name="testing"
    )
    existing = activation_record(store, product, request.context)
    resources = GenericWebTestingResources(
        product=product,
        context=request.context,
        operation_id=receipt.plan_sha256,
        target_id=existing.retained_target_id.target_id if existing else old_id.target_id,
        namespace=f"lp-testing-{receipt.plan_sha256[:32]}",
    )
    if (
        receipt.volumes != resources.volumes
        or receipt.resources_binding_sha256 != resources.binding_sha256
    ):
        raise ValueError("Allocation provenance does not agree with its original placement.")
    return resources, row.record_id


def runtime_values(
    store: PostgresRecordStore,
    root: Path,
    record: TestingActivationRecord,
    resources: GenericWebTestingResources,
) -> dict[str, str]:
    environment = runtime_environments.resolve_site_runtime_environment(
        control_plane_root=root,
        context_name=record.context,
        instance_name="testing",
        database_url=store.database_url,
    )
    values = dict(environment.values)
    for role, key in record.request.volume_keys.items():
        if key in environment.secret_keys:
            raise ValueError("A resource setting cannot be secret-backed.")
        values[key] = resources.volumes[role]
    safety = evaluate_runtime_key_safety_for_live_target_sync(
        record_store=store, context_name=record.context, instance_name="testing"
    )
    if safety.get("status") not in {"pass", "skipped"} or safety.get("reported"):
        raise ValueError("Carried-key safety remains unproved.")
    bindings = runtime_environments.site_delivered_secret_bindings_from_store(
        record_store=store, context_name=record.context, instance_name="testing"
    )
    shared: set[str] = set()
    for key in environment.secret_keys:
        binding = bindings[key]
        secret = store.read_secret_record(binding.secret_id)
        if (secret.scope, secret.context, secret.instance) != (
            "context_instance",
            record.context,
            "testing",
        ):
            raise ValueError("Testing credentials must have lane-specific bindings.")
        if (
            binding.declared_secret_class == "shared_safe"
            and binding.sharing_reason
            and binding.sharing_reason.kind == "read_only_source"
        ):
            shared.add(key)
        elif binding.declared_secret_class not in {"testing", "non_prod"}:
            raise ValueError("A carried key can reach a live writable system.")
    if any(
        looks_like_credential(key, value) and key not in environment.secret_keys
        for key, value in values.items()
    ):
        raise ValueError("Application credentials require safe managed bindings.")
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
            raise ValueError("Opaque production credential comparison is unavailable.")
        for key in environment.secret_keys - shared:
            if any(
                value and hmac.compare_digest(values[key].encode(), value.encode())
                for value in production.values()
            ):
                raise ValueError("A testing credential overlaps production.")
    values["DOCKER_IMAGE_REFERENCE"] = record.image_reference
    return values


def ports_for(record: TestingActivationRecord, values: dict[str, str]) -> set[int]:
    raw = [values.get(key, "") for key in record.request.port_keys]
    if (
        not raw
        or any(not value.isdecimal() or not 1024 <= int(value) <= 65535 for value in raw)
        or len(set(raw)) != len(raw)
    ):
        raise ValueError("Independent testing ports require distinct existing lane settings.")
    return {int(value) for value in raw}


def rendered_source(
    record: TestingActivationRecord, values: dict[str, str], resources: GenericWebTestingResources
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
    return cast(dict[str, Any], rendered)


def target_without_hold(target: Any) -> dict[str, Any]:
    payload = target.model_dump(mode="json", exclude={"updated_at", "source_label"})
    payload["policies"].pop("staff_testing_hold", None)
    return cast(dict[str, Any], payload)


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
        if (
            record is None
            or not record.published
            or reservation not in record.retained_reservations
        ):
            return False
        target = store.read_provider_target_record(
            context_name=record.context, instance_name="testing"
        )
        return (
            target.target_id == record.target_id
            and target.target_id != record.retained_target_id.target_id
            and record.published_target is not None
            and target_without_hold(
                store.read_dokploy_target_record(
                    context_name=record.context, instance_name="testing"
                )
            )
            == target_without_hold(record.published_target)
        )
    except (ValueError, FileNotFoundError):
        return False


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
        return False
    record = activation_record(store, request.product, request.context)
    if (
        record is None
        or not record.published
        or record.actor != actor
        or record.plan_sha256 != request.activation_plan_sha256
        or request.hold
    ):
        raise ValueError("The activation does not authorize this interlock continuation.")
    adapter = TestingActivationAdapter(
        root,
        store,
        record.product,
        record.request,
        record.actor,
        record.scope,
        "activation_hold_preflight",
    )
    adapter.require_unchanged(record)
    target = store.read_dokploy_target_record(context_name=record.context, instance_name="testing")
    if target.policies.staff_testing_hold != record.retained_target.policies.staff_testing_hold:
        raise ValueError("A later genuine staff hold remains protected.")
    if not inspect_provider:
        return True
    adapter.verify_provider(record)
    resources, _ = preparation_resources(store, record.product, record.request, record.scope)
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
            raise ValueError("Retain the original activation request and caller.")
        return existing
    profile, target, target_id, provider = _bind(store, product, request.context, "testing")
    if not profile.is_active or target_record_sha256(target) != request.interlock_record_sha256:
        raise ValueError("Explicit staging-interlock identification no longer matches.")
    hold = target.policies.staff_testing_hold
    if hold is None or hold.recorded_by != actor:
        raise ValueError("An absent, foreign or unacknowledged staff hold is protected.")
    protection = store.read_disposable_lane_safety(product, request.context, "testing")
    if protection and protection.usage == "in_use":
        raise ValueError("Known staff use protects the old testing recipient.")
    resources, receipt_id = preparation_resources(store, product, request, scope)
    binding, old_key = preparation_binding(store, product, request.context, actor, root)
    if (
        resources.operation_id != binding
        or set(request.volume_keys) != set(resources.volumes)
        or len(set(request.volume_keys.values())) != len(resources.volumes)
    ):
        raise ValueError("Original allocation or its role mapping changed.")
    if any(
        not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
        for key in (*request.volume_keys.values(), *request.port_keys)
    ) or set(request.volume_keys.values()).intersection(request.port_keys):
        raise ValueError("Resource settings require distinct explicit key names.")
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
        compose_source=document,
        image_reference=image_reference,
    )
    record.plan_sha256 = canonical_json_sha256(record.model_dump(exclude={"plan_sha256"}))
    rendered_source(record, runtime_values(store, root, record, resources), resources)
    return record


def plan(record: TestingActivationRecord) -> dict[str, Any]:
    return TestingActivationPlan(
        product=record.product,
        context=record.context,
        plan_sha256=record.plan_sha256,
        source_commit=record.request.source_commit,
    ).model_dump(mode="json")


def inspect_new_resources(
    root: Path,
    resources: GenericWebTestingResources,
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
        raise ValueError("Actual empty storage or independent ports are unproved.")


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

    def require_unchanged(self, record: TestingActivationRecord) -> None:
        if (
            configuration_digest(self.store, self.product, record.context)
            != record.configuration_sha256
        ):
            raise ValueError("Testing configuration or key policy changed.")
        _, target, target_id, provider = _bind(self.store, self.product, record.context, "testing")
        if record.published:
            if (
                target_id.target_id != record.target_id
                or provider.target_id != record.target_id
                or record.published_target is None
                or target_without_hold(target) != target_without_hold(record.published_target)
            ):
                raise ValueError("Activated testing recipient changed.")
        elif (target, target_id, provider) != (
            record.retained_target,
            record.retained_target_id,
            record.retained_provider,
        ):
            raise ValueError("Retained testing authority changed.")
        for original in record.retained_reservations:
            current = self.store.read_idempotency_record(
                scope=original.scope,
                route_path=original.route_path,
                idempotency_key=original.idempotency_key,
            )
            if current != original:
                raise ValueError("The retained original reservation changed.")

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
            or payload.get("sourceType") != "raw"
            or payload.get("autoDeploy") is not False
            or payload.get("mounts")
            or payload.get("serviceNetworks")
            or payload.get("randomize")
            or payload.get("isolatedDeployment")
            or json.loads(str(payload.get("composeFile") or "null")) != expected
        ):
            raise ValueError("Fresh provider source or recipient identity changed.")
        if api.parse_dokploy_env_text(str(payload.get("env") or "")) != values:
            raise ValueError("Fresh provider environment differs from safe managed inputs.")
        actual = yaml.safe_load(
            compose.fetch_dokploy_converted_compose_file(
                host=host, token=token, compose_id=record.target_id
            )
        )
        if isinstance(actual, dict) and isinstance(actual.get("services"), dict):
            for service_name, service in actual["services"].items():
                labels = service.pop("labels", {})
                if isinstance(labels, list):
                    labels = dict(item.split("=", 1) for item in labels)
                if not isinstance(labels, dict) or set(labels) - {
                    "dokploy.organization.id",
                    "dokploy.project.id",
                    "dokploy.project",
                    "dokploy.environment.id",
                    "dokploy.environment",
                    "dokploy.application.id",
                    "dokploy.application",
                    "dokploy.service",
                }:
                    raise ValueError("The provider added an unqualified routing label.")
                if labels and (
                    labels.get("dokploy.application.id") != record.target_id
                    or labels.get("dokploy.service") != service_name
                ):
                    raise ValueError("Provider metadata labels identify another recipient.")
        actual = interpolate(actual, values)
        qualify_compose(
            actual,
            volumes=set(resources.volumes.values()),
            ports=ports_for(record, values),
            credential_values={
                value for key, value in values.items() if looks_like_credential(key, value)
            },
        )
        # Semantic equality prevents a provider conversion from replacing safe env,
        # command or health values while retaining only the storage settings.
        expected_render = interpolate(expected, values)
        if isinstance(actual, dict):
            actual.pop("name", None)
        if actual != expected_render:
            raise ValueError("Actual provider render differs from the reviewed source and values.")

    def result(self, record: TestingActivationRecord) -> dict[str, Any]:
        return TestingActivationResult(
            product=self.product,
            context=record.context,
            plan_sha256=record.plan_sha256,
            source_commit=record.request.source_commit,
            qualified_at=record.qualified_at,
        ).model_dump(mode="json")

    def publish(
        self,
        record: TestingActivationRecord,
        resources: GenericWebTestingResources,
        checkpoint: Callable[[str], None],
    ) -> TestingActivationRecord:
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
                    "env": {
                        **record.retained_target.env,
                        **{
                            key: resources.volumes[role]
                            for role, key in record.request.volume_keys.items()
                        },
                    },
                    "updated_at": stamp,
                    "source_label": "service:testing-activation",
                }
            )
            if (
                new_target.domains
                or new_target.public_hosts
                or new_target.pending_public_host_effects
            ):
                raise ValueError("Existing routes require their own accepted cutover.")
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

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            record = self.current()
            if record.plan_sha256 != reconciliation_key:
                return ProviderObservation("unknown")
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
                self.verify_provider(record)
                if not record.published:
                    if record.phase != "configure_new_recipient":
                        return ProviderObservation("unknown")
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
                raise ValueError("Review the changed activation plan.")
            if activation_record(self.store, self.product, record.context) is not None:
                raise ValueError(
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
                    raise ValueError("The retained executor is active.")
                excluded = inspect_generic_web_testing_exclusion(
                    store=self.store,
                    root=self.root,
                    product=self.product,
                    context=record.context,
                    instance="testing",
                    trace_id=self.trace_id,
                )
                if excluded.status != "excluded":
                    raise ValueError("The retained recipient is not fully excluded.")
                self.require_unchanged(record)
                self.store.write_testing_activation(
                    expected=None, replacement=record.model_dump(mode="json")
                )
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

                inspect_new_resources(
                    self.root,
                    resources,
                    ports_for(record, runtime_values(self.store, self.root, record, resources)),
                    checkpoint,
                )
                host, token = source.read_dokploy_config(control_plane_root=self.root)
                old = api.fetch_dokploy_target_payload(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=record.retained_target_id.target_id,
                )
                if any(
                    item.get("name") == record.namespace
                    for item in api.fetch_dokploy_projects(host=host, token=token)
                ):
                    raise ValueError(
                        "Preserve an existing activation placement for original-key inspection."
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
                    record = self.save(record, phase=phase)
                    checkpoint(phase)
                    response = api.dokploy_request(
                        host=host,
                        token=token,
                        path=path,
                        method="POST",
                        payload=cast(api.JsonObject, body),
                    )
                    if (
                        not isinstance(response, dict)
                        or not isinstance(response.get(field), str)
                        or not response[field]
                    ):
                        raise ValueError("Creation outcome is unknown; retain its original key.")
                    record = self.save(
                        record,
                        **{
                            {
                                "projectId": "project_id",
                                "environmentId": "environment_id",
                                "composeId": "target_id",
                            }[field]: response[field]
                        },
                    )
                fresh = api.fetch_dokploy_target_payload(
                    host=host, token=token, target_type="compose", target_id=record.target_id
                )
                app_name = fresh.get("appName")
                if (
                    not isinstance(app_name, str)
                    or not re.fullmatch(
                        re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?", app_name
                    )
                    or fresh.get("environmentId") != record.environment_id
                    or fresh.get("env") not in {None, ""}
                ):
                    raise ValueError("Fresh recipient has unexpected identity or environment.")
                record = self.save(record, app_name=app_name)
                values = runtime_values(self.store, self.root, record, resources)
                rendered_source(record, values, resources)
                template = json.loads(record.compose_source)
                for volume in template["volumes"].values():
                    volume["external"] = True
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
                        "env": api.serialize_dokploy_env_text(values),
                    },
                )
                self.verify_provider(record)
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
                    ValueError("Activation refused before provider effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Activation outcome is unknown; retain the original key, placement, old hold and fences."
            ) from error
