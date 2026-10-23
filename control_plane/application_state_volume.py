from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from http.client import HTTPException
import json
import re
from typing import TYPE_CHECKING, Any

import click
from uuid import uuid4

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.application_state_volume_errors import StateVolumeRefusal
from control_plane.dokploy import api, source
from control_plane.dokploy_inventory import InventoryUnavailable
from control_plane.http_routes.mutation_support import request_fingerprint
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    ProviderEvidenceLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

if TYPE_CHECKING:
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

ROUTE = "/v1/dokploy-targets/setup"
PLAN_ROUTE = ROUTE + "/state-volume-plan"
MOUNT_CREATE = "/api/mounts.create"
LANE_LOCK_PREFIX = "testing-target-setup:"


def state_volume_allocation_names(plan_id: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Storage allocation requires its service-generated plan identity.")
    return {
        "volume_name": "lp-testing-state-" + plan_id,
        "app_name": "lp-testing-app-" + plan_id + "-state",
        "project_marker": "lp-testing-project-" + plan_id,
        "environment_marker": "lp-testing-environment-" + plan_id,
        "target_marker": "lp-testing-target-" + plan_id,
    }


def state_volume_target_key(context: str) -> str:
    return f"application-state-setup:{context}:testing"


def require_no_held_state_volume(
    store: PostgresRecordStore, context: str, *, original_scope: str = "", original_key: str = ""
) -> None:
    if any(
        row.provider_target_key == state_volume_target_key(context)
        and not (
            original_scope
            and original_key
            and row.scope == original_scope
            and row.route_path == ROUTE
            and row.idempotency_key == original_key
        )
        for row in store.list_held_provider_target_reservations()
    ):
        raise ValueError(
            "Testing application setup is unresolved; preserve its original request and key."
        )


def validate_volume_request(request: "DokployTargetSetupEnvelope") -> None:
    from control_plane.contracts.application_state_path import require_application_state_path

    if request.retain_state_volume_allocation:
        if request.mode != "apply" or not re.fullmatch(
            r"[a-f0-9]{64}", request.expected_state_volume_custody_sha256
        ):
            raise ValueError("Retention requires reviewed readback custody and apply mode.")
    elif request.expected_state_volume_custody_sha256:
        raise ValueError("Custody comparison is only used for explicit allocation retention.")
    path = request.state_volume_path
    require_application_state_path(path)
    if (
        request.operation != "create-application"
        or request.instance != "testing"
        or request.app_name
        or request.target_id
        or request.project_id
        or request.environment_id
        or request.domains
        or request.custom_git_branch
        or request.source_type != "raw"
        or request.replaces_operation_id
        or not request.project_name
        or not request.environment_name
        or not request.server_id
    ):
        raise ValueError("State storage only creates a fresh isolated testing application volume.")
    if request.mode != "dry-run" and (
        not re.fullmatch(r"[a-f0-9]{32}", request.state_volume_plan_id)
        or not re.fullmatch(r"[a-f0-9]{64}", request.expected_plan_sha256)
    ):
        raise ValueError("State storage requires the saved service plan and reviewed digest.")
    if request.mode == "dry-run" and (request.state_volume_plan_id or request.expected_plan_sha256):
        raise ValueError("A new dry-run allocates its own storage plan.")


def validate_ordinary_allocation_request(request: "DokployTargetSetupEnvelope") -> None:
    if (
        request.operation != "create-application"
        or request.instance != "testing"
        or request.state_volume_path
    ):
        raise ValueError("Allocation recovery requires an ordinary testing application request.")
    if request.retain_allocation:
        if request.mode != "apply" or not re.fullmatch(
            r"[a-f0-9]{64}", request.expected_allocation_custody_sha256
        ):
            raise ValueError("Retention requires reviewed readback custody and apply mode.")
    elif request.expected_allocation_custody_sha256:
        raise ValueError("Custody comparison is only used for explicit allocation retention.")


def application_allocation_names(plan_id: str, *, state_volume: bool) -> dict[str, str]:
    names = state_volume_allocation_names(plan_id)
    if not state_volume:
        names.update(volume_name="", app_name="lp-testing-app-" + plan_id + "-empty")
    return names


def ordinary_allocation_id(scope: str, key: str) -> str:
    return request_fingerprint({"scope": scope, "route": ROUTE, "key": key})[:32]


def ordinary_allocation_names(
    request: "DokployTargetSetupEnvelope", scope: str, key: str
) -> dict[str, str]:
    names = application_allocation_names(ordinary_allocation_id(scope, key), state_volume=False)
    if not key:
        names.update(
            project_marker="<service-generated-project>",
            environment_marker="<service-generated-environment>",
            target_marker="<service-generated-target>",
            app_name="<service-generated-application>",
        )
    if request.project_id or request.environment_id:
        names["project_marker"] = request.project_name
    if request.environment_id:
        names["environment_marker"] = request.environment_name
    return names


def named_application_request(
    request: "DokployTargetSetupEnvelope", names: dict[str, Any]
) -> "DokployTargetSetupEnvelope":
    return request.model_copy(
        update={
            "app_name": names["app_name"],
            "project_name": names["project_marker"],
            "environment_name": names["environment_marker"],
            "target_name": names["target_marker"],
        }
    )


def _normalized(request: "DokployTargetSetupEnvelope") -> dict[str, Any]:
    return request.model_dump(
        mode="json",
        exclude={
            "mode",
            "retain_allocation",
            "expected_allocation_custody_sha256",
            "confirmation",
            "reason",
            "state_volume_plan_id",
            "expected_plan_sha256",
            "retain_state_volume_allocation",
            "expected_state_volume_custody_sha256",
        },
    )


def _binding(store: PostgresRecordStore, context: str) -> str:
    # Hash, rather than copy, record contents that can include runtime inputs.
    profiles = [
        p.model_dump(mode="json")
        for p in store.list_product_profile_records()
        if context in {lane.context for lane in p.lanes} or context in p.historical_contexts
    ]
    records: list[Any] = []
    for read in (
        store.read_dokploy_target_record,
        store.read_dokploy_target_id_record,
        store.read_provider_target_record,
    ):
        try:
            records.append(
                read(context_name=context, instance_name="testing").model_dump(mode="json")
            )
        except FileNotFoundError:
            records.append(None)
    return request_fingerprint({"profiles": profiles, "records": records})


@dataclass
class ApplicationStateVolumeAdapter:
    root: Path
    store: PostgresRecordStore
    request: "DokployTargetSetupEnvelope"
    scope: str
    trace: str
    product: str
    intent: dict[str, Any]
    operation_key: str = ""
    first_testing_setup: bool = False

    @classmethod
    def completed_for_testing_lane(
        cls,
        root: Path,
        store: PostgresRecordStore,
        *,
        product: str,
        context: str,
        scope: str,
        trace: str,
    ) -> "ApplicationStateVolumeAdapter":
        """Resolve this caller's original allocation from its service-owned markers."""
        from control_plane.application_state_volume_retention import (
            state_volume_progress_operation_key,
        )
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        marker = re.fullmatch(r"lp-testing-project-([a-f0-9]{32})", target.project_name)
        if target.target_type != "application" or marker is None:
            raise ValueError("Testing continuation requires an original application allocation.")
        plan_id = marker[1]
        plan = store.read_idempotency_record(
            scope=scope, route_path=PLAN_ROUTE, idempotency_key=plan_id
        )
        if plan is None or plan.state != "completed":
            raise ValueError(
                "Original application allocation belongs to another caller or is absent."
            )
        intent = plan.response_payload
        if (
            intent.get("product") != product
            or intent.get("plan_id") != plan_id
            or not isinstance(intent.get("request"), dict)
            or not isinstance(intent.get("plan_sha256"), str)
            or any(
                intent.get(key) != value
                for key, value in state_volume_allocation_names(plan_id).items()
            )
        ):
            raise ValueError("Original application allocation identity differs from this lane.")
        request = DokployTargetSetupEnvelope.model_validate(
            {
                **intent["request"],
                "mode": "readback",
                "state_volume_plan_id": plan_id,
                "expected_plan_sha256": intent["plan_sha256"],
            }
        )
        if request.context != context:
            raise ValueError("Original application allocation belongs to another lane.")
        key = state_volume_progress_operation_key(store, scope, plan_id)
        adapter = cls.prepare(root, store, request, scope, trace, product, key)
        receipt = store.read_idempotency_record(scope=scope, route_path=ROUTE, idempotency_key=key)
        if (
            receipt is None
            or receipt.state != "completed"
            or receipt.provider_target_key != adapter.target_key()
            or receipt.reconciliation_key != adapter.reconciliation_key()
            or receipt.request_fingerprint != adapter.reconciliation_key()
            or store.read_original_mutation_intent(receipt) != intent
        ):
            raise ValueError("Original application allocation has no completed matching receipt.")
        result = receipt.response_payload.get("result", {})
        if (
            not isinstance(result, dict)
            or result.get("allocation_state") != "adopted"
            or result.get("applied") is not True
            or result.get("deployment_performed") is not False
        ):
            raise ValueError("Original application allocation was not completed and adopted.")
        return adapter

    def qualify_testing_continuation(self, *, inspect_provider: bool) -> str:
        """Verify the adopted allocation; history alone never attests its current state."""
        from control_plane.application_state_volume_retention import require_state_volume_owner

        require_state_volume_owner(self.store, product=self.product, context=self.request.context)
        phase = self._progress_phase()
        data = json.loads(phase)
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str) and data[key]
            for key in ("application", "environment", "project", "app_name")
        ):
            raise ValueError("Original application allocation progress is incomplete.")
        self._verify_adopted(phase, configuration_verified=False)
        if inspect_provider:
            from control_plane.dokploy.application_domains import (
                require_private_application_domains,
            )
            from control_plane.dokploy.fresh_application import require_fresh_application_runtime

            live = self._live(data["application"], data["environment"], data["app_name"])
            require_fresh_application_runtime(
                live, environment_id=data["environment"], project_id=data["project"]
            )
            target = self.store.read_dokploy_target_record(
                context_name=self.request.context, instance_name="testing"
            )
            profile = self.store.read_product_profile_record(self.product)
            require_private_application_domains(
                live.get("domains"),
                hostname=target.domains[0] if len(target.domains) == 1 else None,
                runtime_port=profile.runtime_port,
                application_id=data["application"],
            )
            if not self._configuration_matches(live):
                raise ValueError("Application source, execution, storage or placement changed.")
        return request_fingerprint(
            {
                "intent": self.intent,
                "phase": phase,
                "binding": _binding(self.store, self.request.context),
            }
        )

    @classmethod
    def prepare(
        cls,
        root: Path,
        store: PostgresRecordStore,
        request: "DokployTargetSetupEnvelope",
        scope: str,
        trace: str,
        product: str,
        operation_key: str = "",
        *,
        first_testing_setup: bool = False,
    ) -> "ApplicationStateVolumeAdapter":
        from control_plane.dokploy_target_setup_http import require_empty_testing_target_creation

        if not request.state_volume_path:
            return cls.prepare_ordinary(
                root,
                store,
                request,
                scope,
                trace,
                product,
                operation_key,
                first_testing_setup=first_testing_setup,
            )
        validate_volume_request(request)
        if request.mode == "dry-run":
            require_empty_testing_target_creation(
                store, request, product=product, control_plane_root_path=root
            )
            if store.read_product_profile_record(product).driver_id != "generic-web":
                raise ValueError("Application state storage requires a generic-web product.")
            allocation = uuid4().hex
            host, _token = source.read_dokploy_config(control_plane_root=root)
            intent: dict[str, Any] = {
                "request": _normalized(request),
                "product": product,
                "binding": _binding(store, request.context),
                "provider_host": host,
                "plan_id": allocation,
                **state_volume_allocation_names(allocation),
            }
            intent["plan_sha256"] = request_fingerprint(intent)
            store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id="application-state-plan-" + allocation,
                    scope=scope,
                    route_path=PLAN_ROUTE,
                    idempotency_key=allocation,
                    request_fingerprint=intent["plan_sha256"],
                    response_status_code=202,
                    response_trace_id=trace,
                    recorded_at=utc_now_timestamp(),
                    response_payload=intent,
                )
            )
        else:
            plan = store.read_idempotency_record(
                scope=scope, route_path=PLAN_ROUTE, idempotency_key=request.state_volume_plan_id
            )
            if plan is None:
                raise ValueError("Original storage plan is unavailable to this caller.")
            intent = plan.response_payload
            if (
                intent["request"] != _normalized(request)
                or intent["plan_sha256"] != request.expected_plan_sha256
            ):
                raise ValueError("Storage plan differs from the reviewed request.")
        return cls(
            root, store, request, scope, trace, str(intent["product"]), intent, operation_key
        )

    @classmethod
    def prepare_ordinary(
        cls,
        root: Path,
        store: PostgresRecordStore,
        request: "DokployTargetSetupEnvelope",
        scope: str,
        trace: str,
        product: str,
        operation_key: str,
        *,
        first_testing_setup: bool,
    ) -> "ApplicationStateVolumeAdapter":
        validate_ordinary_allocation_request(request)
        if not operation_key:
            raise ValueError("Allocation recovery requires the original operation key.")
        plan_id = ordinary_allocation_id(scope, operation_key)
        plan = store.read_idempotency_record(
            scope=scope, route_path=PLAN_ROUTE, idempotency_key=plan_id
        )
        if plan is None:
            if request.mode == "readback" or request.retain_allocation:
                raise ValueError("Original allocation is unavailable to this caller.")
            host, _token = source.read_dokploy_config(control_plane_root=root)
            intent: dict[str, Any] = {
                "request": _normalized(request),
                "product": product,
                "binding": _binding(store, request.context),
                "provider_host": host,
                "plan_id": plan_id,
                "ordinary": True,
                "first_testing_setup": first_testing_setup,
                **ordinary_allocation_names(request, scope, operation_key),
            }
            intent["plan_sha256"] = request_fingerprint(intent)
            adapter = cls(
                root,
                store,
                request,
                scope,
                trace,
                product,
                intent,
                operation_key,
                first_testing_setup,
            )
            adapter.checkpoint()
            store.write_idempotency_record_once(
                LaunchplaneIdempotencyRecord(
                    record_id="application-allocation-plan-" + plan_id,
                    scope=scope,
                    route_path=PLAN_ROUTE,
                    idempotency_key=plan_id,
                    request_fingerprint=intent["plan_sha256"],
                    response_status_code=202,
                    response_trace_id=trace,
                    recorded_at=utc_now_timestamp(),
                    response_payload=intent,
                )
            )
            plan = store.read_idempotency_record(
                scope=scope, route_path=PLAN_ROUTE, idempotency_key=plan_id
            )
        if plan is None or plan.response_payload.get("ordinary") is not True:
            raise ValueError("Original allocation intent is unavailable.")
        intent = plan.response_payload
        if intent["request"] != _normalized(request):
            raise ValueError("Preserve the original allocation request and operation key.")
        return cls(
            root,
            store,
            request,
            scope,
            trace,
            str(intent["product"]),
            intent,
            operation_key,
            first_testing_setup,
        )

    def _configuration_checkpoint(self) -> str:
        return "mount_verified" if self.request.state_volume_path else "application_verified"

    def target_key(self) -> str:
        return state_volume_target_key(self.request.context)

    def reconciliation_key(self) -> str:
        return str(self.intent["plan_sha256"])

    def plan_result(self) -> dict[str, object]:
        result: dict[str, object] = {
            "operation": "create-application",
            "context": self.request.context,
            "instance": "testing",
            "applied": False,
            "deployment_performed": False,
            "target": {
                key: self.intent["request"][key]
                for key in (
                    "context",
                    "instance",
                    "target_name",
                    "project_name",
                    "environment_name",
                    "server_id",
                    "expected_current_provider_target",
                )
            },
            "allocation": {
                "project_name": self.intent["project_marker"],
                "environment_name": self.intent["environment_marker"],
                "target_name": self.intent["target_marker"],
                "app_name": self.intent["app_name"],
            },
            "state_volume_plan_id": self.intent["plan_id"],
            "plan_sha256": self.intent["plan_sha256"],
            "state_volume": {
                "name": self.intent["volume_name"],
                "mount_path": self.request.state_volume_path,
            },
        }
        if not self.request.state_volume_path:
            result.pop("state_volume")
            result.pop("state_volume_plan_id")
            result["allocation_id"] = self.intent["plan_id"]
        return result

    def checkpoint(self, *, read_provider_source: bool = True) -> None:
        from control_plane.dokploy_target_setup_http import require_empty_testing_target_creation
        from control_plane.application_state_volume_retention import require_state_volume_owner

        if (
            self.request.state_volume_path
            or self.first_testing_setup
            or self.intent.get("first_testing_setup")
        ):
            require_state_volume_owner(
                self.store, product=self.product, context=self.request.context
            )
            require_empty_testing_target_creation(
                self.store,
                self.request,
                product=self.product,
                control_plane_root_path=self.root,
                read_provider_source=read_provider_source,
                allocation_scope=self.scope if not self.request.state_volume_path else "",
                allocation_key=self.operation_key if not self.request.state_volume_path else "",
            )
        else:
            from control_plane.dokploy_target_setup_http import (
                ensure_dokploy_target_setup_context_is_not_historical,
            )

            ensure_dokploy_target_setup_context_is_not_historical(
                record_store=self.store, context=self.request.context
            )
            require_no_held_state_volume(
                self.store,
                self.request.context,
                original_scope=self.scope,
                original_key=self.operation_key,
            )
        host, _token = source.read_dokploy_config(control_plane_root=self.root)
        if (
            host != self.intent["provider_host"]
            or _binding(self.store, self.request.context) != self.intent["binding"]
        ):
            raise ValueError(
                "Reviewed testing target or provider changed; allocation intent is stale."
            )

    def authority_checkpoint(self) -> None:
        # The live source was checked immediately before entering the guard.
        # This callback runs under the global authority lock; keep network I/O outside it.
        self.checkpoint(read_provider_source=False)

    def _application_name(self, value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            re.escape(self.intent["app_name"]) + r"(?:-[a-z0-9]{6})?", value
        ):
            raise ValueError("Provider application name does not belong to this allocation.")
        return value

    def _live(self, application_id: str, environment_id: str, app_name: str) -> api.JsonObject:
        from control_plane.dokploy_target_setup_http import fetch_dokploy_target_payload_for_setup

        host, token = source.read_dokploy_config(control_plane_root=self.root)
        self._application_name(app_name)
        if host != self.intent["provider_host"]:
            raise InventoryUnavailable("provider_binding_changed")
        for other in self.store.list_physical_provider_target_records():
            if other.target_id == application_id and (other.context, other.instance) != (
                self.request.context,
                "testing",
            ):
                raise ValueError("Application is aliased by another lane.")
        live = fetch_dokploy_target_payload_for_setup(host, token, "application", application_id)
        if live.get("applicationId") != application_id:
            raise InventoryUnavailable("target_identity_unavailable")
        if any(
            live.get(key) != value
            for key, value in {
                "applicationId": application_id,
                "environmentId": environment_id,
                "appName": app_name,
            }.items()
        ):
            raise ValueError("New application identity or placement differs from the plan.")
        if self.request.server_id and live.get("serverId") != self.request.server_id:
            raise ValueError("New application server differs from the plan.")
        return live

    def _configuration_matches(self, live: api.JsonObject) -> bool:
        if not self.request.state_volume_path:
            from control_plane.dokploy.target_source_setup import require_empty_target_source

            try:
                require_empty_target_source(live)
            except ValueError:
                return False
            return (
                live.get("mounts") == []
                and live.get("deployments") == []
                and not live.get("domains")
                and live.get("applicationStatus") in {None, "idle"}
            )
        mounts = live.get("mounts")
        if not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
            return False
        mount = mounts[0]
        return (
            mount.get("type") == "volume"
            and mount.get("volumeName") == self.intent["volume_name"]
            and mount.get("mountPath") == self.request.state_volume_path
            and mount.get("applicationId") == live["applicationId"]
            and not any(
                mount.get(key)
                for key in (
                    "hostPath",
                    "filePath",
                    "content",
                    "composeId",
                    "postgresId",
                    "mysqlId",
                    "mariadbId",
                    "mongoId",
                    "redisId",
                    "libsqlId",
                )
            )
            and bool(mount.get("mountId"))
        )

    def _verify_adopted(
        self, phase: str, *, configuration_verified: bool = True
    ) -> dict[str, object]:
        from control_plane.dokploy_target_setup_http import (
            dokploy_target_setup_result_payload,
        )

        data = json.loads(phase)
        if not configuration_verified and data.get(self._configuration_checkpoint()) != "true":
            raise ValueError(
                "Original allocation has no durable verified-configuration checkpoint."
            )
        application_id, environment_id = data["application"], data["environment"]
        if configuration_verified:
            live = self._live(application_id, environment_id, data["app_name"])
            if not self._configuration_matches(live):
                raise ValueError(
                    "Original volume effect is incomplete or changed; never repeat creation."
                )
        target = self.store.read_dokploy_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.request.context, instance_name="testing"
        )
        current = self.store.read_provider_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.workflows.dokploy_target_adoption import DokployTargetAdoptionResult

        if (
            target.target_type != "application"
            or (
                self.intent["project_marker"]
                and target.project_name != self.intent["project_marker"]
            )
            or target.target_name != self.intent["target_marker"]
            or target_id.target_id != application_id
            or current.to_deployed_target_reference()
            != ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            ).to_deployed_target_reference()
        ):
            raise ValueError(
                "Created application binding changed; keep original recovery evidence."
            )
        setup = dokploy_target_setup_result_payload(
            DokployTargetAdoptionResult(
                applied=True,
                target_record=target,
                target_id_record=target_id,
                provider_target_record=current,
            )
        )
        if not self.request.state_volume_path:
            from control_plane.workflows.dokploy_target_adoption import (
                DokployTargetCreateResult,
                plan_dokploy_application_target,
                planned_application_provider_requests,
            )

            named = named_application_request(self.request, self.intent)
            creation = dokploy_target_setup_result_payload(
                DokployTargetCreateResult(
                    applied=True,
                    plan=plan_dokploy_application_target(
                        project_id=named.project_id,
                        project_name=named.project_name,
                        environment_id=named.environment_id,
                        environment_name=named.environment_name or "testing",
                        target_name=named.target_name,
                        app_name=named.app_name,
                        server_id=named.server_id,
                    ),
                    project_id=data.get("project", self.request.project_id),
                    environment_id=environment_id,
                    target_record=target,
                    target_id_record=target_id,
                    provider_target_record=current,
                    provider_requests=planned_application_provider_requests(
                        project_id=named.project_id,
                        project_name=named.project_name,
                        project_description=named.project_description,
                        environment_id=named.environment_id,
                        environment_name=named.environment_name or "testing",
                        environment_description=named.environment_description,
                        target_name=named.target_name,
                        app_name=named.app_name,
                        application_description=named.description,
                        server_id=named.server_id,
                    ),
                )
            )
            creation["allocation"] = {
                **self.plan_result(),
                "configuration_matches": configuration_verified,
                "configuration_evidence": "matches" if configuration_verified else "unavailable",
                "allocation_state": "adopted",
            }
            return {
                "mode": "apply",
                "operation": self.request.operation,
                "context": self.request.context,
                "instance": self.request.instance,
                "applied": True,
                "route_domain_ids": [],
                "reason": self.request.reason,
                "setup": creation,
            }
        return {
            **self.plan_result(),
            "applied": True,
            "configuration_matches": configuration_verified,
            "configuration_evidence": "matches" if configuration_verified else "unavailable",
            "allocation_state": "adopted",
            "setup": setup,
        }

    def _progress_phase(self, fallback: str = "") -> str:
        from control_plane.application_state_volume_retention import read_state_volume_progress

        return (
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]) or fallback
        )

    def _checkpoint_phase(self, lease: ProviderOperationLease, phase: str) -> None:
        from control_plane.application_state_volume_retention import (
            checkpoint_state_volume_progress,
        )

        if not isinstance(lease, ProviderEvidenceLease):
            raise ValueError("Storage progress requires a fenced provider evidence lease.")
        lease.checkpoint_effect(phase)
        checkpoint_state_volume_progress(
            self.store,
            reservation=lease.evidence_reservation(),
            plan_id=self.intent["plan_id"],
            phase=phase,
        )

    def _resume_phase(self, phase: str) -> dict[str, str]:
        """Recover positive placement evidence without ever repeating an unknown create."""
        from control_plane.dokploy_inventory import MAX_PROJECTS, read_dokploy_inventory
        from control_plane.workflows.dokploy_target_adoption import (
            APPLICATION_CREATE_PATH,
            ENVIRONMENT_CREATE_PATH,
            PROJECT_CREATE_PATH,
        )

        data: dict[str, str] = json.loads(phase) if phase else {}
        if not data:
            return {
                key: value
                for key, value in {
                    "project": self.request.project_id if not self.request.environment_id else "",
                    "environment": self.request.environment_id,
                }.items()
                if value
            }
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        if host != self.intent["provider_host"]:
            raise ValueError("Provider binding changed.")
        inventory = read_dokploy_inventory(host=host, token=token, offset=0, limit=MAX_PROJECTS)
        if inventory.next_offset is not None or len(inventory.projects) != inventory.total_projects:
            raise ValueError("Original placement inventory is incomplete.")
        if self.request.environment_id:
            projects = [
                p
                for p in inventory.projects
                if any(e.environment_id == self.request.environment_id for e in p.environments)
            ]
        elif self.request.project_id:
            projects = [p for p in inventory.projects if p.project_id == self.request.project_id]
        else:
            projects = [p for p in inventory.projects if p.name == self.intent["project_marker"]]
        if len(projects) != 1 or (
            data.get("project") and data["project"] != projects[0].project_id
        ):
            raise ValueError("Original project is absent, changed or ambiguous; retain custody.")
        project = projects[0]
        if (
            not data.get("project")
            and not self.request.environment_id
            and data.get("pending") != PROJECT_CREATE_PATH
        ):
            raise ValueError("Unrequested project placement.")
        data["project"] = project.project_id
        if data.get("pending") == PROJECT_CREATE_PATH:
            data.pop("pending")
        environments = [
            e
            for e in project.environments
            if (
                e.environment_id == self.request.environment_id
                if self.request.environment_id
                else e.name == self.intent["environment_marker"]
            )
        ]
        if not data.get("environment") and data.get("pending") is None:
            return data
        if len(environments) != 1 or (
            data.get("environment") and data["environment"] != environments[0].environment_id
        ):
            raise ValueError(
                "Original environment is absent, changed or ambiguous; retain custody."
            )
        environment = environments[0]
        if not data.get("environment") and data.get("pending") != ENVIRONMENT_CREATE_PATH:
            raise ValueError("Unrequested environment placement.")
        data["environment"] = environment.environment_id
        if data.get("pending") == ENVIRONMENT_CREATE_PATH:
            data.pop("pending")
        if not data.get("application") and data.get("pending") is None:
            return data
        targets = [
            t
            for t in environment.targets
            if t.category == "application" and t.name == self.intent["target_marker"]
        ]
        if len(targets) != 1 or (
            data.get("application") and data["application"] != targets[0].target_id
        ):
            raise ValueError(
                "Original application is absent, changed or ambiguous; retain custody."
            )
        if not data.get("application") and data.get("pending") != APPLICATION_CREATE_PATH:
            raise ValueError("Unrequested application placement.")
        data["application"] = targets[0].target_id
        from control_plane.dokploy_target_setup_http import fetch_dokploy_target_payload_for_setup

        raw = fetch_dokploy_target_payload_for_setup(
            host, token, "application", data["application"]
        )
        app_name = self._application_name(raw.get("appName"))
        if data.get("app_name") and data["app_name"] != app_name:
            raise ValueError("Original application name changed.")
        data["app_name"] = app_name
        live = self._live(data["application"], data["environment"], app_name)
        if (
            data.get("pending") == MOUNT_CREATE
            or data.get(self._configuration_checkpoint()) == "true"
        ):
            if not self._configuration_matches(live):
                raise ValueError("Original mount effect is uncertain or changed; retain custody.")
            # Positive recovery promotes the dispatched effect permanently.
            # Clearing pending must never make it eligible for another create.
            data[self._configuration_checkpoint()] = "true"
        if data.get("pending") not in {None, APPLICATION_CREATE_PATH, MOUNT_CREATE}:
            raise ValueError("Unknown original provider phase.")
        data.pop("pending", None)
        return data

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        phase = self._progress_phase(provider_effect_phase)
        try:
            # Adoption committed after positive mount readback. Finish that
            # durable receipt even if provider configuration has since changed;
            # this does not claim the current mount is still correct.
            result = self._verify_adopted(phase, configuration_verified=False)
        except (ValueError, KeyError, FileNotFoundError):
            pass
        else:
            return ProviderObservation(
                "present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace, records={}, result=result
                ),
            )
        try:
            self._resume_phase(phase)
        except Exception:
            return ProviderObservation("unknown")
        # A renewed lease performs all writes and adoption; observation only proves
        # that apply can skip positively recovered creates.
        return ProviderObservation("absent", retry_safe=True)

    def readback(self, key: str) -> dict[str, object]:
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=ROUTE, idempotency_key=key
        )
        if row is None or row.reconciliation_key != self.reconciliation_key():
            raise ValueError("Readback requires the original operation key and plan.")
        from control_plane.application_state_volume_retention import state_volume_custody_sha256

        phase = self._progress_phase(row.provider_effect_phase)
        # Readback neither adopts records nor repeats provider writes.
        evidence = "unavailable"
        try:
            data = json.loads(phase) if phase else {}
            if all(data.get(key) for key in ("application", "environment", "app_name")):
                live = self._live(data["application"], data["environment"], data["app_name"])
                evidence = "matches" if self._configuration_matches(live) else "differs"
        except (
            click.ClickException,
            OSError,
            HTTPException,
            InventoryUnavailable,
            json.JSONDecodeError,
        ):
            evidence = "unavailable"
        except (ValueError, KeyError):
            evidence = "differs"
        return {
            **self.plan_result(),
            "operation_state": row.state,
            "provider_effect_phase": phase,
            "configuration_matches": evidence == "matches",
            "configuration_evidence": evidence,
            "custody_sha256": state_volume_custody_sha256(self.store, row),
            "allocation_state": row.response_payload.get("result", {}).get(
                "allocation_state", row.state
            ),
        }

    def retain(self, key: str) -> dict[str, object]:
        from control_plane.application_state_volume_retention import retain_state_volume_allocation

        if (
            self.intent.get("ordinary") is True
            and self.intent.get("first_testing_setup") is False
            and self.first_testing_setup
        ):
            raise ValueError(
                "Original service allocation retention requires current service authority."
            )

        with self.store.testing_replacement_execution_lock(
            LANE_LOCK_PREFIX + self.request.context
        ) as check:
            if check is None:
                raise StateVolumeRefusal(
                    "application_state_volume_lane_busy",
                    "Testing lane setup is busy; retry the original operation.",
                )
            row = self.store.read_idempotency_record(
                scope=self.scope, route_path=ROUTE, idempotency_key=key
            )
            if row is None or row.reconciliation_key != self.reconciliation_key():
                raise ValueError("Retention requires the original operation key and plan.")

            def checkpoint() -> None:
                from control_plane.application_state_volume_retention import (
                    require_state_volume_retention_eligibility,
                )

                check()
                phase = self._progress_phase(row.provider_effect_phase)
                allocation = json.loads(phase) if phase else {}
                require_state_volume_retention_eligibility(
                    self.store,
                    product=self.product,
                    context=self.request.context,
                    application_id=allocation.get("application", ""),
                    require_exclusive_owner=not (
                        self.intent.get("ordinary") is True
                        and self.intent.get("first_testing_setup") is False
                        and not self.first_testing_setup
                    ),
                )

            return retain_state_volume_allocation(
                self.store,
                reservation=row,
                expected_custody_sha256=(
                    self.request.expected_state_volume_custody_sha256
                    if self.request.state_volume_path
                    else self.request.expected_allocation_custody_sha256
                ),
                expected_original_intent=self.intent,
                trace=self.trace,
                checkpoint=checkpoint,
            )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        from control_plane.dokploy_target_setup_http import (
            TestingTargetSourceEvidenceUnavailable,
            execute_dokploy_target_setup,
            first_testing_replacement_lock_name,
            mutate_dokploy_payload_for_target_setup,
        )
        from control_plane.workflows.dokploy_target_adoption import (
            APPLICATION_CREATE_PATH,
            PROJECT_CREATE_PATH,
            ENVIRONMENT_CREATE_PATH,
            _extract_provider_id,
        )

        from control_plane.application_state_volume_retention import (
            state_volume_plan_is_retained,
            state_volume_progress_operation_key,
        )

        allocation_key = state_volume_progress_operation_key(
            self.store, self.scope, self.intent["plan_id"]
        )
        if allocation_key and allocation_key != self.operation_key:
            raise ProviderMutationRejectedError(
                ValueError(
                    "Preserve this allocation's original operation key; review a fresh plan for new work."
                )
            )
        if state_volume_plan_is_retained(self.store, self.scope, self.intent["plan_id"]):
            raise ProviderMutationRejectedError(
                ValueError("This allocation is retained; review a fresh storage plan.")
            )
        if not isinstance(lease, ProviderEvidenceLease):
            raise ProviderMutationRejectedError(
                ValueError("Application adoption requires its live evidence lease.")
            )
        started = False
        original = self.store.read_idempotency_record(
            scope=self.scope, route_path=ROUTE, idempotency_key=self.operation_key
        )
        phase = self._progress_phase(original.provider_effect_phase if original else "")
        created: dict[str, str] = {}
        execution_check: Callable[[], None]

        def mutate(host: str, token: str, path: str, payload: api.JsonObject) -> api.JsonObject:
            nonlocal started
            execution_check()
            self.checkpoint()
            lease.assert_current()
            key, field = {
                PROJECT_CREATE_PATH: ("project", "projectId"),
                ENVIRONMENT_CREATE_PATH: ("environment", "environmentId"),
                APPLICATION_CREATE_PATH: ("application", "applicationId"),
            }[path]
            if key in created:
                response: api.JsonObject = {field: created[key]}
                if key == "application":
                    response["appName"] = created["app_name"]
            else:
                self._checkpoint_phase(
                    lease, json.dumps({**created, "pending": path}, sort_keys=True)
                )
                started = True
                response = mutate_dokploy_payload_for_target_setup(host, token, path, payload)
            value = _extract_provider_id(response, field, key)
            created[key] = value
            default_environment = api.as_json_object(response.get("environment"))
            if path == PROJECT_CREATE_PATH and default_environment:
                default_id = default_environment.get("environmentId")
                if isinstance(default_id, str) and default_id:
                    created["default_environment"] = default_id
            application_fields = api.as_json_object(response.get("application")) or response
            if path == APPLICATION_CREATE_PATH and isinstance(
                application_fields.get("appName"), str
            ):
                created["app_name"] = str(application_fields["appName"])
            # Retain every acknowledged identity before any post-create validation
            # or provider read can fail, including identities of refused resources.
            self._checkpoint_phase(lease, json.dumps(created, sort_keys=True))
            if path == APPLICATION_CREATE_PATH:
                # Dokploy appends a generated suffix to the requested base name.
                # Retain its exact returned identity before dispatching the mount.
                app_name = self._application_name(application_fields.get("appName"))
                created["app_name"] = app_name
                self.checkpoint()
                live = self._live(value, created["environment"], app_name)
                if self._configuration_matches(live):
                    created[self._configuration_checkpoint()] = "true"
                    self._checkpoint_phase(lease, json.dumps(created, sort_keys=True))
                    return response
                if not self.request.state_volume_path:
                    raise ValueError(
                        "Original empty application configuration changed; retain custody."
                    )
                if created.get(self._configuration_checkpoint()) == "true":
                    raise ValueError("Original verified mount changed; never repeat creation.")
                if (
                    live.get("mounts") != []
                    or live.get("deployments") != []
                    or live.get("applicationStatus") not in {None, "idle"}
                    or live.get("domains")
                    or live.get("dockerImage")
                ):
                    raise ValueError(
                        "A new state mount requires a provably undeployed application."
                    )
                execution_check()
                self.checkpoint()
                self._checkpoint_phase(
                    lease, json.dumps({**created, "pending": MOUNT_CREATE}, sort_keys=True)
                )
                mutate_dokploy_payload_for_target_setup(
                    host,
                    token,
                    MOUNT_CREATE,
                    {
                        "type": "volume",
                        "volumeName": self.intent["volume_name"],
                        "mountPath": self.request.state_volume_path,
                        "serviceType": "application",
                        "serviceId": value,
                    },
                )
                if not self._configuration_matches(
                    self._live(value, created["environment"], app_name)
                ):
                    raise ValueError("Independent state-volume readback differs.")
                created[self._configuration_checkpoint()] = "true"
                self._checkpoint_phase(lease, json.dumps(created, sort_keys=True))
                execution_check()
                self.checkpoint()
            return response

        try:
            lock_name = first_testing_replacement_lock_name(self.store, self.request)
            with (
                self.store.testing_replacement_execution_lock(
                    LANE_LOCK_PREFIX + self.request.context
                ) as lane_check,
                (
                    self.store.testing_replacement_execution_lock(lock_name)
                    if lock_name
                    else nullcontext(lambda: None)
                ) as check,
            ):
                if check is None or lane_check is None:
                    raise StateVolumeRefusal(
                        "application_state_volume_lane_busy", "Testing executor is busy."
                    )

                def execution_check() -> None:
                    check()
                    lane_check()

                execution_check()
                created = self._resume_phase(phase)
                started = bool(phase)
                lease.assert_current()
                if phase:
                    self._checkpoint_phase(lease, json.dumps(created, sort_keys=True))
                try:
                    current = self.store.read_provider_target_record(
                        context_name=self.request.context, instance_name="testing"
                    )
                except FileNotFoundError:
                    current = None
                if (
                    created.get("application")
                    and current is not None
                    and current.target_id == created["application"]
                ):
                    result = self._verify_adopted(json.dumps(created))
                    return ProviderMutationOutcome(
                        202,
                        provider_operation_response_payload(
                            trace_id=self.trace, records={}, result=result
                        ),
                    )
                self.checkpoint()
                with self.store.provider_evidence_guard(
                    lease.evidence_reservation, authority_checkpoint=self.authority_checkpoint
                ):
                    execute_dokploy_target_setup(
                        control_plane_root_path=self.root,
                        record_store=self.store,
                        request=named_application_request(self.request, self.intent),
                        lane_lock_held=True,
                        required_context_owner=(self.product, self.request.context)
                        if self.product
                        else None,
                        application_mutate=mutate,
                    )
                lease.assert_current()
                result = self._verify_adopted(json.dumps(created))
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace, records={}, result=result
                    ),
                )
        except Exception as error:
            if not started and not phase:
                raise ProviderMutationRejectedError(
                    error
                    if isinstance(
                        error, StateVolumeRefusal | TestingTargetSourceEvidenceUnavailable
                    )
                    or (not self.request.state_volume_path and isinstance(error, ValueError))
                    else ValueError("Testing application allocation refused before effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Creation may have taken effect; preserve the original request, key and allocation receipt for readback/recovery."
            ) from error
