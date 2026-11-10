"""Original-operation recovery for first production Compose target setup."""

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from control_plane.dokploy import api, source
from control_plane.dokploy_inventory import InventoryUnavailable, read_dokploy_project_membership
import click
from control_plane.dokploy_target_setup_http import (
    DokployTargetSetupEnvelope,
    execute_dokploy_target_setup,
    first_production_target_lock_name,
    require_first_production_target_creation,
)
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
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.workflows.dokploy_target_adoption import (
    PROJECT_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    COMPOSE_CREATE_PATH,
    _extract_provider_id,
)

ROUTE = "/v1/dokploy-targets/setup"


@dataclass
class ComposeAllocationAdapter:
    root: Path
    store: PostgresRecordStore
    request: DokployTargetSetupEnvelope
    product: str
    scope: str
    key: str
    trace: str

    def intent(self) -> dict[str, Any]:
        payload = self.request.model_dump(mode="json")
        for field in ("mode", "confirmation", "expected_compose_recovery_sha256"):
            payload.pop(field, None)
        host, _ = source.read_dokploy_config(control_plane_root=self.root)
        return {"request": payload, "product": self.product, "provider_host": host}

    def target_key(self) -> str:
        return first_production_target_lock_name(self.request.context)

    def reconciliation_key(self) -> str:
        return request_fingerprint(self.intent())

    def original(self) -> LaunchplaneIdempotencyRecord | None:
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=ROUTE, idempotency_key=self.key
        )
        if row is not None and (
            row.reconciliation_key != self.reconciliation_key()
            or self.store.read_original_mutation_intent(row) != self.intent()
        ):
            raise ValueError("Preserve the original caller, request, key and provider binding.")
        return row

    def retained_phase(self) -> dict[str, Any]:
        evidence = self.request.retained_compose_setup
        if evidence is None:
            return {}
        if {
            "retained_compose_setup",
            "expected_compose_recovery_sha256",
        } & evidence.original_request.keys():
            raise ValueError(
                "Retained evidence requires the original request, not another recovery."
            )
        original = DokployTargetSetupEnvelope.model_validate(
            {**evidence.original_request, "mode": "readback", "operation": "create-compose"}
        )
        expected = self.request.model_dump(
            mode="json",
            exclude={
                "mode",
                "confirmation",
                "expected_compose_recovery_sha256",
                "retained_compose_setup",
            },
        )
        if (
            evidence.original_key != self.key
            or evidence.original_scope != self.scope
            or original.model_dump(
                mode="json",
                exclude={
                    "mode",
                    "confirmation",
                    "expected_compose_recovery_sha256",
                    "retained_compose_setup",
                },
            )
            != expected
            or evidence.project().name != self.request.project_name
            or self.request.environment_name != "production"
            or any(p.name == self.request.project_name for p in evidence.before.projects)
        ):
            raise ValueError(
                "Retained evidence does not bind this original caller, key, request and placement."
            )
        return {
            "project": {
                "projectId": evidence.project_id,
                "environment": {
                    "environmentId": evidence.environment_id,
                    "projectId": evidence.project_id,
                    "name": "production",
                    "isDefault": True,
                },
            }
        }

    def recovery_digest(self, phase: dict[str, Any]) -> str:
        self.checkpoint()
        evidence = self.request.retained_compose_setup
        if evidence is not None:
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            project = api.as_json_object(
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/project.one",
                    query={"projectId": evidence.project_id},
                )
            )
            environment = api.as_json_object(
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/environment.one",
                    query={"environmentId": evidence.environment_id},
                )
            )
            if (
                not project
                or not environment
                or project.get("projectId") != evidence.project_id
                or project.get("name") != self.request.project_name
                or environment.get("environmentId") != evidence.environment_id
                or environment.get("projectId") != evidence.project_id
                or environment.get("name") != "production"
                or environment.get("isDefault") is not True
                or environment.get("env") != ""
            ):
                raise ValueError(
                    "Retained placement is not the original empty default environment."
                )
            for resource in (project, environment):
                created = datetime.fromisoformat(str(resource.get("createdAt", "")))
                if (
                    created.utcoffset() is None
                    or not evidence.before_at <= created <= evidence.after_at
                ):
                    raise ValueError(
                        "Retained placement predates or differs from the original observation window."
                    )
        return request_fingerprint(
            {
                "intent": self.intent(),
                "phase": phase,
                "placement": self.placement(phase),
                "owner": self.store.read_product_profile_record(self.product).model_dump(
                    mode="json"
                ),
            }
        )

    def checkpoint(self) -> None:
        require_first_production_target_creation(self.store, self.request, product=self.product)

    def placement(self, phase: dict[str, Any]) -> dict[str, Any]:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        projects = read_dokploy_project_membership(host=host, token=token)
        matches = [p for p in projects if p.name == self.request.project_name]
        if not phase:
            if matches:
                raise ValueError("Unrecorded placement exists; preserve the original operation.")
            return {}
        project = phase.get("project", {})
        project_id = _extract_provider_id(project, "projectId", "project")
        if len(matches) != 1 or matches[0].project_id != project_id:
            raise ValueError("Original project is absent, ambiguous or changed.")
        if any(environment.targets for environment in matches[0].environments):
            raise ValueError("Original placement is no longer empty; retain custody.")
        known = project.get("environment", {})
        known_id = known.get("environmentId")
        environment = phase.get("environment", {})
        if environment:
            known_id = _extract_provider_id(environment, "environmentId", "environment")
        if known_id and not any(e.environment_id == known_id for e in matches[0].environments):
            raise ValueError("Original environment is absent or changed.")
        if phase.get("pending") not in {None, ENVIRONMENT_CREATE_PATH}:
            raise ValueError("Uncertain provider creation requires observation; never replay it.")
        if phase.get("pending") == ENVIRONMENT_CREATE_PATH and (
            known.get("name") != self.request.environment_name or known.get("isDefault") is not True
        ):
            raise ValueError("The interrupted environment creation remains uncertain.")
        allowed = {value for value in (known.get("environmentId"), known_id) if value}
        if any(e.environment_id not in allowed for e in matches[0].environments):
            raise ValueError("Unrecorded environment exists in the original project.")
        return matches[0].model_dump(mode="json")

    def readback(self) -> dict[str, Any]:
        row = self.original()
        if row is None:
            retained = self.retained_phase()
            if retained:
                return {
                    "allocation_state": "retained_original",
                    "recoverable": True,
                    "historical_reservation_present": False,
                    "recovery_sha256": self.recovery_digest(retained),
                }
            return {
                "allocation_state": "unrecorded_original",
                "recoverable": False,
                "instructions": "Preserve the original request, key, trace and placement. No durable original exists; do not replay apply.",
            }
        phase = (
            json.loads(row.provider_effect_phase)
            if row.provider_effect_phase
            else self.retained_phase()
        )
        result: dict[str, Any] = {
            "allocation_state": row.state,
            "recoverable": False,
            "original_trace_id": row.response_trace_id,
        }
        if row.state == "completed":
            return result
        self.checkpoint()
        result["recovery_sha256"] = self.recovery_digest(phase)
        result["recoverable"] = row.state == "reconcile_required" and bool(phase.get("project"))
        return result

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        try:
            observed = self.readback()
        except (ValueError, OSError, InventoryUnavailable, click.ClickException):
            return ProviderObservation("unknown")
        if (
            observed.get("recoverable") is True
            and self.request.expected_compose_recovery_sha256
            and observed.get("recovery_sha256") == self.request.expected_compose_recovery_sha256
        ):
            return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        from control_plane.dokploy_target_setup_http import mutate_dokploy_payload_for_target_setup

        row = self.original()
        phase = json.loads(row.provider_effect_phase) if row and row.provider_effect_phase else {}
        retained = self.retained_phase()
        if not phase and retained:
            if self.request.expected_compose_recovery_sha256 != self.recovery_digest(retained):
                raise ProviderMutationRejectedError(
                    ValueError("Review original-operation readback before continuation.")
                )
            phase = retained
        started = bool(phase)
        try:
            with self.store.testing_replacement_execution_lock(self.target_key()) as check:
                if check is None:
                    raise ValueError("Another executor holds first target setup.")
                check()
                self.checkpoint()
                self.placement(phase)
                if (
                    retained
                    and self.request.expected_compose_recovery_sha256 != self.recovery_digest(phase)
                ):
                    raise ValueError("Reviewed original placement changed before continuation.")
                if retained and row is not None and not row.provider_effect_phase:
                    # This is a new recovery checkpoint, not a fabricated historical reservation.
                    lease.checkpoint_effect(json.dumps(phase))

                def mutate(
                    host: str, token: str, path: str, payload: api.JsonObject
                ) -> api.JsonObject:
                    nonlocal started
                    check()
                    self.checkpoint()
                    lease.assert_current()
                    field, identifier = {
                        PROJECT_CREATE_PATH: ("project", "projectId"),
                        ENVIRONMENT_CREATE_PATH: ("environment", "environmentId"),
                        COMPOSE_CREATE_PATH: ("compose", "composeId"),
                    }[path]
                    if field in phase:
                        response = api.as_json_object(phase[field])
                        if response is None:
                            raise ValueError("Original creation response is malformed.")
                        return response
                    lease.checkpoint_effect(json.dumps({**phase, "pending": path}))
                    started = True
                    response = mutate_dokploy_payload_for_target_setup(host, token, path, payload)
                    value = _extract_provider_id(response, identifier, field)
                    retained: api.JsonObject = {identifier: value}
                    if path == PROJECT_CREATE_PATH and "environment" in response:
                        environment = api.as_json_object(response["environment"])
                        if environment is None:
                            raise ValueError(
                                "Created default environment is malformed; retain custody."
                            )
                        retained["environment"] = {
                            key: environment[key]
                            for key in ("environmentId", "projectId", "name", "isDefault")
                            if key in environment
                        }
                    phase[field] = retained
                    phase.pop("pending", None)
                    lease.checkpoint_effect(json.dumps(phase))
                    return response

                result = execute_dokploy_target_setup(
                    control_plane_root_path=self.root,
                    record_store=self.store,
                    request=self.request,
                    required_context_owner=(self.product, self.request.context),
                    disposable_checkpoint=self.checkpoint,
                    compose_mutate=mutate,
                )
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace, records={}, result=result
                    ),
                )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(error) from error
            raise ProviderMutationUnknownError(
                "Target creation may have partial effects; preserve the original request and key."
            ) from error
