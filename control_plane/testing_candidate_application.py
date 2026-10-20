"""Prepare a private build/copy without publishing any serving-lane authority."""

import base64
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import re

from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_candidate_application import (
    TestingCandidateApplication,
    TestingCandidatePrepared,
)
from control_plane.dokploy import api, compose, post_deploy, source
from control_plane.dokploy.native_copy_stream import _read_remote, NativeProgramFailed
from control_plane.dokploy.target_source_setup import require_empty_compose_source
from control_plane.dokploy_target_setup_http import fetch_dokploy_compose_domains_for_target_setup
from control_plane.integration_readback import (
    INTEGRATION_FAMILIES,
    INTEGRATION_READBACK_PASSED_PATH,
    INTEGRATION_READBACK_PROGRAM,
    IntegrationReadbackPolicy,
)
from control_plane.odoo_testing_native_source import prepare_native_testing_delivery
from control_plane.odoo_testing_restore import generation_binding, TESTING_COPY_COMPLETION_PARAMETER
from control_plane.provider_operations import ProviderOperationLease
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import require_candidate_target_custody
from control_plane.testing_candidate_probe import CANDIDATE_HOST_PROBE, CANDIDATE_CONTAINER_PROBE
from control_plane.testing_candidate_runtime import TestingCandidateRuntimeInputs


class CandidateObservationUnavailable(ValueError):
    """The original candidate effect or its private health needs observation."""


class CandidatePrivateReadinessFailed(CandidateObservationUnavailable):
    """The fixed read-only probe returned a failure, rather than losing its transport."""


def candidate_private_readiness(
    *,
    host: str,
    token: str,
    allocation: TestingCandidateAllocation,
    inputs: TestingCandidateRuntimeInputs,
    deployment_id: str,
) -> TestingCandidatePrepared:
    plan = inputs.plan
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=allocation.compose_id
    )
    require_candidate_configuration(allocation, inputs, payload)
    generation = plan.generation
    spec: dict[str, object] = {
        "generation": generation.model_dump(mode="json")
        if generation
        else {
            "namespace": allocation.namespace,
            "instance": "testing",
            "resources_binding_sha256": allocation.resources_binding_sha256,
        },
        "compose_project": str(payload.get("appName") or ""),
        "database_name": inputs.runtime_environment["ODOO_DB_NAME"],
        "database_data_directory": compose.ODOO_DATABASE_DATA_DIRECTORY,
        "image_reference": plan.image_reference,
        "runtime_identity": plan.runtime_identity.model_dump(mode="json", exclude={"deployed_at"}),
        "expected_overrides_payload": inputs.workflow_environment.get(
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""
        ),
        "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
        "web_config": compose.ODOO_WEB_CONFIG_PATH,
        "base_url": plan.base_url,
        "http_port": int(inputs.runtime_environment["ODOO_HTTP_PORT"]),
        "copy_binding_sha256": generation_binding(generation) if generation else "",
        "witness_sql": "SELECT value FROM ir_config_parameter WHERE key='"
        + TESTING_COPY_COMPLETION_PARAMETER
        + "';\n",
        "readback_spec": IntegrationReadbackPolicy(
            families=INTEGRATION_FAMILIES,
            allowed_integrations=(),
            protected_shopify_store_handles=(),
        ).encoded_spec(),
        "readback_program": base64.b64encode(INTEGRATION_READBACK_PROGRAM.encode()).decode(),
        "credential_program": base64.b64encode(CANDIDATE_CONTAINER_PROBE.encode()).decode(),
    }
    frames: list[dict[str, object]] = []
    try:
        _read_remote(
            host=host,
            token=token,
            server_id=allocation.server_id,
            program=CANDIDATE_HOST_PROBE,
            payload=spec,
            receive=frames.append,
            timeout_seconds=120,
        )
    except NativeProgramFailed:
        raise CandidatePrivateReadinessFailed("The private probe returned a failure.") from None
    if frames != [{"type": "complete", "private_readiness_verified": True}]:
        raise CandidateObservationUnavailable("Candidate private readiness is unverified.")
    return TestingCandidatePrepared(
        plan_sha256=plan.digest,
        runtime_identity=plan.runtime_identity,
        private_endpoint="http://"
        + allocation.namespace
        + ":"
        + inputs.runtime_environment["ODOO_HTTP_PORT"],
        deployment_id=deployment_id,
        source_witness=generation_binding(generation) if generation else "empty:" + plan.digest,
        integration_isolation_verified=True,
        credentials_verified=True,
        observed_at=datetime.now(UTC),
    )


def require_candidate_configuration(
    allocation: TestingCandidateAllocation,
    inputs: TestingCandidateRuntimeInputs,
    payload: api.JsonObject,
) -> None:
    if (
        payload.get("composeId"),
        payload.get("environmentId"),
        payload.get("serverId"),
        payload.get("name"),
    ) != (
        allocation.compose_id,
        allocation.environment_id,
        allocation.server_id,
        allocation.namespace,
    ):
        raise ValueError("Candidate placement changed.")
    if (
        payload.get("autoDeploy") is not False
        or payload.get("sourceType") != "raw"
        or payload.get("composePath") != "docker-compose.yml"
        or compose.compose_file_sha256(str(payload.get("composeFile") or ""))
        != compose.compose_file_sha256(inputs.compose_file)
        or api.parse_dokploy_env_text(str(payload.get("env") or "")) != inputs.runtime_environment
        or (
            api.compose_environment_is_encoded(str(payload.get("env") or ""))
            and api.compose_unqualified_keys(
                str(payload.get("env") or ""), inputs.runtime_environment
            )
        )
    ):
        raise ValueError("Candidate configuration has not retained its exact admitted inputs.")


def candidate_configuration_sha256(payload: api.JsonObject) -> str:
    """Bind the intended private configuration without storing its secret values."""
    return canonical_json_sha256(
        {
            **{
                key: payload.get(key)
                for key in (
                    "composeId",
                    "environmentId",
                    "serverId",
                    "name",
                    "sourceType",
                    "composePath",
                    "autoDeploy",
                )
            },
            "composeFile": compose.compose_file_sha256(str(payload.get("composeFile") or "")),
            "env": api.parse_dokploy_env_text(str(payload.get("env") or "")),
            # Keep historical unmarked custody hashes recoverable. New literal
            # transport must also bind its wire form, which changes consumption.
            **(
                {"env_wire_sha256": canonical_json_sha256(str(payload.get("env") or ""))}
                if api.compose_environment_is_encoded(str(payload.get("env") or ""))
                else {}
            ),
        }
    )


def observe_candidate_configuration(
    *,
    root: Path,
    store: PostgresRecordStore,
    record: TestingCandidateAllocation,
    lease: ProviderOperationLease,
    guard: Callable[[], None],
    retain_configured: bool = False,
) -> TestingCandidateAllocation:
    """Observe the original private outcome; never select inputs or send an update."""
    app = record.application
    if app is None or not app.configuration_only:
        raise CandidateObservationUnavailable("The original configuration custody is unavailable.")
    lease.assert_current()
    guard()
    require_candidate_target_custody(store, record)
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=record.compose_id
    )
    if fetch_dokploy_compose_domains_for_target_setup(
        host=host, token=token, compose_id=record.compose_id
    ) or api.list_deployments_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=record.compose_id,
        require_complete=True,
    ):
        raise CandidateObservationUnavailable("The original private configuration remains unknown.")
    if (
        app.configuration_sha256
        and candidate_configuration_sha256(payload) == app.configuration_sha256
    ):
        observed_app = app.model_copy(
            update={
                "phase": "abandoned",
                "abandonment_evidence": app.retained_configuration_evidence,
            }
            if retain_configured
            else {"phase": "configured"}
        )
    else:
        try:
            if app.phase != "configuring":
                raise ValueError("A configured copy no longer matches its original update.")
            require_empty_compose_source(payload, record.compose_id)
            if (
                (payload.get("serverId"), payload.get("environmentId"))
                != (record.server_id, record.environment_id)
                or payload.get("name") != record.namespace
                or not re.fullmatch(
                    re.escape(record.namespace) + r"(?:-[a-z0-9]{6})?",
                    str(payload.get("appName") or ""),
                )
                or payload.get("env") not in (None, "")
            ):
                raise ValueError("The original empty configuration changed.")
        except ValueError:
            raise CandidateObservationUnavailable(
                "The original private configuration remains unknown."
            ) from None
        # A late original update may still install the full credential-bearing
        # configuration in this retained private copy. This is not cancellation.
        # Its lease/CAS cannot advance an abandoned copy to deployment.
        observed_app = app.model_copy(
            update={
                "phase": "abandoned",
                "abandonment_evidence": app.unapplied_configuration_evidence,
            }
        )
    lease.assert_current()
    guard()
    updated = record.model_copy(update={"application": observed_app})
    store.compare_and_write_testing_candidate(record, updated, observation_only=True)
    return updated


def prepare_candidate_application(
    *,
    root: Path,
    store: PostgresRecordStore,
    allocation: TestingCandidateAllocation,
    inputs: TestingCandidateRuntimeInputs,
    lease: ProviderOperationLease,
    guard: Callable[[], None],
) -> TestingCandidateAllocation:
    """Resume through observations of the original effects, never blind redispatch.

    The service caller owns lane authorization/holds/use and the exclusive lease.
    It supplies freshly resolved inputs on every continuation. Failed preparation
    leaves the old lane serving and retains this copy's custody for abandonment.
    """
    existing = store.read_testing_candidate(allocation.namespace)
    if existing is None:
        raise ValueError("Candidate allocation is unavailable.")
    record: TestingCandidateAllocation = existing
    record.require_successor(allocation)
    if record.application is not None and record.application.plan != inputs.plan:
        raise ValueError("Candidate application intent changed.")

    def checkpoint() -> None:
        lease.assert_current()
        guard()
        require_candidate_target_custody(store, record)
        if fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=record.compose_id
        ):
            raise ValueError("Candidate acquired a public route before adoption.")

    def save(**changes: object) -> None:
        nonlocal record
        checkpoint()
        app = record.application or TestingCandidateApplication(plan=inputs.plan)
        updated = TestingCandidateApplication.model_validate({**app.model_dump(), **changes})
        next_record = TestingCandidateAllocation.model_validate(
            {**record.model_dump(), "application": updated.model_dump()}
        )
        store.compare_and_write_testing_candidate(record, next_record)
        record = next_record

    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    checkpoint()
    if record.application is None:
        save()
    app = record.application
    assert app is not None
    if app.phase == "abandoned":
        raise ValueError("An abandoned candidate cannot run again.")
    if app.phase == "prepared":
        candidate_private_readiness(
            host=host,
            token=token,
            allocation=record,
            inputs=inputs,
            deployment_id=app.deployment_id,
        )
        return record

    def payload() -> api.JsonObject:
        checkpoint()
        return api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.compose_id
        )

    if app.phase in {"planned", "configuring"}:
        current = payload()
        if app.phase == "planned":
            require_empty_compose_source(current, record.compose_id)
            if (
                (current.get("serverId"), current.get("environmentId"))
                != (record.server_id, record.environment_id)
                or current.get("env") not in (None, "")
                or api.list_deployments_for_target(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=record.compose_id,
                    require_complete=True,
                )
            ):
                raise ValueError("Candidate is no longer unused.")
            environment = api.serialize_dokploy_env_text(
                inputs.runtime_environment,
                target_type="compose",
                compose_type=str(current.get("composeType") or "docker-compose"),
            )
            api.refuse_app_runtime_env_write(
                env_text=environment, current_env_text="", target="testing candidate"
            )
            compose.validate_odoo_raw_compose_file(compose_file=inputs.compose_file)
            update_payload: api.JsonObject = {
                "composeId": record.compose_id,
                "name": record.namespace,
                "environmentId": record.environment_id,
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": inputs.compose_file,
                "autoDeploy": False,
                "env": environment,
            }
            save(
                phase="configuring",
                configuration_sha256=candidate_configuration_sha256(
                    {**update_payload, "serverId": record.server_id}
                ),
            )
            lease.checkpoint_effect("candidate_configuration")
            checkpoint()
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/compose.update",
                method="POST",
                payload=update_payload,
            )
            current = payload()
        # A lost update reply is recovered only when all intended values match.
        require_candidate_configuration(record, inputs, current)
        save(phase="configured")
    app = record.application
    assert app is not None
    title = "Launchplane testing candidate " + inputs.plan.digest
    if app.phase in {"configured", "deploying"}:
        require_candidate_configuration(record, inputs, payload())
        deployments = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=record.compose_id,
            require_complete=True,
        )
        if app.phase == "configured":
            if deployments:
                raise ValueError("Candidate has unexpected deployment history.")
            save(phase="deploying")
            lease.checkpoint_effect("candidate_deployment")
            checkpoint()
            api.trigger_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=record.compose_id,
                no_cache=False,
                title=title,
            )
            deployments = api.list_deployments_for_target(
                host=host,
                token=token,
                target_type="compose",
                target_id=record.compose_id,
                require_complete=True,
            )
        matching = [item for item in deployments if item.get("title") == title]
        if len(matching) != 1 or len(deployments) != 1:
            raise CandidateObservationUnavailable(
                "Candidate original deployment is absent or ambiguous; do not redispatch."
            )
        deployment_id = api.deployment_key(matching[0])
        if not deployment_id:
            raise CandidateObservationUnavailable("Candidate deployment identity is unavailable.")
        if api.deployment_status(matching[0]) in api._DEPLOYMENT_FAILURE_STATUSES:
            raise api.DokployDeploymentFailed(
                deployment_id=deployment_id,
                deployment_status=api.deployment_status(matching[0]),
                message_prefix="Candidate original build failed",
                deployment=matching[0],
            )
        api.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=record.compose_id,
            before_key="",
            timeout_seconds=inputs.target.deploy_timeout_seconds or 600,
            deployment_title=title,
        )
        save(phase="deployed", deployment_id=deployment_id)
    app = record.application
    assert app is not None
    if app.phase == "deployed":
        save(phase="restoring", restore_workflow_started=False)
    app = record.application
    assert app is not None
    if (
        app.phase == "restoring"
        and app.restore_workflow_started is False
        and not app.restore_schedule_id
    ):

        def before_mutation(phase: str) -> None:
            checkpoint()
            if phase in {"post_deploy_target_update", "post_deploy_deploy_trigger"}:
                raise ValueError("Candidate runtime inputs must be complete before deployment.")
            if phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER:
                target_payload = payload()
                schedule_type, lookup_id, _, _ = post_deploy._resolve_dokploy_schedule_runtime(
                    host=host,
                    token=token,
                    compose_id=record.compose_id,
                    compose_name=record.namespace,
                    target_payload=target_payload,
                )
                schedule = api.find_matching_dokploy_schedule(
                    host=host,
                    token=token,
                    target_id=lookup_id,
                    schedule_type=schedule_type,
                    schedule_name=post_deploy.DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME,
                    app_name=record.namespace,
                )
                schedule_id = api.schedule_key(schedule) if schedule is not None else ""
                if not schedule_id or post_deploy._has_running_schedule_deployment(schedule):
                    raise ValueError("Candidate restore schedule is unavailable or already active.")
                # The provider cannot trigger until this callback returns.
                # Complete every refusing check, including the effect lease,
                # before one CAS records the original dispatch receipt.
                lease.checkpoint_effect(phase)
                save(
                    restore_workflow_started=True,
                    restore_schedule_id=schedule_id,
                    restore_previous_deployments=tuple(
                        api.deployment_key(item)
                        for item in post_deploy._schedule_deployments(schedule)
                    ),
                )
                return
            lease.checkpoint_effect(phase)

        def native(spec: dict[str, object], server_id: str) -> dict[str, object]:
            generation = inputs.plan.generation
            descriptor = inputs.native_source
            if generation is None or descriptor is None or server_id != record.server_id:
                raise ValueError("Candidate native receiver differs from its admitted source.")
            return prepare_native_testing_delivery(
                store,
                root=root,
                generation=generation,
                descriptor=descriptor,
                host=host,
                token=token,
                server_id=server_id,
                spec=spec,
                before_mutation=before_mutation,
            ).model_dump(mode="json")

        evidence = post_deploy.run_compose_post_deploy_update(
            host=host,
            token=token,
            target_definition=inputs.target,
            env_file=None,
            workflow_environment_overrides=inputs.workflow_environment,
            required_workflow_environment_keys=inputs.required_workflow_keys,
            run_destructive_restore=inputs.plan.source != "empty",
            bootstrap_missing_database=inputs.plan.source == "empty",
            before_provider_mutation=before_mutation,
            deployment_title=title,
            fresh_testing_generation=inputs.plan.generation,
            fresh_restore_source_environment=inputs.plan.upstream_source,
            prepare_native_source=native if inputs.native_source else None,
            testing_candidate_namespace=record.namespace,
        )
        save(workflow_evidence=evidence)
    # On an interrupted restore, inspect the actual copy/gate/credentials instead
    # of creating another schedule or re-running module hooks.
    app = record.application
    assert app is not None
    checkpoint()
    candidate_restore_terminal(host=host, token=token, record=record, require_success=True)
    prepared = candidate_private_readiness(
        host=host, token=token, allocation=record, inputs=inputs, deployment_id=app.deployment_id
    )
    save(phase="prepared", prepared=prepared)
    return record


def candidate_restore_terminal(
    *,
    host: str,
    token: str,
    record: TestingCandidateAllocation,
    require_success: bool,
) -> str:
    """Prove exactly one original restore finished; an absent request stays unknown."""
    app = record.application
    if app is None or not app.restore_schedule_id:
        raise CandidateObservationUnavailable(
            "Candidate restore has no original schedule dispatch receipt."
        )
    payload = api.dokploy_request(
        host=host,
        token=token,
        path="/api/deployment.allByType",
        query={"id": app.restore_schedule_id, "type": "schedule"},
    )
    history = api.parse_deployment_history(payload)
    if history.state == "unknown":
        raise CandidateObservationUnavailable("Candidate restore history is unavailable.")
    deployments = api.extract_deployments(payload)
    keys = [api.deployment_key(item) for item in deployments]
    if not all(keys) or len(set(keys)) != len(keys):
        raise CandidateObservationUnavailable("Candidate restore history is ambiguous.")
    new = [
        item
        for item in deployments
        if api.deployment_key(item) not in app.restore_previous_deployments
    ]
    if len(new) != 1:
        raise CandidateObservationUnavailable("Candidate original restore is absent or ambiguous.")
    status = api.deployment_status(new[0])
    if status not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES:
        raise CandidateObservationUnavailable("Candidate restore is still running or unknown.")
    if require_success and status not in api._DEPLOYMENT_SUCCESS_STATUSES:
        raise ValueError("Candidate restore failed; keep the old copy serving.")
    return app.restore_schedule_id + ":" + api.deployment_key(new[0]) + ":" + status


def candidate_build_terminal(*, host: str, token: str, record: TestingCandidateAllocation) -> str:
    app = record.application
    if (
        app is None
        or app.restore_schedule_id
        or not (
            app.phase == "deploying"
            or app.phase == "restoring"
            and app.restore_workflow_started is False
        )
    ):
        raise ValueError("Build abandonment requires the original pre-restore deployment intent.")
    deployments = api.list_deployments_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=record.compose_id,
        require_complete=True,
    )
    if (
        len(deployments) != 1
        or deployments[0].get("title") != "Launchplane testing candidate " + app.plan.digest
    ):
        raise CandidateObservationUnavailable(
            "Original candidate build history is absent or ambiguous."
        )
    key, status = api.deployment_key(deployments[0]), api.deployment_status(deployments[0])
    if not key or status not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES:
        raise CandidateObservationUnavailable(
            "The original candidate build is still running or unknown."
        )
    return key + ":" + status


def candidate_terminal_custody_evidence(
    *,
    root: Path,
    store: PostgresRecordStore,
    record: TestingCandidateAllocation,
) -> str:
    """Read original terminal jobs and retained private custody without changing the copy."""
    if record.application is None:
        raise ValueError("Candidate application is unavailable.")
    app = record.application
    require_candidate_target_custody(store, record)
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    if fetch_dokploy_compose_domains_for_target_setup(
        host=host, token=token, compose_id=record.compose_id
    ):
        raise ValueError("A publicly routed candidate cannot be abandoned as private.")
    if app.phase not in {"deploying", "restoring", "prepared"}:
        raise ValueError("Observe the original candidate effects before abandonment.")
    evidence = (
        candidate_build_terminal(host=host, token=token, record=record)
        if app.phase == "deploying"
        or app.phase == "restoring"
        and app.restore_workflow_started is False
        and not app.restore_schedule_id
        else candidate_restore_terminal(
            host=host, token=token, record=record, require_success=False
        )
    )
    # No new deployment may have started after the admitted candidate build.
    deployments = api.list_deployments_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=record.compose_id,
        require_complete=True,
    )
    if (
        len(deployments) != 1
        or (app.deployment_id and api.deployment_key(deployments[0]) != app.deployment_id)
        or deployments[0].get("title") != "Launchplane testing candidate " + app.plan.digest
        or api.deployment_status(deployments[0])
        not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
    ):
        raise ValueError("Candidate deployment is still running or changed.")
    return evidence


def abandon_candidate_application(
    *,
    root: Path,
    store: PostgresRecordStore,
    namespace: str,
    lease: ProviderOperationLease,
    guard: Callable[[], None],
) -> TestingCandidateAllocation:
    """Retain failed-copy custody; never cancel unknown jobs, stop a copy or delete storage."""
    lease.assert_current()
    guard()
    record = store.read_testing_candidate(namespace)
    if record is None or record.application is None:
        raise ValueError("Candidate application is unavailable.")
    app = record.application
    if app.phase == "abandoned":
        return record
    evidence = candidate_terminal_custody_evidence(root=root, store=store, record=record)
    lease.assert_current()
    guard()
    require_candidate_target_custody(store, record)
    updated_app = app.model_copy(update={"phase": "abandoned", "abandonment_evidence": evidence})
    updated = TestingCandidateAllocation.model_validate(
        {**record.model_dump(), "application": updated_app.model_dump()}
    )
    store.compare_and_write_testing_candidate(record, updated)
    return updated
