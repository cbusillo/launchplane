"""Reviewed new application storage; no caller-selected volumes or existing mounts."""

from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import json
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.dokploy import api, source
from control_plane.http_routes.mutation_support import request_fingerprint
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

if TYPE_CHECKING:
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

ROUTE = "/v1/dokploy-targets/setup"
PLAN_ROUTE = ROUTE + "/state-volume-plan"
MOUNT_CREATE = "/api/mounts.create"


def state_volume_target_key(context: str) -> str:
    return f"application-state-setup:{context}:testing"


def require_no_held_state_volume(store: PostgresRecordStore, context: str) -> None:
    if any(
        row.provider_target_key == state_volume_target_key(context)
        for row in store.list_held_provider_target_reservations()
    ):
        raise ValueError("Testing storage setup is unresolved; preserve its original key and plan.")


def validate_volume_request(request: "DokployTargetSetupEnvelope") -> None:
    path = request.state_volume_path
    if (
        request.operation != "create-application"
        or request.instance != "testing"
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path)
        or str(PurePosixPath(path)) != path
        or ".." in PurePosixPath(path).parts
        or path in {"/", "/proc", "/sys", "/dev", "/etc", "/run"}
        or any(
            path.startswith(prefix + "/") for prefix in ("/proc", "/sys", "/dev", "/etc", "/run")
        )
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


def _normalized(request: "DokployTargetSetupEnvelope") -> dict[str, Any]:
    return request.model_dump(
        mode="json",
        exclude={"mode", "confirmation", "reason", "state_volume_plan_id", "expected_plan_sha256"},
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

    @classmethod
    def prepare(
        cls,
        root: Path,
        store: PostgresRecordStore,
        request: "DokployTargetSetupEnvelope",
        scope: str,
        trace: str,
        product: str,
    ) -> "ApplicationStateVolumeAdapter":
        from control_plane.dokploy_target_setup_http import require_empty_testing_target_creation

        validate_volume_request(request)
        if request.mode == "dry-run":
            require_empty_testing_target_creation(store, request, product=product)
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
                "volume_name": "lp-testing-state-" + allocation,
                "app_name": "lp-testing-app-" + allocation + "-state",
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
                or intent["product"] != product
                or intent["plan_sha256"] != request.expected_plan_sha256
            ):
                raise ValueError("Storage plan differs from the reviewed request.")
        return cls(root, store, request, scope, trace, product, intent)

    def target_key(self) -> str:
        return state_volume_target_key(self.request.context)

    def reconciliation_key(self) -> str:
        return str(self.intent["plan_sha256"])

    def plan_result(self) -> dict[str, object]:
        return {
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
            "state_volume_plan_id": self.intent["plan_id"],
            "plan_sha256": self.intent["plan_sha256"],
            "state_volume": {
                "name": self.intent["volume_name"],
                "mount_path": self.request.state_volume_path,
            },
        }

    def checkpoint(self) -> None:
        from control_plane.dokploy_target_setup_http import require_empty_testing_target_creation

        require_empty_testing_target_creation(self.store, self.request, product=self.product)
        host, _token = source.read_dokploy_config(control_plane_root=self.root)
        if (
            host != self.intent["provider_host"]
            or _binding(self.store, self.request.context) != self.intent["binding"]
        ):
            raise ValueError("Reviewed testing target or provider changed; storage plan is stale.")

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
            raise ValueError("Provider binding changed.")
        for other in self.store.list_physical_provider_target_records():
            if other.target_id == application_id and (other.context, other.instance) != (
                self.request.context,
                "testing",
            ):
                raise ValueError("Application is aliased by another lane.")
        live = fetch_dokploy_target_payload_for_setup(host, token, "application", application_id)
        if any(
            live.get(key) != value
            for key, value in {
                "applicationId": application_id,
                "environmentId": environment_id,
                "serverId": self.request.server_id,
                "appName": app_name,
            }.items()
        ):
            raise ValueError("New application identity or placement differs from the plan.")
        return live

    def _mount_matches(self, live: api.JsonObject) -> bool:
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

    def _recover(self, phase: str) -> dict[str, object]:
        from control_plane.dokploy_target_setup_http import (
            dokploy_target_setup_result_payload,
        )

        data = json.loads(phase)
        application_id, environment_id = data["application"], data["environment"]
        live = self._live(application_id, environment_id, data["app_name"])
        if not self._mount_matches(live):
            raise ValueError(
                "Original volume effect is incomplete or changed; never repeat creation."
            )
        # Only original completed mounting may finish its interrupted record adoption.
        try:
            current = self.store.read_provider_target_record(
                context_name=self.request.context, instance_name="testing"
            )
        except FileNotFoundError:
            current = None
        if current is None or current.target_id != application_id:
            from control_plane.dokploy_target_setup_http import first_testing_replacement_lock_name

            lock_name = first_testing_replacement_lock_name(self.store, self.request)
            with (
                self.store.testing_replacement_execution_lock(lock_name)
                if lock_name
                else nullcontext(lambda: None) as check
            ):
                if check is None:
                    raise ValueError("Testing executor is busy; keep the original operation.")
                check()
                self._adopt_recovered(application_id)
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
        return {
            **self.plan_result(),
            "applied": True,
            "configuration_matches": True,
            "setup": setup,
        }

    def _adopt_recovered(self, application_id: str) -> None:
        from control_plane.dokploy_target_setup_http import (
            CREATE_APPLICATION_SOURCE_LABEL,
            _replaced_target_policies,
        )
        from control_plane.workflows.dokploy_target_adoption import adopt_dokploy_target

        self.checkpoint()
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        from control_plane.dokploy_target_setup_http import (
            fetch_dokploy_target_payload_for_setup,
        )

        adopt_dokploy_target(
            record_store=self.store,
            host=host,
            token=token,
            context=self.request.context,
            instance="testing",
            target_type="application",
            target_id=application_id,
            target_name=self.request.target_name,
            project_name=self.request.project_name,
            source_git_ref=self.request.source_git_ref,
            healthcheck_path=self.request.healthcheck_path,
            deploy_timeout_seconds=self.request.deploy_timeout_seconds,
            expected_current_provider_target=self.request.expected_current_provider_target,
            source_label=CREATE_APPLICATION_SOURCE_LABEL,
            apply=True,
            fetch_target_payload=fetch_dokploy_target_payload_for_setup,
            required_context_owner=(self.product, self.request.context),
            policies=_replaced_target_policies(self.store, self.request),
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        try:
            result = self._recover(provider_effect_phase)
        except Exception:
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            202,
            provider_operation_response_payload(trace_id=self.trace, records={}, result=result),
        )

    def readback(self, key: str) -> dict[str, object]:
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=ROUTE, idempotency_key=key
        )
        if row is None or row.reconciliation_key != self.reconciliation_key():
            raise ValueError("Readback requires the original operation key and plan.")
        # Readback is observational: record adoption is reserved for runner recovery.
        try:
            data = json.loads(row.provider_effect_phase)
            live = self._live(data["application"], data["environment"], data["app_name"])
            matches = self._mount_matches(live)
        except (ValueError, KeyError):
            matches = False
        return {
            **self.plan_result(),
            "operation_state": row.state,
            "provider_effect_phase": row.provider_effect_phase,
            "configuration_matches": matches,
        }

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        from control_plane.dokploy_target_setup_http import (
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

        started = False
        created: dict[str, str] = {}
        execution_check: Callable[[], None]

        def mutate(host: str, token: str, path: str, payload: api.JsonObject) -> api.JsonObject:
            nonlocal started
            execution_check()
            self.checkpoint()
            lease.assert_current()
            lease.checkpoint_effect(json.dumps({**created, "pending": path}, sort_keys=True))
            started = True
            response = mutate_dokploy_payload_for_target_setup(host, token, path, payload)
            key, field = {
                PROJECT_CREATE_PATH: ("project", "projectId"),
                ENVIRONMENT_CREATE_PATH: ("environment", "environmentId"),
                APPLICATION_CREATE_PATH: ("application", "applicationId"),
            }[path]
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
            lease.checkpoint_effect(json.dumps(created, sort_keys=True))
            if path == APPLICATION_CREATE_PATH:
                # Dokploy appends a generated suffix to the requested base name.
                # Retain its exact returned identity before dispatching the mount.
                app_name = self._application_name(application_fields.get("appName"))
                created["app_name"] = app_name
                self.checkpoint()
                live = self._live(value, created["environment"], app_name)
                if live.get("mounts") != []:
                    raise ValueError("A new application must have positively empty mounts.")
                execution_check()
                self.checkpoint()
                lease.checkpoint_effect(
                    json.dumps({**created, "pending": MOUNT_CREATE}, sort_keys=True)
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
                if not self._mount_matches(self._live(value, created["environment"], app_name)):
                    raise ValueError("Independent state-volume readback differs.")
                lease.checkpoint_effect(json.dumps(created, sort_keys=True))
                execution_check()
                self.checkpoint()
            return response

        try:
            self.checkpoint()
            lock_name = first_testing_replacement_lock_name(self.store, self.request)
            with (
                self.store.testing_replacement_execution_lock(lock_name)
                if lock_name
                else nullcontext(lambda: None) as check
            ):
                if check is None:
                    raise ValueError("Testing executor is busy.")
                execution_check = check
                check()
                execute_dokploy_target_setup(
                    control_plane_root_path=self.root,
                    record_store=self.store,
                    request=self.request.model_copy(update={"app_name": self.intent["app_name"]}),
                    required_context_owner=(self.product, self.request.context),
                    application_mutate=mutate,
                )
                lease.assert_current()
                result = self._recover(json.dumps(created))
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace, records={}, result=result
                    ),
                )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    ValueError("Testing storage plan refused before effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Creation may have taken effect; retain the original key and storage plan for readback/recovery."
            ) from error
