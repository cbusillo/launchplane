"""Prepare an independent private recipient without publishing or starting it."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, cast

import yaml

from control_plane import runtime_environments
from control_plane.integration_allowances import canonical_sha256
from control_plane.contracts.production_recipient import (
    ProductionRecipientPlan,
    ProductionRecipientRecord,
    ProductionRecipientRequest,
)
from control_plane.dokploy import api, compose, source
from control_plane.generic_web_deploy_http import resolve_generic_web_deploy_lane
from control_plane.production_recipient_resources import inspect_resources
from control_plane.production_recipient_upstreams import (
    require_delivered_upstreams,
    upstream_connections,
    upstream_binding,
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
from control_plane.testing_activation import read_product_compose, verified_product_image
from control_plane.testing_activation_compose import interpolate, qualify_compose
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = "/v1/products/{product}/production-recipient"


def provider_binding_digest(payload: dict[str, Any]) -> str:
    """Pin configuration and placement, excluding deployment/status observations."""
    return canonical_sha256(
        {
            key: payload.get(key)
            for key in (
                "composeId",
                "appName",
                "serverId",
                "environmentId",
                "composeFile",
                "sourceType",
                "composeType",
                "env",
                "domains",
                "mounts",
                "serviceNetworks",
                "autoDeploy",
                "customGitUrl",
                "customGitBranch",
                "composePath",
            )
        }
    )


def custody(
    store: PostgresRecordStore, product: str, context: str
) -> ProductionRecipientRecord | None:
    payload = store.read_production_recipient(product, context)
    return ProductionRecipientRecord.model_validate(payload) if payload else None


def authority_snapshot(store: PostgresRecordStore, product: str, context: str) -> dict[str, Any]:
    from control_plane.product_config_service import resolve_product_config_profile

    resolve_product_config_profile(store, product=product, context=context, instance="prod")
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
    require_delivered_upstreams(store, record)
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
    try:
        return _qualified_render(record, values)
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError("Malformed production Compose source is not qualified.") from error


def _qualified_render(record: ProductionRecipientRecord, values: dict[str, str]) -> dict[str, Any]:
    document = interpolate(json.loads(record.compose_source), values)
    if not isinstance(document, dict):
        raise ValueError("Compose source is unavailable.")
    services = document.get("services", {})
    if set(services) != {record.request.application_service, record.request.database_service}:
        raise ValueError("Fresh-state preparation supports one application and one database.")
    # Runtime limits are product-owned source, not a service-host override. The
    # actual converted provider definition must agree with this exact source.
    for name, service in services.items():
        required = record.request.service_limits[name]
        deploy = service.get("deploy", {})
        limits = deploy.get("resources", {}).get("limits", {})
        if (
            set(deploy) != {"resources"}
            or set(deploy["resources"]) != {"limits"}
            or set(limits) != {"cpus", "memory"}
        ):
            raise ValueError("Each service must declare only explicit CPU and memory limits.")
        if (
            not 0 < float(limits["cpus"]) <= required.cpus
            or not 0 < memory_bytes(str(limits["memory"])) <= required.memory_bytes
        ):
            raise ValueError("Product source exceeds its reviewed service resource limits.")
        import shlex

        command = service.get("command", [])
        arguments = shlex.split(command) if isinstance(command, str) else command
        if not isinstance(arguments, list) or any(
            not isinstance(argument, str) for argument in arguments
        ):
            raise ValueError("Command arguments must be explicit strings.")

        def normalized_option(argument: object) -> str:
            option = str(argument).split("=", 1)[0].replace("_", "-")
            while option.startswith(("--loose-", "--maximum-")):
                option = "--" + option.split("-", 3)[3]
            return option

        if name == record.request.database_service and (
            "entrypoint" in service
            or any(normalized_option(argument).startswith("--defaults-") for argument in arguments)
        ):
            raise ValueError(
                "The database must use its image entrypoint and explicit reviewed command limits."
            )
        if name == record.request.database_service and required.command_memory_limits:
            index = int(bool(arguments and arguments[0] in {"mariadbd", "mysqld"}))
            while index < len(arguments):
                argument = arguments[index]
                if not argument.startswith("--") or argument == "--":
                    raise ValueError(
                        "Database memory bounds require direct database options, not a wrapper."
                    )
                index += (
                    2
                    if "=" not in argument
                    and normalized_option(argument) in required.command_memory_limits
                    else 1
                )
        for argument_name, maximum in required.command_memory_limits.items():
            configured = [
                (
                    str(argument).split("=", 1)[1]
                    if "=" in str(argument)
                    else str(arguments[index + 1])
                    if index + 1 < len(arguments)
                    else ""
                )
                for index, argument in enumerate(arguments)
                if normalized_option(argument) == argument_name
            ]
            if len(configured) != 1 or not 0 < memory_bytes(configured[0]) <= maximum:
                raise ValueError("Product source must enforce its reviewed command memory limit.")
    qualified = deepcopy(document)
    upstream_connections(record, document, values)
    for upstream in record.request.upstreams:
        environment = qualified["services"][upstream.service].get("environment", {})
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) for item in environment)
            qualified["services"][upstream.service]["environment"] = environment
        for key in upstream.keys.values():
            environment.pop(key, None)
    for service in qualified["services"].values():
        service.pop("deploy")
        environment = service.get("environment", {})
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) for item in environment)
            service["environment"] = environment
        if isinstance(environment, dict):
            from urllib.parse import urlsplit

            for key, value in tuple(environment.items()):
                if key.endswith("_URL") and isinstance(value, str):
                    parsed = urlsplit(value)
                    if parsed.hostname in {*services, "localhost", "127.0.0.1", "::1"}:
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
    if existing and (existing.actor, existing.scope) != (actor, scope):
        raise ValueError("Retain the original production-recipient caller.")
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
    if existing:
        if (
            existing.request == intent
            and existing.authority_snapshot == snapshot
            and existing.retained_provider_sha256 == provider_binding_digest(old)
        ):
            if original_key and original_key != existing.original_key:
                raise ValueError("Retain the original production-recipient key.")
            return existing
        reservation = store.read_idempotency_record(
            scope=existing.scope,
            route_path=ROUTE.format(product=product),
            idempotency_key=existing.original_key,
        )
        if (
            any(
                (
                    existing.project_id,
                    existing.environment_id,
                    existing.target_id,
                    existing.qualified_at,
                )
            )
            or reservation is not None
            or original_key == existing.original_key
        ):
            raise ValueError(
                "Retain custody with any possible provider effect under its original key."
            )
    plan_digest = canonical_sha256(
        {
            "product": product,
            "request": intent.model_dump(mode="json"),
            "authority": snapshot,
            "provider_sha256": provider_binding_digest(old),
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
        retained_provider_sha256=provider_binding_digest(old),
        compose_source=read_product_compose(
            store,
            product,
            request.source_commit,
            target.compose_path or str(old.get("composePath") or ""),
        ),
        image_reference=verified_product_image(store, product, request.source_commit),
        supersedes_plan_sha256=existing.plan_sha256 if existing else "",
        prior_attempts=(
            [*existing.prior_attempts, existing.model_dump(mode="json", exclude={"prior_attempts"})]
            if existing
            else []
        ),
    )
    values = runtime_values(store, root, record)
    qualified_render(record, values)
    # The key owns execution, but is not part of the saved dry-run's digest.
    record.plan_sha256 = canonical_sha256(
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
        retained_binding_sha256=canonical_sha256(
            {
                "target": record.retained_target.model_dump(mode="json"),
                "target_id": record.retained_target_id.model_dump(mode="json"),
                "provider": record.retained_provider.model_dump(mode="json"),
            }
        ),
        qualified=bool(record.qualified_at),
        qualified_at=record.qualified_at,
        upstream_qualified=bool(record.upstream_proof),
        recovery_qualified=bool(record.recovery_proof),
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
        if provider_binding_digest(old) != record.retained_provider_sha256:
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

    def unused_recipient(self, record: ProductionRecipientRecord) -> dict[str, Any]:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.target_id
        )
        if (
            payload.get("composeId") != record.target_id
            or payload.get("environmentId") != record.environment_id
            or str(payload.get("serverId") or "") != record.request.destination_server_id
            or any(
                payload.get(key)
                for key in (
                    "domains",
                    "mounts",
                    "serviceNetworks",
                    "randomize",
                    "isolatedDeployment",
                )
            )
            or api.list_deployments_for_target(
                host=host, token=token, target_type="compose", target_id=record.target_id
            )
        ):
            raise ValueError("Retain a recipient with unqualified identity or deployment effects.")
        return cast(dict[str, Any], payload)

    def recover_placement(
        self,
        record: ProductionRecipientRecord,
        *,
        persist: bool = True,
        guard: Callable[[], None] | None = None,
    ) -> ProductionRecipientRecord:
        """Adopt only observed IDs in this original namespace; never repeat an unknown create."""

        def advance(
            current: ProductionRecipientRecord, **updates: Any
        ) -> ProductionRecipientRecord:
            if not persist:
                return current.model_copy(update=updates)
            if guard is None:
                raise ValueError("Placement adoption requires the current execution lease.")
            guard()
            return self.save(current, **updates)

        if record.phase == "prepared":
            return record
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        matches = [
            item
            for item in api.fetch_dokploy_projects(host=host, token=token)
            if item.get("name") == record.namespace
        ]
        if len(matches) != 1:
            raise ValueError("Original placement is absent or ambiguous; retain its original key.")
        project = cast(dict[str, Any], matches[0])
        project_id = project.get("projectId")
        if (
            not isinstance(project_id, str)
            or not project_id
            or (record.project_id and record.project_id != project_id)
        ):
            raise ValueError("Original project identity changed.")
        if not record.project_id:
            if record.phase != "create_project":
                raise ValueError("Unrequested project placement.")
            record = advance(record, project_id=project_id, phase="project_ready")
        environments = project.get("environments")
        if not isinstance(environments, list) or not all(
            isinstance(item, dict) for item in environments
        ):
            raise ValueError("Original environment inventory is incomplete.")
        candidates = [item for item in environments if item.get("name") == record.namespace]
        if not candidates and record.phase == "project_ready":
            return record
        if len(candidates) != 1:
            raise ValueError("Original environment is absent or ambiguous.")
        environment = candidates[0]
        environment_id = environment.get("environmentId")
        if (
            not isinstance(environment_id, str)
            or not environment_id
            or (record.environment_id and record.environment_id != environment_id)
        ):
            raise ValueError("Original environment identity changed.")
        if not record.environment_id:
            if record.phase != "create_environment":
                raise ValueError("Unrequested environment placement.")
            record = advance(record, environment_id=environment_id, phase="environment_ready")
        recipients = environment.get("compose")
        if not isinstance(recipients, list) or not all(
            isinstance(item, dict) for item in recipients
        ):
            raise ValueError("Original recipient inventory is incomplete.")
        candidates = [item for item in recipients if item.get("name") == record.namespace]
        if not candidates and record.phase == "environment_ready":
            return record
        if len(candidates) != 1:
            raise ValueError("Original recipient is absent or ambiguous.")
        target_id = candidates[0].get("composeId")
        if (
            not isinstance(target_id, str)
            or not target_id
            or (record.target_id and record.target_id != target_id)
        ):
            raise ValueError("Original recipient identity changed.")
        if not record.target_id:
            if record.phase != "create_recipient":
                raise ValueError("Unrequested recipient placement.")
            record = advance(record, target_id=target_id, phase="recipient_ready")
        self.unused_recipient(record)
        return record

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
            ):
                return ProviderObservation("unknown")
            self.require_unchanged(record)
            record = self.recover_placement(record, persist=False)
            if not record.target_id or record.phase == "recipient_ready":
                return ProviderObservation("absent", retry_safe=True)
            try:
                self.verify(record)
            except ValueError:
                unused = self.unused_recipient(record)
                if (
                    record.before_configuration_sha256
                    and provider_binding_digest(unused) == record.before_configuration_sha256
                ):
                    return ProviderObservation("absent", retry_safe=True)
                return ProviderObservation("unknown")
            if not record.qualified_at:
                return ProviderObservation("absent", retry_safe=True)
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
        # A reacquired reservation clears its current-attempt checkpoint. It
        # does not erase the original attempt's possible provider effects.
        started = True
        try:
            existing = custody(self.store, self.product, self.request.context)
            reservation = self.store.read_idempotency_record(
                scope=self.scope,
                route_path=ROUTE.format(product=self.product),
                idempotency_key=self.original_key,
            )
            started = bool(
                reservation is None
                or reservation.attempt > 1
                or reservation.provider_effect_started_at
                or (
                    existing
                    and any((existing.project_id, existing.environment_id, existing.target_id))
                )
            )
            record = self.current()
            if record.plan_sha256 != self.request.reviewed_plan_sha256:
                raise ValueError("Review the current production recipient plan.")
            existing = custody(self.store, self.product, record.context)
            superseding = existing is not None and record.plan_sha256 != existing.plan_sha256
            if existing is not None and not superseding:
                if (
                    reservation is None
                    or reservation.state != "running"
                    or reservation.provider_effect_started_at
                    or record.qualified_at
                ):
                    raise ValueError("Reconcile the original checkpoint before continuation.")
                if reservation.attempt > 1:
                    record = self.recover_placement(record, guard=lease.assert_current)

            def checkpoint(phase: str) -> None:
                nonlocal started
                lease.assert_current()
                self.require_unchanged(record)
                lease.checkpoint_effect(phase)
                started = True

            self.require_unchanged(record)
            lease.assert_current()
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            if existing is None or superseding:
                if any(
                    row.get("name") == record.namespace
                    for row in api.fetch_dokploy_projects(host=host, token=token)
                ):
                    raise ValueError("A preparation namespace already exists without custody.")
                self.store.write_production_recipient(
                    expected=existing.model_dump(mode="json") if existing else None,
                    replacement=record.model_dump(mode="json"),
                )
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
                storage_field = {
                    "projectId": "project_id",
                    "environmentId": "environment_id",
                    "composeId": "target_id",
                }[field]
                if getattr(record, storage_field):
                    continue
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
                        storage_field: result[field],
                        "phase": {
                            "projectId": "project_ready",
                            "environmentId": "environment_ready",
                            "composeId": "recipient_ready",
                        }[field],
                    },
                )
            fresh = self.unused_recipient(record)
            import re

            app_name = fresh.get("appName")
            if (
                not isinstance(app_name, str)
                or not re.fullmatch(re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?", app_name)
                or fresh.get("domains")
            ):
                raise ValueError("Fresh recipient identity is unverified.")
            configured = False
            if record.before_configuration_sha256:
                if provider_binding_digest(fresh) != record.before_configuration_sha256:
                    self.verify(record)
                    configured = True
            else:
                if fresh.get("env"):
                    raise ValueError("The fresh recipient has an unexpected environment.")
                record = self.save(
                    record,
                    app_name=app_name,
                    before_configuration_sha256=provider_binding_digest(fresh),
                )
            inspect_resources(self.root, record, prepare=not configured, checkpoint=checkpoint)
            if record.request.upstreams:
                from control_plane.production_recipient_host import (
                    run_recipient_program,
                    upstream_program,
                )

                values = runtime_values(self.store, self.root, record)
                rendered = qualified_render(record, values)
                binding = upstream_binding(record)
                evidence = run_recipient_program(
                    self.root,
                    record,
                    {
                        "connections": upstream_connections(record, rendered, values),
                        "image": rendered["services"][record.request.database_service]["image"],
                        "binding": binding,
                        "project": record.app_name,
                    },
                    upstream_program(),
                    checkpoint,
                )
                if (
                    evidence.get("recipient_upstreams_ok") != "true"
                    or evidence.get("recipient_upstreams_binding") != binding
                ):
                    raise ValueError("The actual destination upstream grants are not read-only.")
                if not record.upstream_proof:
                    record = self.save(
                        record,
                        upstream_proof={
                            "binding": binding,
                            "observed_at": utc_now_timestamp(),
                            "grants": "read_only",
                            "origin": "recipient_default_network",
                        },
                    )
            template = json.loads(record.compose_source)
            for volume in template["volumes"].values():
                volume["external"] = True
            if not configured:
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
            lease.assert_current()
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
