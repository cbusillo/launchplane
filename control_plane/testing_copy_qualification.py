import base64
import gzip
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from control_plane._testing_copy_qualification_program import COPY_QUALIFICATION_HOST_PROGRAM
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.lane_service_restart import (
    SERVICE_RESTART_ROUTE,
    LaneServiceRestartPlan,
    LaneServiceRestartRequest,
    LaneServiceRestartResponse,
    LaneServiceRestartResult,
    restart_activity_scope,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.testing_copy_qualification import (
    TestingCopyPhysicalIdentity,
    TestingCopyQualificationPlan,
    TestingCopyQualificationProof,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import (
    ODOO_DATABASE_DATA_DIRECTORY,
    ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
)
from control_plane.dokploy.native_copy_stream import (
    _read_remote,
    require_registered_remote_server,
    NativeSourceAuthorizationDenied,
)
from control_plane.dokploy.post_deploy import (
    DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
    odoo_testing_schedule_lock_script,
    run_manual_data_schedule,
)
from control_plane.integration_readback import (
    INTEGRATION_FAMILIES,
    INTEGRATION_READBACK_PROGRAM,
    IntegrationReadbackPolicy,
)
from control_plane.lane_service_restart import (
    ServiceRestartAdapter,
    ServiceRestartRefused,
    plan_service_restart,
)
from control_plane.lane_usage import lane_usage_protected
from control_plane.odoo_testing_drill_usage import testing_drill_environment
from control_plane.odoo_testing_restore import (
    generation_binding,
    read_current_testing_generation,
    validate_fresh_testing_overrides,
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
)
from control_plane.runtime_key_safety import is_secret_shaped_runtime_key, runtime_key_integrations
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore

_logger = logging.getLogger(__name__)


def start_parameter_keys(
    *,
    host: str,
    token: str,
    server_id: str,
    physical: TestingCopyPhysicalIdentity,
    readback_spec: str,
) -> list[str]:
    raw = api.dokploy_request(
        host=host,
        token=token,
        path="/api/docker.getConfig",
        query={"containerId": physical.container_ids["script-runner"], "serverId": server_id},
    )
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict) or raw.get("Id") != physical.container_ids["script-runner"]:
        raise ServiceRestartRefused("The original runner's immutable configuration is unavailable.")
    configuration = raw.get("Config")
    if not isinstance(configuration, dict) or not isinstance(configuration.get("Env"), list):
        raise ServiceRestartRefused("The original runner's credential boundary is unavailable.")
    raw_environment = configuration["Env"]
    if not isinstance(raw_environment, list) or any(
        not isinstance(value, str) or "=" not in value for value in raw_environment
    ):
        raise ServiceRestartRefused("The original runner environment is incomplete.")
    environment: dict[str, str] = {}
    for value in raw_environment:
        key, _, setting = str(value).partition("=")
        if key in environment:
            raise ServiceRestartRefused("The original runner environment is ambiguous.")
        environment[key] = setting
    for key, value in environment.items():
        if (
            value
            and key not in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
            and (
                is_secret_shaped_runtime_key(key)
                or runtime_key_integrations(key)
                or key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_"))
            )
        ):
            raise ServiceRestartRefused(
                "The original runner still carries an unqualified external capability."
            )
    encoded = environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", "")
    validate_fresh_testing_overrides(encoded_payload=encoded, readback_spec=readback_spec)
    payload = json.loads(base64.b64decode(encoded, validate=True)) if encoded else {}
    for item in payload.get("addon_settings", []):
        if is_secret_shaped_runtime_key(str(item.get("setting", ""))):
            raise ServiceRestartRefused(
                "The original runner carries an unqualified credential override."
            )
    return [str(item["key"]) for item in payload.get("config_parameters", [])]


def ordinary_restart_request(request: LaneServiceRestartRequest) -> LaneServiceRestartRequest:
    return request.model_copy(update={"qualification_mode": None, "qualification_source_key": ""})


def qualification_spec(
    store: PostgresRecordStore,
    request: LaneServiceRestartRequest,
    plan: LaneServiceRestartPlan,
    host: str,
    token: str,
) -> dict[str, Any]:
    generation = read_current_testing_generation(
        store,
        product=request.product,
        context=request.context,
        instance=request.instance,
        resource_qualification=True,
    )
    if generation is None or not generation.completed_at or generation.target_id != plan.target_id:
        raise ServiceRestartRefused("The retained completed testing generation is unavailable.")
    target = store.read_dokploy_target_record(context_name=request.context, instance_name="testing")
    hold = target.policies.staff_testing_hold
    if hold is None or hold.purpose != "isolation_staging" or hold.recorded_by != plan.actor:
        raise ServiceRestartRefused(
            "Copy qualification requires this caller's unchanged isolation hold."
        )
    safety = store.read_disposable_lane_safety(request.product, request.context, "testing")
    if safety is not None and (
        lane_usage_protected(
            store,
            product=request.product,
            context=request.context,
            instance="testing",
            usage=safety.usage,
            require_unused=False,
        )
        or safety.data == "only_copy"
        or safety.production_resources in {"shared", "separation_ready"}
    ):
        raise ServiceRestartRefused(
            "Existing use, original-copy custody or production-sharing protection remains."
        )
    provider = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=plan.target_id
    )
    if (
        provider.get("autoDeploy") is not False
        or provider.get("appName") != plan.app_name
        or provider.get("serverId") != plan.server_id
    ):
        raise ServiceRestartRefused(
            "The provider's automatic-deploy exclusion or placement is unavailable."
        )
    require_registered_remote_server(plan.server_id)
    environment = testing_drill_environment(store, request.context)
    database = environment.get("ODOO_DB_NAME", "")
    if not database:
        raise ServiceRestartRefused("The generation's database authority is unavailable.")
    policy = IntegrationReadbackPolicy(
        families=INTEGRATION_FAMILIES, allowed_integrations=(), protected_shopify_store_handles=()
    )
    result: dict[str, Any] = {
        "generation": generation.model_dump(mode="json"),
        "compose_project": plan.app_name,
        "image_reference": plan.before.image_reference,
        "database_name": database,
        "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
        "storage_maintenance": True,
        "copy_binding_sha256": generation_binding(generation),
        "source_mode": generation.source_mode,
        "readback_spec": policy.encoded_spec(),
        "readback_program": INTEGRATION_READBACK_PROGRAM,
        "runner_healthcheck_command": ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
        "qualification_mode": request.qualification_mode,
        "hold_sha256": canonical_json_sha256(hold.model_dump(mode="json")),
    }
    if plan.qualification is not None:
        qualification = plan.qualification
        if (
            qualification.generation_binding_sha256 != result["copy_binding_sha256"]
            or qualification.hold_sha256 != result["hold_sha256"]
        ):
            raise ServiceRestartRefused("The original generation or isolation hold changed.")
        result.update(
            physical=qualification.physical.model_dump(mode="json"),
            qualification_binding=plan.digest(),
        )
        result["start_parameter_keys"] = (
            start_parameter_keys(
                host=host,
                token=token,
                server_id=plan.server_id,
                physical=qualification.physical,
                readback_spec=policy.encoded_spec(),
            )
            if request.qualification_mode == "start"
            else []
        )
    return result


def read_qualification_host(
    *,
    host: str,
    token: str,
    server_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=COPY_QUALIFICATION_HOST_PROGRAM,
        payload=spec,
        receive=frames.append,
        timeout_seconds=120,
    )
    if len(frames) != 1:
        raise ServiceRestartRefused("Copy qualification returned incomplete evidence.")
    return frames[0]


def plan_copy_qualification(
    *,
    store: PostgresRecordStore,
    root: Path,
    request: LaneServiceRestartRequest,
    actor: str,
) -> tuple[LaneServiceRestartPlan, RuntimeIdentity, str, str, str]:
    plan, identity, host, token, health = plan_service_restart(
        store=store,
        root=root,
        request=ordinary_restart_request(request),
        actor=actor,
    )
    spec = qualification_spec(store, request, plan, host, token)
    spec["image_reference"] = identity.image_reference
    frame = read_qualification_host(
        host=host, token=token, server_id=plan.server_id, spec={**spec, "mode": "inspect"}
    )
    if frame.get("type") != "complete":
        raise ServiceRestartRefused(
            "Current PostgreSQL-only execution and no-egress prerequisites are unverified."
        )
    physical = TestingCopyPhysicalIdentity.model_validate(frame.get("physical"))
    if request.qualification_mode == "start":
        previous = store.read_idempotency_record(
            scope=restart_activity_scope(request.product),
            route_path=SERVICE_RESTART_ROUTE,
            idempotency_key=request.qualification_source_key,
        )
        if previous is None or previous.state != "completed":
            raise ServiceRestartRefused(
                "Start requires the original completed sanitization receipt."
            )
        response = LaneServiceRestartResponse.model_validate(previous.response_payload)
        source_plan, proof = response.result.plan, response.result.qualification_proof
        if (
            response.result.status != "pass"
            or source_plan.actor != actor
            or source_plan.qualification is None
            or source_plan.qualification.mode != "sanitize"
            or proof is None
            or proof.generation_binding_sha256 != spec["copy_binding_sha256"]
            or proof.resources_sha256 != physical.resources_sha256
            or any(
                getattr(source_plan, field) != getattr(plan, field)
                for field in (
                    "product",
                    "context",
                    "instance",
                    "artifact_id",
                    "deployment_record_id",
                )
            )
        ):
            raise ServiceRestartRefused(
                "The sanitization receipt no longer proves this exact original copy."
            )
    if request.qualification_mode is None:
        raise ServiceRestartRefused("Qualification must be explicitly requested.")
    qualification = TestingCopyQualificationPlan(
        mode=request.qualification_mode,
        generation_binding_sha256=spec["copy_binding_sha256"],
        hold_sha256=spec["hold_sha256"],
        namespace=spec["generation"]["namespace"],
        database_name=spec["database_name"],
        physical=physical,
        source_key=request.qualification_source_key,
    )
    return plan.model_copy(update={"qualification": qualification}), identity, host, token, health


def qualification_schedule(spec: dict[str, Any]) -> tuple[str, str, str]:
    encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
    program = base64.b64encode(gzip.compress(COPY_QUALIFICATION_HOST_PROGRAM.encode())).decode()
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + odoo_testing_schedule_lock_script(spec["compose_project"])
        + "python3 - <<'LP_COPY_QUALIFICATION'\nimport base64,gzip,sys\n"
        + "sys.argv=['copy-qualification',"
        + repr(encoded)
        + ",'copy_qualification=']\n"
        + "exec(compile(gzip.decompress(base64.b64decode("
        + repr(program)
        + ")), '<copy-qualification>', 'exec'),{})\nLP_COPY_QUALIFICATION\n"
    )
    return (
        "Launchplane copy qualification " + spec["operation"],
        "lp-copy-qualification-" + spec["operation"][:24],
        script,
    )


class TestingCopyQualificationAdapter(ServiceRestartAdapter):
    def spec(self, provider_operation_key: str, mode: str) -> dict[str, Any]:
        plan, expected, host, token, _ = plan_service_restart(
            store=self.store,
            root=self.root,
            request=ordinary_restart_request(self.request),
            actor=self.plan.actor,
        )
        for field in (
            "product",
            "context",
            "instance",
            "target_id",
            "app_name",
            "server_id",
            "artifact_id",
            "deployment_record_id",
        ):
            if getattr(plan, field) != getattr(self.plan, field):
                raise ServiceRestartRefused("The original copy's deployment authority changed.")
        result = qualification_spec(self.store, self.request, self.plan, host, token)
        result.update(
            mode=mode,
            operation=hashlib.sha256(provider_operation_key.encode()).hexdigest(),
            image_reference=expected.image_reference,
        )
        return result

    def response(self, proof: TestingCopyQualificationProof) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.trace_id,
            records={},
            result=LaneServiceRestartResult(
                status="pass",
                plan=self.plan,
                plan_sha256=self.plan.digest(),
                qualification_proof=proof,
            ).model_dump(mode="json"),
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation(outcome="unknown")
        try:
            spec = self.spec(provider_operation_key, "observe")
            frame = read_qualification_host(
                host=self.host, token=self.token, server_id=self.plan.server_id, spec=spec
            )
            if frame.get("type") == "complete":
                proof = TestingCopyQualificationProof.model_validate(frame.get("proof"))
                qualification = self.plan.qualification
                if (
                    qualification is None
                    or proof.generation_binding_sha256 != qualification.generation_binding_sha256
                    or proof.resources_sha256 != qualification.physical.resources_sha256
                    or proof.database_stopped != (qualification.mode == "sanitize")
                    or proof.runner_stopped != (qualification.mode == "sanitize")
                ):
                    raise ServiceRestartRefused(
                        "The copy proof belongs to a different qualification."
                    )
                return ProviderObservation(
                    outcome="present",
                    response_status_code=200,
                    response_payload=self.response(proof),
                )
            name, app, _ = qualification_schedule(spec)
            schedule = api.find_matching_dokploy_schedule(
                host=self.host,
                token=self.token,
                target_id=self.plan.server_id,
                schedule_type="dokploy-server",
                schedule_name=name,
                app_name=app,
            )
            deployment = (
                api.latest_deployment_for_schedule(
                    host=self.host, token=self.token, schedule_id=api.schedule_key(schedule)
                )
                if schedule
                else None
            )
            if (
                frame.get("type") == "absent"
                and frame.get("retry_safe") is True
                and api.deployment_status(deployment) in {"done", "error"}
            ):
                return ProviderObservation(outcome="absent", retry_safe=True)
            if (
                frame.get("type") == "unknown"
                and not deployment
                and (not provider_effect_phase or provider_effect_phase.endswith("schedule_upsert"))
            ):
                return ProviderObservation(outcome="absent", retry_safe=True)
        except NativeSourceAuthorizationDenied:
            raise
        except Exception:
            _logger.warning("Testing copy qualification observation is unavailable.")
            return ProviderObservation(outcome="unknown")
        return ProviderObservation(outcome="unknown")

    def observe_with_effect_started_at(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        provider_effect_started_at: str,
    ) -> ProviderObservation:
        return self.observe(provider_operation_key, provider_effect_phase, reconciliation_key)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.store.odoo_synchronous_lane_reservation(
            product=self.plan.product, context=self.plan.context, instance="testing"
        ) as owner:
            if owner is not None:
                raise ProviderMutationRejectedError(
                    ServiceRestartRefused("An existing operation holds the original copy.")
                )
            try:
                spec = self.spec(provider_operation_key, "apply")
            except (ValueError, OSError) as error:
                raise ProviderMutationRejectedError(
                    ServiceRestartRefused("The original copy's qualification authority changed.")
                ) from error
            name, app, script = qualification_schedule(spec)

            def checkpoint(phase: str) -> None:
                self.spec(provider_operation_key, "apply")
                lease.checkpoint_effect("copy_qualification_" + phase)

            payload: api.JsonObject = {
                "name": name,
                "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
                "appName": app,
                "shellType": "bash",
                "scheduleType": "dokploy-server",
                "command": "testing copy qualification",
                "script": script,
                "serverId": self.plan.server_id,
                "userId": self.plan.server_id,
                "enabled": False,
                "timezone": "UTC",
            }
            try:
                run_manual_data_schedule(
                    host=self.host,
                    token=self.token,
                    target_definition=source.DokployTargetDefinition(
                        context=self.plan.context,
                        instance="testing",
                        target_id=self.plan.target_id,
                        target_name=self.plan.app_name,
                    ),
                    schedule_lookup_id=self.plan.server_id,
                    schedule_type="dokploy-server",
                    schedule_name=name,
                    schedule_app_name=app,
                    schedule_payload=payload,
                    resolved_schedule_execution_timeout_seconds=1200,
                    before_provider_mutation=checkpoint,
                )
                observed = self.observe(
                    provider_operation_key, "copy_qualification_complete", self.reconciliation_key()
                )
            except NativeSourceAuthorizationDenied:
                raise
            except Exception as error:
                raise ProviderMutationUnknownError(
                    "Copy qualification is unsettled; retain the original key and custody."
                ) from error
            if observed.outcome != "present":
                raise ProviderMutationUnknownError(
                    "The original copy has no complete qualification proof; retain the original key."
                )
            return ProviderMutationOutcome(
                response_status_code=200, response_payload=observed.response_payload
            )
