"""Prepare an independent private recipient without publishing or starting it."""

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, cast

import yaml

from control_plane import runtime_environments
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.production_recipient import (
    ProductionRecipientPlan,
    ProductionRecipientRecord,
    ProductionRecipientRequest,
)
from control_plane.dokploy import api, compose, source
from control_plane.generic_web_deploy_http import resolve_generic_web_deploy_lane
from control_plane.production_recipient_resources import inspect_resources
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
from control_plane.testing_activation import read_product_compose, verified_product_image
from control_plane.testing_activation_compose import interpolate, qualify_compose
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = "/v1/products/{product}/production-recipient"


def custody(
    store: PostgresRecordStore, product: str, context: str
) -> ProductionRecipientRecord | None:
    payload = store.read_production_recipient(product, context)
    return ProductionRecipientRecord.model_validate(payload) if payload else None


def authority_snapshot(store: PostgresRecordStore, product: str, context: str) -> dict[str, Any]:
    profile, lane = resolve_generic_web_deploy_lane(
        record_store=store, product=product, instance="prod"
    )
    if (
        lane.context != context
        or not profile.is_active
        or profile.public_website != "none"
        or lane.generic_web_extensions.configured
    ):
        raise ValueError("A private, active generic-web production lane is required.")
    target = store.read_dokploy_target_record(context_name=context, instance_name="prod")
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name="prod")
    provider = store.read_provider_target_record(context_name=context, instance_name="prod")
    if (
        target.target_type != "compose"
        or provider.provider_id != "dokploy"
        or provider.target_category != "compose"
        or provider.target_id != target_id.target_id
        or target.public_hosts
        or target.pending_public_host_effects
    ):
        raise ValueError("Exact Compose authority without public traffic is required.")
    if any(
        row.target_id == target_id.target_id and (row.context, row.instance) != (context, "prod")
        for rows in (store.list_provider_target_records(), store.list_dokploy_target_id_records())
        for row in rows
    ):
        raise ValueError("Another lane shares this production target.")
    bindings = store.list_secret_bindings()
    # Include inherited bindings and their version metadata; never resolved values.
    selected_bindings = [
        row for row in bindings if row.context in {"", context} and row.instance in {"", "prod"}
    ]
    policies = store.list_production_backup_policy_records(
        product=product, context_name=context, instance_name="prod"
    )
    target_ids = {key for row in policies for key in row.target_ids}
    runtime = [
        row
        for row in store.list_runtime_environment_records()
        if (row.scope == "global" or row.context == context) and row.instance in {"", "prod"}
    ]
    try:
        inventory = store.read_environment_inventory(
            context_name=context, instance_name="prod"
        ).model_dump(mode="json")
    except FileNotFoundError:
        inventory = None
    return {
        "profile": profile.model_dump(mode="json"),
        "target": target.model_dump(mode="json"),
        "target_id": target_id.model_dump(mode="json"),
        "provider": provider.model_dump(mode="json"),
        "inventory": inventory,
        "runtime": [row.model_dump(mode="json") for row in runtime],
        "bindings": [row.model_dump(mode="json") for row in selected_bindings],
        "secrets": [
            store.read_secret_record(key).model_dump(mode="json")
            for key in sorted({row.secret_id for row in selected_bindings})
        ],
        "backup_policies": [row.model_dump(mode="json") for row in policies],
        "backup_targets": [
            row.model_dump(mode="json")
            for row in store.list_production_backup_target_records()
            if row.target_id in target_ids
        ],
        "health_endpoints": [
            row.model_dump(mode="json")
            for row in store.list_private_health_endpoint_records(
                product=product, context_name=context, instance_name="prod"
            )
        ],
    }


def runtime_values(
    store: PostgresRecordStore, root: Path, record: ProductionRecipientRecord
) -> dict[str, str]:
    environment = runtime_environments.resolve_site_runtime_environment(
        control_plane_root=root,
        context_name=record.context,
        instance_name="prod",
        database_url=store.database_url,
        target_environment_override={},
    )
    keys = {*record.request.volume_keys.values(), *record.request.ports}
    if keys & (environment.secret_keys | environment.retired_keys) or not keys <= set(
        environment.values
    ):
        raise ValueError("Use existing, nonsecret, unretired production resource settings.")
    values = dict(environment.values)
    for role, key in record.request.volume_keys.items():
        values[key] = record.volumes[role]
    for key, value in record.request.ports.items():
        if values.get(key) == str(value) or record.retained_target.env.get(key) == str(value):
            raise ValueError("Candidate ports must differ from production ports.")
        values[key] = str(value)
    values["DOCKER_IMAGE_REFERENCE"] = record.image_reference
    if any(
        looks_like_credential(key, value) and key not in environment.secret_keys
        for key, value in values.items()
    ):
        raise ValueError("Credentials require existing managed production bindings.")
    return values


def qualified_render(record: ProductionRecipientRecord, values: dict[str, str]) -> dict[str, Any]:
    document = interpolate(json.loads(record.compose_source), values)
    if not isinstance(document, dict):
        raise ValueError("Compose source is unavailable.")
    services = document.get("services", {})
    if set(services) != {record.request.application_service, record.request.database_service}:
        raise ValueError("Fresh-state preparation supports one application and one database.")
    # Runtime limits are product-owned source, not a service-host override. The
    # actual converted provider definition must agree with this exact source.
    for service in services.values():
        deploy = service.get("deploy", {})
        limits = deploy.get("resources", {}).get("limits", {})
        if (
            set(deploy) != {"resources"}
            or set(deploy["resources"]) != {"limits"}
            or set(limits) != {"cpus", "memory"}
        ):
            raise ValueError("Each service must declare only explicit CPU and memory limits.")
        if (
            not 0 < float(limits["cpus"]) <= 1
            or not 0 < memory_bytes(str(limits["memory"])) <= 2 * 1024**3
        ):
            raise ValueError("Each service must enforce at most one CPU and two GiB.")
    database = services[record.request.database_service]
    import re
    import shlex

    command = database.get("command", [])
    arguments = shlex.split(command) if isinstance(command, str) else command
    pools = [
        str(argument).split("=", 1)[1]
        for argument in arguments
        if str(argument).startswith("--innodb-buffer-pool-size=")
    ]
    if len(pools) != 1 or not 0 < memory_bytes(pools[0]) <= 512 * 1024**2:
        raise ValueError("The database must enforce its buffer-pool cap in product source.")
    qualified = deepcopy(document)
    for service in qualified["services"].values():
        service.pop("deploy")
        environment = service.get("environment", {})
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) for item in environment)
            service["environment"] = environment
        if isinstance(environment, dict):
            for key, value in tuple(environment.items()):
                if re.search(r"(?:_HOST|_URL)$", key) and value in values.values():
                    environment.pop(key)
    qualify_compose(
        qualified,
        volumes=set(record.volumes.values()),
        ports=set(record.request.ports.values()),
        credential_values={
            value for key, value in values.items() if looks_like_credential(key, value)
        },
    )
    if services[record.request.application_service]["image"] != record.image_reference:
        raise ValueError("The application must use its verified product artifact.")
    return cast(dict[str, Any], document)


def memory_bytes(value: str) -> int:
    import re

    match = re.fullmatch(r"([0-9]+)([kmgt]?)(?:i?b)?", value.strip(), re.IGNORECASE)
    if not match:
        raise ValueError("A bounded explicit memory size is required.")
    exponent = "kmgt".index(match[2].lower()) + 1 if match[2] else 0
    return int(int(match[1]) * (1024**exponent))


def build_recipient(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: ProductionRecipientRequest,
    actor: str,
    scope: str,
    original_key: str = "",
) -> ProductionRecipientRecord:
    existing = custody(store, product, request.context)
    intent = request.model_copy(update={"mode": "dry-run", "reviewed_plan_sha256": ""})
    if existing:
        if (existing.actor, existing.scope, existing.request) != (actor, scope, intent) or (
            original_key and original_key != existing.original_key
        ):
            raise ValueError("Retain the original production-recipient intent, caller and key.")
        return existing
    snapshot = authority_snapshot(store, product, request.context)
    from control_plane.contracts.dokploy_target_record import DokployTargetRecord
    from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
    from control_plane.contracts.deploy_target import ProviderTargetRecord

    target = DokployTargetRecord.model_validate(snapshot["target"])
    target_id = DokployTargetIdRecord.model_validate(snapshot["target_id"])
    host, token = source.read_dokploy_config(control_plane_root=root)
    old = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id.target_id
    )
    if old.get("composeId") != target_id.target_id:
        raise ValueError("The provider returned a different production recipient.")
    plan_digest = canonical_json_sha256(
        {
            "product": product,
            "request": intent.model_dump(mode="json"),
            "authority": snapshot,
            "provider_sha256": canonical_json_sha256(old),
        }
    )
    record = ProductionRecipientRecord(
        product=product,
        context=request.context,
        actor=actor,
        scope=scope,
        original_key=original_key,
        request=intent,
        plan_sha256="",
        namespace="lp-prod-recipient-" + plan_digest[:32],
        retained_target=target,
        retained_target_id=target_id,
        retained_provider=ProviderTargetRecord.model_validate(snapshot["provider"]),
        authority_snapshot=snapshot,
        retained_provider_sha256=canonical_json_sha256(old),
        compose_source=read_product_compose(
            store,
            product,
            request.source_commit,
            target.compose_path or str(old.get("composePath") or ""),
        ),
        image_reference=verified_product_image(store, product, request.source_commit),
    )
    values = runtime_values(store, root, record)
    qualified_render(record, values)
    # The key owns execution, but is not part of the saved dry-run's digest.
    record.plan_sha256 = canonical_json_sha256(
        record.model_dump(exclude={"plan_sha256", "original_key"})
    )
    return record


def plan(record: ProductionRecipientRecord) -> dict[str, Any]:
    return ProductionRecipientPlan(
        product=record.product,
        context=record.context,
        plan_sha256=record.plan_sha256,
        source_commit=record.request.source_commit,
        image_reference=record.image_reference,
        retained_binding_sha256=canonical_json_sha256(
            {
                "target": record.retained_target.model_dump(mode="json"),
                "target_id": record.retained_target_id.model_dump(mode="json"),
                "provider": record.retained_provider.model_dump(mode="json"),
            }
        ),
        qualified=bool(record.qualified_at),
        qualified_at=record.qualified_at,
    ).model_dump(mode="json")


@dataclass
class ProductionRecipientAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: ProductionRecipientRequest
    actor: str
    scope: str
    trace_id: str
    original_key: str = ""

    def target_key(self) -> str:
        return "production-recipient:" + self.product + ":" + self.request.context

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def current(self) -> ProductionRecipientRecord:
        return build_recipient(
            self.root,
            self.store,
            self.product,
            self.request,
            self.actor,
            self.scope,
            self.original_key,
        )

    def require_unchanged(self, record: ProductionRecipientRecord) -> None:
        if (
            authority_snapshot(self.store, self.product, record.context)
            != record.authority_snapshot
        ):
            raise ValueError("Production authority changed after review.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        old = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=record.retained_target_id.target_id,
        )
        if canonical_json_sha256(old) != record.retained_provider_sha256:
            raise ValueError("The retained production recipient changed after review.")

    def save(self, record: ProductionRecipientRecord, **updates: Any) -> ProductionRecipientRecord:
        replacement = record.model_copy(update=updates)
        self.store.write_production_recipient(
            expected=record.model_dump(mode="json"), replacement=replacement.model_dump(mode="json")
        )
        return replacement

    def verify(self, record: ProductionRecipientRecord) -> None:
        self.require_unchanged(record)
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.target_id
        )
        expected = json.loads(record.compose_source)
        for volume in expected["volumes"].values():
            volume["external"] = True
        if (
            payload.get("composeId") != record.target_id
            or payload.get("environmentId") != record.environment_id
            or payload.get("appName") != record.app_name
            or str(payload.get("serverId") or "") != record.request.destination_server_id
            or payload.get("sourceType") != "raw"
            or payload.get("autoDeploy") is not False
            or any(
                payload.get(field)
                for field in (
                    "domains",
                    "mounts",
                    "serviceNetworks",
                    "randomize",
                    "isolatedDeployment",
                )
            )
        ):
            raise ValueError("Recipient identity, isolation or configuration changed.")
        if json.loads(str(payload.get("composeFile") or "null")) != expected:
            raise ValueError("The provider source differs from the reviewed immutable source.")
        values = runtime_values(self.store, self.root, record)
        if api.parse_dokploy_env_text(str(payload.get("env") or "")) != values:
            raise ValueError("The provider environment differs from managed production inputs.")
        actual = yaml.safe_load(
            compose.fetch_dokploy_converted_compose_file(
                host=host, token=token, compose_id=record.target_id
            )
        )
        if not isinstance(actual, dict):
            raise ValueError("Provider Compose conversion is unavailable.")
        actual.pop("name", None)
        for name, service in actual.get("services", {}).items():
            labels = service.pop("labels", {})
            if isinstance(labels, list):
                labels = dict(label.split("=", 1) for label in labels)
            allowed = {
                "dokploy.organization.id",
                "dokploy.project.id",
                "dokploy.project",
                "dokploy.environment.id",
                "dokploy.environment",
                "dokploy.application.id",
                "dokploy.application",
                "dokploy.service",
            }
            if (
                not isinstance(labels, dict)
                or set(labels) - allowed
                or (
                    labels
                    and (
                        labels.get("dokploy.application.id") != record.target_id
                        or labels.get("dokploy.service") != name
                    )
                )
            ):
                raise ValueError("Provider-added labels are not private recipient metadata.")
        if interpolate(actual, values) != interpolate(expected, values):
            raise ValueError("The actual provider render differs from reviewed source and limits.")
        qualified_render(record, values)

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            record = custody(self.store, self.product, self.request.context)
            if record is None:
                return ProviderObservation("absent", retry_safe=True)
            if (
                record.plan_sha256 != reconciliation_key
                or record.scope != self.scope
                or record.original_key != self.original_key
                or not record.target_id
            ):
                return ProviderObservation("unknown")
            # Never redispatch a partially persisted create or unknown update.
            # A complete provider readback may adopt the original effect only.
            self.verify(record)
            inspect_resources(
                self.root,
                record,
                prepare=False,
                checkpoint=lambda phase: self.require_unchanged(self.current()),
            )
            if not record.qualified_at:
                record = self.save(record, qualified_at=utc_now_timestamp(), phase="qualified")
            return ProviderObservation(
                "present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=plan(record)
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
                raise ValueError("Review the current production recipient plan.")
            if custody(self.store, self.product, record.context) is not None:
                raise ValueError(
                    "Preserve the original operation; reconcile rather than redispatch."
                )

            def checkpoint(phase: str) -> None:
                nonlocal started
                lease.assert_current()
                self.require_unchanged(record)
                lease.checkpoint_effect(phase)
                started = True

            self.require_unchanged(record)
            lease.assert_current()
            self.store.write_production_recipient(
                expected=None, replacement=record.model_dump(mode="json")
            )
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            for phase, path, field, body in (
                (
                    "create_project",
                    "/api/project.create",
                    "projectId",
                    {
                        "name": record.namespace,
                        "description": "Private production replacement; serving binding preserved",
                    },
                ),
                ("create_environment", "/api/environment.create", "environmentId", {}),
                ("create_recipient", "/api/compose.create", "composeId", {}),
            ):
                if field == "environmentId":
                    body = {"name": record.namespace, "projectId": record.project_id}
                if field == "composeId":
                    body = {
                        "name": record.namespace,
                        "appName": record.namespace,
                        "environmentId": record.environment_id,
                        "serverId": record.request.destination_server_id,
                        "composeType": "docker-compose",
                    }
                record = self.save(record, phase=phase)
                checkpoint(phase)
                result = api.dokploy_request(
                    host=host,
                    token=token,
                    path=path,
                    method="POST",
                    payload=cast(api.JsonObject, body),
                )
                if (
                    field == "projectId"
                    and isinstance(result, dict)
                    and isinstance(result.get("project"), dict)
                ):
                    result = result["project"]
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get(field), str)
                    or not result[field]
                ):
                    raise ValueError("Creation outcome is unknown; retain the original key.")
                record = self.save(
                    record,
                    **{
                        {
                            "projectId": "project_id",
                            "environmentId": "environment_id",
                            "composeId": "target_id",
                        }[field]: result[field]
                    },
                )
            fresh = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=record.target_id
            )
            import re

            app_name = fresh.get("appName")
            if (
                not isinstance(app_name, str)
                or not re.fullmatch(re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?", app_name)
                or fresh.get("env")
                or fresh.get("domains")
            ):
                raise ValueError("Fresh recipient identity is unverified.")
            record = self.save(record, app_name=app_name)
            inspect_resources(self.root, record, prepare=True, checkpoint=checkpoint)
            template = json.loads(record.compose_source)
            for volume in template["volumes"].values():
                volume["external"] = True
            record = self.save(record, phase="configure_recipient")
            checkpoint(record.phase)
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/compose.update",
                method="POST",
                payload={
                    "composeId": record.target_id,
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": json.dumps(template),
                    "autoDeploy": False,
                    "env": api.serialize_dokploy_env_text(
                        runtime_values(self.store, self.root, record)
                    ),
                },
            )
            self.verify(record)
            inspect_resources(self.root, record, prepare=False, checkpoint=checkpoint)
            record = self.save(record, qualified_at=utc_now_timestamp(), phase="qualified")
            self.require_unchanged(record)
            return ProviderMutationOutcome(
                202,
                provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=plan(record)
                ),
            )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    ValueError("Production preparation refused before provider effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Keep the original key and both recipients; outcome requires reconciliation."
            ) from error
