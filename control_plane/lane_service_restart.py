"""Restart an identity-bound service under the existing lane and provider fences."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
import time

import click

from control_plane.client_release import read_client_release_run
from control_plane.contracts.lane_service_restart import (
    LaneServiceRestartPlan,
    LaneServiceRestartRequest,
    LaneServiceRestartResult,
    RestartContainerIdentity,
)
from control_plane.contracts.deployment_record import deployment_record_passed
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy import api, source
from control_plane.dokploy.service_restart import (
    ServiceInspectionUnavailable,
    inspect_service,
    restart_container,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.runtime_identity_health import (
    wait_for_runtime_identity_healthcheck_with_retry,
)


class ServiceRestartRefused(ValueError):
    """A fixed, credential-free diagnosis; no provider write has occurred."""


def plan_service_restart(
    *,
    store: PostgresRecordStore,
    root: Path,
    request: LaneServiceRestartRequest,
    actor: str,
) -> tuple[LaneServiceRestartPlan, RuntimeIdentity, str, str, str]:
    profile = store.read_product_profile_record(request.product)
    lane = next(
        (
            lane
            for lane in profile.lanes
            if (lane.context == request.context and lane.instance == request.instance)
        ),
        None,
    )
    if not profile.is_active or lane is None:
        raise ServiceRestartRefused("The requested active product lane is not recorded.")
    if request.service == "web" and not lane.health_url.strip():
        raise ServiceRestartRefused("The lane's HTTP runtime identity endpoint is unavailable.")
    # These lanes have a shared mutex respected by release enqueues, workers and
    # synchronous releases. Other drivers must supply equivalent exclusion first.
    if profile.driver_id != "odoo":
        raise ServiceRestartRefused(
            "This driver does not support service restart with release exclusion."
        )
    target = store.read_provider_target_record(
        context_name=request.context, instance_name=request.instance
    )
    legacy = store.read_dokploy_target_record(
        context_name=request.context, instance_name=request.instance
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name=request.instance
    )
    if not (
        target.provider_id == "dokploy"
        and target.provider_target_type == "compose"
        and legacy.target_type == "compose"
        and target.target_id == target_id.target_id
        and target.display_name == legacy.target_name
    ):
        raise ServiceRestartRefused("The recorded provider target is ambiguous or unsupported.")
    deployments = store.list_deployment_records(
        context_name=request.context, instance_name=request.instance, limit=1
    )
    if not deployments or not deployment_record_passed(deployments[0]):
        raise ServiceRestartRefused("The lane has no settled current deployment.")
    deployment = deployments[0]
    expected = deployment.runtime_identity
    if (
        expected is None
        or expected.product != request.product
        or expected.context != request.context
        or expected.instance != request.instance
        or expected.deployment_record_id != deployment.record_id
        or deployment.deployed_target is None
        or deployment.deployed_target.target_id != target.target_id
        or deployment.artifact_identity is None
        or deployment.artifact_identity.artifact_id != expected.artifact_id
    ):
        raise ServiceRestartRefused("Current deployment identity is ambiguous.")
    manifest = store.read_artifact_manifest(expected.artifact_id)
    if expected.image_reference != f"{manifest.image.repository}@{manifest.image.digest}":
        raise ServiceRestartRefused("Current artifact image identity is ambiguous.")
    if expected.source_git_ref != manifest.source_commit:
        raise ServiceRestartRefused("Current artifact source identity is ambiguous.")
    acceptance_id = ""
    if request.instance == "prod":
        decisions = store.list_release_review_decision_records(product=request.product)
        for decision in decisions:
            run = read_client_release_run(store=store, profile=profile, decision=decision)
            if run is not None and run.state in {"running", "waiting"}:
                raise ServiceRestartRefused("A Client release run holds this lane.")
            if (
                decision.decision == "accepted"
                and decision.checklist.candidate.artifact_id == expected.artifact_id
                and decision.checklist.candidate.source_commit == manifest.source_commit
            ):
                acceptance_id = decision.record_id
        if not acceptance_id:
            raise ServiceRestartRefused(
                "The current production artifact has no recorded acceptance."
            )
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target.target_id
    )
    app_name = str(payload.get("appName") or "")
    server_id = str(payload.get("serverId") or "")
    if payload.get("composeId") != target.target_id or not app_name:
        raise ServiceRestartRefused("The live provider target identity is ambiguous.")
    before = inspect_service(
        host=host,
        token=token,
        app_name=app_name,
        server_id=server_id,
        service=request.service,
        expected=expected,
    )
    if before.health == "unavailable":
        raise ServiceRestartRefused("The service has no supported container health check.")
    return (
        LaneServiceRestartPlan(
            product=request.product,
            context=request.context,
            instance=request.instance,
            service=request.service,
            actor=actor,
            reason=api.redact_dokploy_log_line(request.reason),
            driver_id=profile.driver_id,
            target_id=target.target_id,
            app_name=app_name,
            server_id=server_id,
            artifact_id=expected.artifact_id,
            deployment_record_id=deployment.record_id,
            acceptance_record_id=acceptance_id,
            before=before,
        ),
        expected,
        host,
        token,
        lane.health_url,
    )


class ServiceRestartAdapter:
    def __init__(
        self,
        *,
        store: PostgresRecordStore,
        root: Path,
        request: LaneServiceRestartRequest,
        plan: LaneServiceRestartPlan,
        expected: RuntimeIdentity,
        host: str,
        token: str,
        health_url: str,
        trace_id: str,
    ) -> None:
        self.store = store
        self.root = root
        self.request = request
        self.plan = plan
        self.expected = expected
        self.host = host
        self.token = token
        self.health_url = health_url
        self.trace_id = trace_id

    def target_key(self) -> str:
        # Fence the immutable recipient of the POST. An old uncertain request
        # cannot block a replacement container or affect that new recipient.
        return (
            "lane-service-restart-target:"
            + hashlib.sha256(
                json.dumps(
                    [
                        self.plan.context,
                        self.plan.instance,
                        self.plan.target_id,
                        self.plan.before.container_id,
                    ]
                ).encode()
            ).hexdigest()
        )

    def reconciliation_key(self) -> str:
        # The durable reservation retains before/actor/reason even if the provider
        # reply or subsequent evidence write is lost. It contains no env values.
        return self.plan.model_dump_json()

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # An unobserved restart cannot be distinguished from somebody else's
        # restart. Preserve it for reconciliation instead of repeating the effect.
        return ProviderObservation(outcome="unknown")

    def observe_with_effect_started_at(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        provider_effect_started_at: str,
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation(outcome="unknown")
        try:
            fresh, *_ = plan_service_restart(
                store=self.store, root=self.root, request=self.request, actor=self.plan.actor
            )
            after = fresh.before
            # A full Docker container ID is immutable. A verified later deploy
            # with a different sole service container proves the old POST cannot
            # affect the current container, without guessing the old outcome.
            if (
                all(
                    getattr(fresh, field) == getattr(self.plan, field)
                    for field in ("target_id", "app_name", "server_id")
                )
                and fresh.deployment_record_id != self.plan.deployment_record_id
                and after.container_id != self.plan.before.container_id
            ):
                result = LaneServiceRestartResult(
                    status="unknown",
                    plan=self.plan,
                    plan_sha256=self.plan.digest(),
                    error_message=f"Original restart remains unknown. Verified deployment {fresh.deployment_record_id} replaced its container; no additional restart was dispatched.",
                )
                return ProviderObservation(
                    outcome="present",
                    response_status_code=200,
                    response_payload=provider_operation_response_payload(
                        trace_id=self.trace_id,
                        records={"superseding_deployment_record_id": fresh.deployment_record_id},
                        result=result.model_dump(mode="json"),
                    ),
                )
            # Observation never changes the service. If a later deploy, config
            # change or release intervened, retain the original unknown outcome.
            if any(
                getattr(fresh, field) != getattr(self.plan, field)
                for field in (
                    "target_id",
                    "app_name",
                    "server_id",
                    "artifact_id",
                    "deployment_record_id",
                )
            ) or any(
                getattr(after, field) != getattr(self.plan.before, field)
                for field in (
                    "container_id",
                    "image_id",
                    "image_reference",
                    "configuration_sha256",
                    "runtime_identity_sha256",
                )
            ):
                return ProviderObservation(outcome="unknown")
            if not provider_effect_phase and not provider_effect_started_at:
                status, message = (
                    "fail",
                    "The interrupted request stopped before its restart checkpoint. No restart was dispatched.",
                )
            elif (
                provider_effect_phase == "restart_container"
                and after.running
                and after.health == "healthy"
                and datetime.fromisoformat(after.started_at.replace("Z", "+00:00"))
                > max(
                    datetime.fromisoformat(self.plan.before.started_at.replace("Z", "+00:00")),
                    datetime.fromisoformat(provider_effect_started_at.replace("Z", "+00:00")),
                )
            ):
                if self.plan.service == "web":
                    wait_for_runtime_identity_healthcheck_with_retry(
                        url=self.health_url,
                        timeout_seconds=10,
                        expected_runtime_identity=self.expected,
                        sleep=time.sleep,
                        monotonic=time.monotonic,
                    )
                status, message = (
                    "pass",
                    "Observed the healthy restarted service after the original checkpoint; no additional restart was dispatched.",
                )
            else:
                return ProviderObservation(outcome="unknown")
            result = LaneServiceRestartResult(
                status="pass" if status == "pass" else "fail",
                plan=self.plan,
                plan_sha256=self.plan.digest(),
                after=after,
                error_message=message,
            )
            return ProviderObservation(
                outcome="present",
                response_status_code=200,
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=result.model_dump(mode="json")
                ),
            )
        except (ValueError, click.ClickException, OSError):
            return ProviderObservation(outcome="unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.store.odoo_synchronous_lane_reservation(
            product=self.plan.product, context=self.plan.context, instance=self.plan.instance
        ) as owner:
            if owner is not None:
                raise ProviderMutationRejectedError(
                    ServiceRestartRefused("A release operation holds this lane.")
                )
            try:
                fresh, *_ = plan_service_restart(
                    store=self.store, root=self.root, request=self.request, actor=self.plan.actor
                )
                if fresh.digest() != self.request.reviewed_plan_sha256:
                    raise ServiceRestartRefused(
                        "Restart identity changed; inspect and review again."
                    )
            except (ValueError, click.ClickException, OSError) as error:
                raise ProviderMutationRejectedError(
                    ServiceRestartRefused(
                        "Restart identity or release ownership changed; inspect and review again."
                    )
                ) from error
            lease.checkpoint_effect("restart_container")
            try:
                restart_container(
                    host=self.host,
                    token=self.token,
                    container_id=self.plan.before.container_id,
                    server_id=self.plan.server_id,
                )
            except api.DokployRequestFailed as error:
                if (
                    error.status_code is not None
                    and 400 <= error.status_code < 500
                    and not error.retryable
                    and not error.remote_command_failed
                ):
                    result = LaneServiceRestartResult(
                        status="fail",
                        plan=self.plan,
                        plan_sha256=self.plan.digest(),
                        error_message="The provider rejected this restart request.",
                    )
                    return ProviderMutationOutcome(
                        response_status_code=200,
                        response_payload=provider_operation_response_payload(
                            trace_id=self.trace_id,
                            records={},
                            result=result.model_dump(mode="json"),
                        ),
                    )
                raise ProviderMutationUnknownError(
                    "Container restart outcome is unknown; do not repeat it."
                ) from error
            except (ValueError, click.ClickException, OSError) as error:
                raise ProviderMutationUnknownError(
                    "Container restart outcome is unknown; do not repeat it."
                ) from error
            after: RestartContainerIdentity | None = None
            status: str = "fail"
            message = "Restart completed, but health or runtime identity did not verify."
            deadline = time.monotonic() + 90
            try:
                while time.monotonic() < deadline:
                    lease.assert_current()
                    try:
                        after = inspect_service(
                            host=self.host,
                            token=self.token,
                            app_name=self.plan.app_name,
                            server_id=self.plan.server_id,
                            service=self.plan.service,
                            expected=self.expected,
                        )
                    except ServiceInspectionUnavailable:
                        time.sleep(1)
                        continue
                    except api.DokployRequestFailed as error:
                        if not api.is_transient_dokploy_error(error):
                            raise
                        time.sleep(1)
                        continue
                    for field in (
                        "container_id",
                        "image_id",
                        "image_reference",
                        "configuration_sha256",
                        "runtime_identity_sha256",
                    ):
                        if getattr(after, field) != getattr(self.plan.before, field):
                            raise ValueError("Runtime changed during restart.")
                    if (
                        after.running
                        and after.health == "healthy"
                        and datetime.fromisoformat(after.started_at.replace("Z", "+00:00"))
                        > datetime.fromisoformat(self.plan.before.started_at.replace("Z", "+00:00"))
                    ):
                        if self.plan.service == "web":
                            if not self.health_url:
                                raise ValueError("Lane health URL is unavailable.")
                            wait_for_runtime_identity_healthcheck_with_retry(
                                url=self.health_url,
                                timeout_seconds=max(1, int(deadline - time.monotonic())),
                                expected_runtime_identity=self.expected,
                                sleep=time.sleep,
                                monotonic=time.monotonic,
                            )
                        status, message = "pass", ""
                        break
                    time.sleep(1)
            except (ValueError, click.ClickException, OSError):
                pass
            result = LaneServiceRestartResult(
                status="pass" if status == "pass" else "fail",
                plan=self.plan,
                plan_sha256=self.plan.digest(),
                after=after,
                error_message=message,
            )
            return ProviderMutationOutcome(
                response_status_code=200,
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=result.model_dump(mode="json")
                ),
            )
