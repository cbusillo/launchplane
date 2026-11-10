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
        if evidence.original_request.get("operation", "create-compose") != "create-compose":
            raise ValueError("Retained evidence must describe Compose creation.")
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

    def resolved_phase(self, row: LaunchplaneIdempotencyRecord) -> dict[str, Any]:
        """Positively observe a lost creation response; absence never permits replay."""
        phase = (
            json.loads(row.provider_effect_phase)
            if row.provider_effect_phase
            else self.retained_phase()
        )
        pending = phase.get("pending")
        if not pending:
            return phase
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        projects = read_dokploy_project_membership(host=host, token=token)
        matches = [p for p in projects if p.name == self.request.project_name]
        if len(matches) != 1:
            raise ValueError(
                "Original project outcome remains unknown; retain custody and read back after provider settlement."
            )
        project = matches[0]
        if pending == PROJECT_CREATE_PATH:
            raw = api.as_json_object(
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/project.one",
                    query={"projectId": project.project_id},
                )
            )
            if not raw or raw.get("projectId") != project.project_id:
                raise ValueError("Original project identity is unavailable.")
            created = datetime.fromisoformat(str(raw.get("createdAt", "")))
            if created.utcoffset() is None or created < datetime.fromisoformat(
                row.provider_effect_started_at
            ):
                raise ValueError("Observed project predates the original creation.")
            defaults = [e for e in project.environments if e.name == "production"]
            if len(defaults) != 1:
                raise ValueError("Original default environment is unavailable.")
            environment = api.as_json_object(
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/environment.one",
                    query={"environmentId": defaults[0].environment_id},
                )
            )
            if (
                not environment
                or environment.get("projectId") != project.project_id
                or environment.get("isDefault") is not True
            ):
                raise ValueError("Original default environment identity changed.")
            phase["project"] = {
                "projectId": project.project_id,
                "environment": {
                    key: environment[key]
                    for key in ("environmentId", "projectId", "name", "isDefault")
                },
            }
        else:
            if phase.get("project", {}).get("projectId") != project.project_id:
                raise ValueError("Original project identity changed.")
            environments = [
                e for e in project.environments if e.name == self.request.environment_name
            ]
            if len(environments) != 1:
                raise ValueError("Original environment outcome remains unknown; retain custody.")
            environment_id = environments[0].environment_id
            if pending == ENVIRONMENT_CREATE_PATH:
                phase["environment"] = {"environmentId": environment_id}
            elif pending == COMPOSE_CREATE_PATH:
                expected = phase.get("environment", phase["project"].get("environment", {})).get(
                    "environmentId"
                )
                targets = [t for t in environments[0].targets if t.category == "compose"]
                if expected != environment_id or len(targets) != 1:
                    raise ValueError("Original Compose outcome remains unknown; retain custody.")
                phase["compose"] = {"composeId": targets[0].target_id}
            else:
                raise ValueError("Unknown provider creation phase.")
        phase.pop("pending")
        return phase

    def adopted_result(self, phase: dict[str, Any]) -> dict[str, object]:
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.dokploy_target_setup_http import dokploy_target_setup_result_payload
        from control_plane.workflows.dokploy_target_adoption import DokployTargetAdoptionResult

        compose_id = phase["compose"]["composeId"]
        target = self.store.read_dokploy_target_record(
            context_name=self.request.context, instance_name=self.request.instance
        )
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.request.context, instance_name=self.request.instance
        )
        provider = self.store.read_provider_target_record(
            context_name=self.request.context, instance_name=self.request.instance
        )
        if (
            target_id.target_id != compose_id
            or target.target_type != "compose"
            or target.target_name != self.request.target_name
            or target.project_name != self.request.project_name
            or target.domains != self.request.domains
            or target.healthcheck_path != self.request.healthcheck_path
            or provider.to_deployed_target_reference()
            != ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            ).to_deployed_target_reference()
        ):
            raise ValueError("Original created target binding differs.")
        return {
            "mode": "apply",
            "operation": self.request.operation,
            "context": self.request.context,
            "instance": self.request.instance,
            "applied": True,
            "route_domain_ids": [],
            "setup": dokploy_target_setup_result_payload(
                DokployTargetAdoptionResult(
                    applied=True,
                    target_record=target,
                    target_id_record=target_id,
                    provider_target_record=provider,
                )
            ),
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
            environments = project.get("environments") if project else None
            matches = (
                [
                    item
                    for item in environments
                    if isinstance(item, dict)
                    and item.get("environmentId") == evidence.environment_id
                ]
                if isinstance(environments, list)
                else []
            )
            environment = matches[0] if len(matches) == 1 else None
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
            for kind, resource in (("project", project), ("environment", environment)):
                created_at = resource.get("createdAt")
                try:
                    if not isinstance(created_at, str):
                        raise ValueError("Missing creation timestamp")
                    created = datetime.fromisoformat(created_at)
                except ValueError as error:
                    raise ValueError(
                        f"Original {kind} creation timestamp is unavailable or invalid; "
                        "retain custody and qualify provider metadata before recovery."
                    ) from error
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
        compose_id = phase.get("compose", {}).get("composeId")
        compose_environment_id: api.JsonValue = None
        for environment in matches[0].environments:
            for target in environment.targets:
                if target.target_id != compose_id or target.category != "compose":
                    raise ValueError(
                        "Original placement contains an unrecorded target; retain custody."
                    )
        if compose_id:
            from control_plane.dokploy.target_source_setup import require_empty_compose_source

            raw = api.as_json_object(
                api.dokploy_request(
                    host=host, token=token, path="/api/compose.one", query={"composeId": compose_id}
                )
            )
            if (
                not raw
                or raw.get("serverId") != self.request.server_id
                or raw.get("name") != self.request.target_name
                or raw.get("env", "")
            ):
                raise ValueError("Original Compose identity or empty state differs.")
            require_empty_compose_source(raw, compose_id)
            compose_environment_id = raw.get("environmentId")
        known = project.get("environment", {})
        known_id = known.get("environmentId")
        environment = phase.get("environment", {})
        if environment:
            known_id = _extract_provider_id(environment, "environmentId", "environment")
        if known_id and not any(e.environment_id == known_id for e in matches[0].environments):
            raise ValueError("Original environment is absent or changed.")
        if compose_id and (
            compose_environment_id != known_id
            or not any(
                e.environment_id == known_id
                and any(t.target_id == compose_id and t.category == "compose" for t in e.targets)
                for e in matches[0].environments
            )
        ):
            raise ValueError("Original Compose is not in its recorded environment.")
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
        result: dict[str, Any] = {
            "allocation_state": row.state,
            "recoverable": False,
            "original_trace_id": row.response_trace_id,
        }
        if row.state == "completed":
            return result
        try:
            phase = self.resolved_phase(row)
            try:
                self.adopted_result(phase)
            except (KeyError, ValueError, FileNotFoundError):
                result["recovery_sha256"] = self.recovery_digest(phase)
            else:
                result["adoption_recorded"] = True
                result["recovery_sha256"] = request_fingerprint(
                    {"intent": self.intent(), "phase": phase}
                )
            result["recoverable"] = row.state == "reconcile_required" and bool(phase.get("project"))
        except ValueError as error:
            result["instructions"] = str(error)
        return result

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        try:
            observed = self.readback()
        except (ValueError, OSError, InventoryUnavailable, click.ClickException):
            return ProviderObservation("unknown")
        if (
            observed.get("recoverable") is True
            and self.request.expected_compose_recovery_sha256
            and observed.get("recovery_sha256") == self.request.expected_compose_recovery_sha256
        ):
            if observed.get("adoption_recorded"):
                row = self.original()
                assert row is not None
                return ProviderObservation(
                    "present",
                    response_payload=provider_operation_response_payload(
                        trace_id=self.trace,
                        records={},
                        result=self.adopted_result(self.resolved_phase(row)),
                    ),
                )
            return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        from control_plane.dokploy_target_setup_http import mutate_dokploy_payload_for_target_setup

        row = self.original()
        try:
            phase = self.resolved_phase(row) if row and row.provider_effect_phase else {}
            retained = self.retained_phase()
            if not phase and retained:
                if self.request.expected_compose_recovery_sha256 != self.recovery_digest(retained):
                    raise ProviderMutationRejectedError(
                        ValueError("Review original-operation readback before continuation.")
                    )
                phase = retained
        except ProviderMutationRejectedError:
            raise
        except (ValueError, OSError, InventoryUnavailable, click.ClickException) as error:
            if row and row.provider_effect_phase:
                raise ProviderMutationUnknownError(
                    "Original partial effect still needs observation."
                ) from error
            raise ProviderMutationRejectedError(error) from error
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
                if phase:
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
                    acknowledged: api.JsonObject = {identifier: value}
                    if path == PROJECT_CREATE_PATH and "environment" in response:
                        environment = api.as_json_object(response["environment"])
                        if environment is None:
                            raise ValueError(
                                "Created default environment is malformed; retain custody."
                            )
                        acknowledged["environment"] = {
                            key: environment[key]
                            for key in ("environmentId", "projectId", "name", "isDefault")
                            if key in environment
                        }
                    phase[field] = acknowledged
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
