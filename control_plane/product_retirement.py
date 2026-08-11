from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol, cast

import click

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_retirement import (
    MAX_PRODUCT_RETIREMENT_ERROR_MESSAGE_LENGTH,
    ProductRetirementAuthoritySnapshot,
    ProductRetirementIdentity,
    ProductRetirementMutationEvidence,
    ProductRetirementProviderObservation,
    ProductRetirementRecord,
    ProductRetirementRequest,
    ProductRetirementOutcome,
    build_product_retirement_record_id,
    canonical_sha256,
    provider_identifier_sha256,
)
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.secret_record import SecretRecord
from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    build_provider_operation_key,
)


_ACTIVE_PREVIEW_STATES = frozenset({"pending", "active", "paused", "teardown_pending"})
_NO_DEPLOYMENT_HISTORY_STATUS = "no_history"
_RETIRABLE_APPLICATION_STATES = frozenset({"completed", "done", "exited", "idle", "stopped"})
_RETIRABLE_DEPLOYMENT_STATES = frozenset(
    {"cancelled", "canceled", "completed", "done", "failed", "idle", "skipped", "success"}
)


class ProductRetirementBlockedError(ValueError):
    pass


class ProductRetirementStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def compare_and_write_product_profile_record(
        self,
        *,
        expected_record: LaunchplaneProductProfileRecord,
        replacement_record: LaunchplaneProductProfileRecord,
    ) -> object: ...

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...

    def write_secret_record(self, record: SecretRecord) -> object: ...

    def write_secret_binding(self, binding: SecretBinding) -> object: ...

    def write_secret_audit_event(self, event: SecretAuditEvent) -> object: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def delete_provider_target_record(self, *, expected_record: ProviderTargetRecord) -> object: ...

    def delete_dokploy_target_record(self, *, expected_record: DokployTargetRecord) -> object: ...

    def delete_dokploy_target_id_record(
        self, *, expected_record: DokployTargetIdRecord
    ) -> object: ...

    def delete_runtime_environment_record_with_event(
        self,
        *,
        expected_record: RuntimeEnvironmentRecord,
        event: RuntimeEnvironmentDeleteEvent,
    ) -> object: ...

    def write_product_retirement_record(self, record: ProductRetirementRecord) -> object: ...

    def read_product_retirement_record(self, record_id: str) -> ProductRetirementRecord: ...

    def list_product_retirement_records(
        self,
        *,
        product: str = "",
        actor: str = "",
        mode: str = "",
        idempotency_key: str = "",
        limit: int | None = None,
    ) -> tuple[ProductRetirementRecord, ...]: ...


class BoundProductRetirement:
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord,
        context: str,
        provider_target: ProviderTargetRecord,
        dokploy_target: DokployTargetRecord,
        dokploy_target_id: DokployTargetIdRecord,
        runtime_records: tuple[RuntimeEnvironmentRecord, ...],
        secret_records: tuple[SecretRecord, ...],
    ) -> None:
        self.profile = profile
        self.context = context
        self.provider_target = provider_target
        self.dokploy_target = dokploy_target
        self.dokploy_target_id = dokploy_target_id
        self.runtime_records = runtime_records
        self.secret_records = secret_records


def bind_product_retirement_authority(
    *, record_store: ProductRetirementStore, request: ProductRetirementRequest
) -> BoundProductRetirement:
    profile = record_store.read_product_profile_record(request.product)
    if profile.driver_id.strip() != "generic-web":
        raise ProductRetirementBlockedError(
            "Product retirement supports only exact generic-web profiles."
        )
    if profile.lifecycle_state not in {"active", "retiring"}:
        raise ProductRetirementBlockedError("Product profile is already retired.")
    matching_lanes = tuple(
        lane for lane in profile.lanes if lane.instance.strip() == request.instance
    )
    if len(matching_lanes) != 1:
        raise ProductRetirementBlockedError(
            "Product retirement requires one exact profile-owned stable instance."
        )
    lane = matching_lanes[0]
    context = lane.context.strip()
    route_owners = tuple(
        candidate.product
        for candidate in record_store.list_product_profile_records()
        if candidate.product != profile.product
        and any(
            candidate_lane.context.strip() == context
            and candidate_lane.instance.strip() == request.instance
            for candidate_lane in candidate.lanes
        )
    )
    if route_owners:
        raise ProductRetirementBlockedError(
            "Product retirement route is ambiguous across active product profiles."
        )
    provider_target = record_store.read_provider_target_record(
        context_name=context,
        instance_name=request.instance,
    )
    dokploy_target = record_store.read_dokploy_target_record(
        context_name=context,
        instance_name=request.instance,
    )
    dokploy_target_id = record_store.read_dokploy_target_id_record(
        context_name=context,
        instance_name=request.instance,
    )
    target_ids = {
        provider_target.target_id.strip(),
        dokploy_target_id.target_id.strip(),
    }
    if (
        provider_target.provider_id != "dokploy"
        or provider_target.target_category != "application"
        or provider_target.provider_target_type != "application"
        or dokploy_target.target_type != "application"
        or len(target_ids) != 1
        or request.expected_target_sha256 != provider_identifier_sha256(provider_target.target_id)
    ):
        raise ProductRetirementBlockedError(
            "Product retirement requires exact tracked Dokploy application authority."
        )
    runtime_records = record_store.list_runtime_environment_records(
        context_name=context,
        instance_name=request.instance,
    )
    secret_records = record_store.list_secret_records(
        context_name=context,
        instance_name=request.instance,
    )
    if active_preview_ids(record_store=record_store, profile=profile):
        raise ProductRetirementBlockedError(
            "Product retirement is blocked by active preview evidence."
        )
    return BoundProductRetirement(
        profile=profile,
        context=context,
        provider_target=provider_target,
        dokploy_target=dokploy_target,
        dokploy_target_id=dokploy_target_id,
        runtime_records=runtime_records,
        secret_records=secret_records,
    )


def observe_tracked_dokploy_application(
    *,
    control_plane_root: Path,
    target_id: str,
    observed_at: str,
) -> ProductRetirementProviderObservation:
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    try:
        payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application",
            target_id=target_id,
        )
    except dokploy_api.DokployRequestFailed as error:
        if error.status_code != 404:
            raise
        return ProductRetirementProviderObservation(
            observed_at=observed_at,
            target_id=target_id,
            target_id_sha256=provider_identifier_sha256(target_id),
            state="absent",
            retirable=False,
        )
    domains = dokploy_api.fetch_dokploy_application_domains(
        host=host,
        token=token,
        application_id=target_id,
    )
    deployment_history = dokploy_api.deployment_history_for_target(
        host=host,
        token=token,
        target_type="application",
        target_id=target_id,
    )
    return build_provider_observation(
        target_id=target_id,
        payload=payload,
        domains=domains,
        latest_deployment=deployment_history.latest_deployment,
        deployment_history_state=deployment_history.state,
        observed_at=observed_at,
    )


def build_provider_observation(
    *,
    target_id: str,
    payload: Mapping[str, object],
    domains: tuple[Mapping[str, object], ...],
    latest_deployment: Mapping[str, object] | None,
    deployment_history_state: dokploy_api.DeploymentHistoryState = "present",
    observed_at: str,
) -> ProductRetirementProviderObservation:
    observed_target_id = _application_id(payload)
    if observed_target_id != target_id.strip():
        raise ProductRetirementBlockedError(
            "Provider observation does not match tracked application authority."
        )
    domain_evidence = tuple(
        sorted(
            (
                _domain_id(domain),
                str(domain.get("host") or domain.get("domain") or "").strip().lower(),
            )
            for domain in domains
            if _domain_id(domain)
        )
    )
    application_state = (
        str(
            payload.get("applicationStatus")
            or payload.get("application_status")
            or payload.get("status")
            or ""
        )
        .strip()
        .lower()
    )
    if deployment_history_state == "no_history":
        if latest_deployment is not None:
            raise ProductRetirementBlockedError(
                "No deployment history cannot include a latest deployment."
            )
        deployment_status = _NO_DEPLOYMENT_HISTORY_STATUS
    elif deployment_history_state == "present":
        deployment_status = dokploy_api.deployment_status(
            cast(dokploy_api.JsonObject | None, latest_deployment)
        )
    else:
        deployment_status = ""
    retirable = application_state in _RETIRABLE_APPLICATION_STATES and (
        deployment_status == _NO_DEPLOYMENT_HISTORY_STATUS
        or deployment_status in _RETIRABLE_DEPLOYMENT_STATES
    )
    core_payload = {
        "application_id": observed_target_id,
        "name": str(payload.get("name") or "").strip(),
        "app_name": str(payload.get("appName") or "").strip(),
        "project_id": str(payload.get("projectId") or "").strip(),
        "environment_id": str(payload.get("environmentId") or "").strip(),
        "server_id": str(payload.get("serverId") or "").strip(),
        "source_type": str(payload.get("sourceType") or "").strip(),
    }
    return ProductRetirementProviderObservation(
        observed_at=observed_at,
        target_id=observed_target_id,
        target_id_sha256=provider_identifier_sha256(observed_target_id),
        state="present",
        application_fingerprint_sha256=canonical_sha256(core_payload),
        application_name_sha256=canonical_sha256(
            {"name": core_payload["name"], "app_name": core_payload["app_name"]}
        ),
        project_reference_sha256=canonical_sha256(
            {
                "project_id": core_payload["project_id"],
                "environment_id": core_payload["environment_id"],
                "server_id": core_payload["server_id"],
            }
        ),
        domain_ids=tuple(item[0] for item in domain_evidence),
        domain_id_sha256=tuple(provider_identifier_sha256(item[0]) for item in domain_evidence),
        domain_host_sha256=tuple(canonical_sha256(item[1]) for item in domain_evidence),
        deployment_status=deployment_status,
        retirable=retirable,
    )


def authority_snapshot(bound: BoundProductRetirement) -> ProductRetirementAuthoritySnapshot:
    runtime_refs = tuple(_runtime_ref(record) for record in bound.runtime_records)
    secret_refs, secret_sha256 = _secret_snapshot(bound.secret_records)
    return ProductRetirementAuthoritySnapshot(
        context=bound.context,
        profile_sha256=canonical_sha256(bound.profile.model_dump(mode="json")),
        profile_updated_at=bound.profile.updated_at,
        provider_target_sha256=canonical_sha256(bound.provider_target.model_dump(mode="json")),
        dokploy_target_sha256=canonical_sha256(bound.dokploy_target.model_dump(mode="json")),
        dokploy_target_id_sha256=canonical_sha256(bound.dokploy_target_id.model_dump(mode="json")),
        runtime_record_refs=runtime_refs,
        runtime_record_sha256=tuple(
            canonical_sha256(record.model_dump(mode="json")) for record in bound.runtime_records
        ),
        secret_record_refs=secret_refs,
        secret_record_sha256=secret_sha256,
    )


def build_product_retirement_plan_record(
    *,
    request: ProductRetirementRequest,
    identity: ProductRetirementIdentity,
    trace_id: str,
    idempotency_key: str,
    requested_at: str,
    bound: BoundProductRetirement,
    observation: ProductRetirementProviderObservation,
) -> ProductRetirementRecord:
    if request.mode != "plan":
        raise ValueError("Product retirement plan records require plan mode.")
    if bound.profile.lifecycle_state != "active":
        raise ProductRetirementBlockedError(
            "Product retirement planning requires an active product profile."
        )
    if observation.state != "present" or not observation.retirable:
        raise ProductRetirementBlockedError(
            "Product retirement requires a present, idle, retirable application."
        )
    snapshot = authority_snapshot(bound)
    plan_payload = {
        "continuity_sha256": request.continuity_sha256,
        "authority_snapshot": snapshot.model_dump(mode="json"),
        "provider_observation": observation.model_dump(mode="json"),
    }
    record_id = (
        "product-retirement-plan-"
        f"{
            canonical_sha256(
                {
                    'product': request.product,
                    'actor': identity.actor,
                    'idempotency_key': idempotency_key,
                }
            )[:32]
        }"
    )
    return ProductRetirementRecord(
        record_id=record_id,
        plan_record_id=record_id,
        mode="plan",
        outcome="planned",
        product=request.product,
        context=bound.context,
        instance=request.instance,
        identity=identity,
        reason=request.reason,
        related_issue=request.related_issue,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        requested_at=requested_at,
        recorded_at=requested_at,
        continuity_sha256=request.continuity_sha256,
        plan_sha256=canonical_sha256(plan_payload),
        provider_observation=observation,
        authority_snapshot=snapshot,
    )


def validate_reviewed_product_retirement_plan(
    *, request: ProductRetirementRequest, plan: ProductRetirementRecord
) -> None:
    if plan.mode != "plan" or plan.outcome != "planned":
        raise ProductRetirementBlockedError("Reviewed product retirement record is not a plan.")
    if request.reviewed_plan_record_id != plan.record_id:
        raise ProductRetirementBlockedError("Reviewed product retirement plan id changed.")
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        raise ProductRetirementBlockedError("Reviewed product retirement plan digest changed.")
    if request.continuity_sha256 != plan.continuity_sha256:
        raise ProductRetirementBlockedError(
            "Product retirement apply does not match reviewed intent."
        )


class DokployProductRetirementAdapter:
    def __init__(
        self,
        *,
        control_plane_root: Path,
        record_store: ProductRetirementStore,
        request: ProductRetirementRequest,
        plan: ProductRetirementRecord,
        identity: ProductRetirementIdentity,
        trace_id: str,
        idempotency_key: str,
        requested_at: str,
    ) -> None:
        self._control_plane_root = control_plane_root
        self._record_store = record_store
        self._request = request
        self._plan = plan
        self._identity = identity
        self._trace_id = trace_id
        self._idempotency_key = idempotency_key
        self._requested_at = requested_at
        self._phases: list[str] = []
        self._runtime_delete_event_ids: list[str] = []
        self._deleted_authority_refs: list[str] = []
        self._disabled_secret_record_sha256: list[str] = []
        self._secret_disable_event_sha256: list[str] = []
        self._provider_effect_performed = False
        self._started = False
        self._lifecycle_before_value: Literal["active", "retiring", "retired"] | None = None
        self._terminal_error_message = ""

    def target_key(self) -> str:
        return f"provider-target:dokploy:application:{self._plan.provider_observation.target_id}"

    def reconciliation_key(self) -> str:
        return f"product-retirement:{self._plan.plan_sha256}"

    def provider_operation_key(self, *, scope: str, route_path: str, fingerprint: str) -> str:
        return build_provider_operation_key(
            scope=scope,
            route_path=route_path,
            idempotency_key=self._idempotency_key,
            request_fingerprint=fingerprint,
            reconciliation_key=self.reconciliation_key(),
        )

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del provider_effect_phase, reconciliation_key
        try:
            observation = observe_tracked_dokploy_application(
                control_plane_root=self._control_plane_root,
                target_id=self._plan.provider_observation.target_id,
                observed_at=self._requested_at,
            )
        except (click.ClickException, OSError, TimeoutError, ValueError):
            return ProviderObservation(outcome="unknown")
        if observation.state == "absent":
            return ProviderObservation(outcome="absent", retry_safe=True)
        if not _observation_allows_reconciliation(
            planned=self._plan.provider_observation,
            current=observation,
        ):
            return ProviderObservation(outcome="unknown")
        if active_preview_ids(
            record_store=self._record_store,
            profile=self._record_store.read_product_profile_record(self._request.product),
        ):
            return ProviderObservation(outcome="unknown")
        return ProviderObservation(outcome="absent", retry_safe=True)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            self._write_started_record(provider_operation_key)
            current_bound = bind_product_retirement_authority(
                record_store=self._record_store,
                request=self._request,
            )
            current_observation = observe_tracked_dokploy_application(
                control_plane_root=self._control_plane_root,
                target_id=self._plan.provider_observation.target_id,
                observed_at=self._requested_at,
            )
            if current_bound.profile.lifecycle_state == "active":
                if authority_snapshot(current_bound) != self._plan.authority_snapshot:
                    raise ProductRetirementBlockedError(
                        "Tracked retirement authority changed after planning."
                    )
                if current_observation.state == "present":
                    if not _same_planned_observation(
                        planned=self._plan.provider_observation,
                        current=current_observation,
                    ):
                        raise ProductRetirementBlockedError(
                            "Provider observation changed after planning."
                        )
                    if not current_observation.retirable:
                        raise ProductRetirementBlockedError(
                            "Provider application is not idle and retirable."
                        )
            elif (
                not _observation_allows_reconciliation(
                    planned=self._plan.provider_observation,
                    current=current_observation,
                )
                and current_observation.state != "absent"
            ):
                raise ProductRetirementBlockedError(
                    "Provider observation changed during retirement reconciliation."
                )
        except (
            FileNotFoundError,
            ProductRetirementBlockedError,
            click.ClickException,
            TimeoutError,
        ) as error:
            raise ProviderMutationRejectedError(error) from error

        self._ensure_retiring_profile()
        self._checkpoint(lease, "profile_retiring")
        if current_observation.state == "absent":
            self._finalize_authority()
            terminal = self._write_terminal_record(
                outcome="already_absent",
                provider_operation_key=provider_operation_key,
                provider_absence_verified=True,
            )
            return ProviderMutationOutcome(
                response_status_code=202,
                response_payload=redacted_product_retirement_response(terminal),
                provider_effect_performed=False,
            )
        try:
            host, token = dokploy_source.read_dokploy_config(
                control_plane_root=self._control_plane_root
            )
        except (
            click.ClickException,
            FileNotFoundError,
            OSError,
            TimeoutError,
            ValueError,
        ) as error:
            raise self._unknown_provider_error(error) from error
        for domain_id in current_observation.domain_ids:
            self._checkpoint(
                lease,
                f"delete_domain:{provider_identifier_sha256(domain_id)[:24]}",
            )
            try:
                dokploy_api.delete_dokploy_domain(
                    host=host,
                    token=token,
                    domain_id=domain_id,
                )
                self._provider_effect_performed = True
            except dokploy_api.DokployRequestFailed as error:
                if error.status_code != 404:
                    raise self._unknown_provider_error(error) from error
            except (OSError, TimeoutError) as error:
                raise self._unknown_provider_error(error) from error
            lease.assert_current()
        self._checkpoint(lease, "delete_application")
        try:
            dokploy_api.delete_dokploy_application(
                host=host,
                token=token,
                application_id=current_observation.target_id,
            )
            self._provider_effect_performed = True
        except dokploy_api.DokployRequestFailed as error:
            if error.status_code != 404:
                raise self._unknown_provider_error(error) from error
        except (OSError, TimeoutError) as error:
            raise self._unknown_provider_error(error) from error
        try:
            absence = observe_tracked_dokploy_application(
                control_plane_root=self._control_plane_root,
                target_id=current_observation.target_id,
                observed_at=self._requested_at,
            )
        except (click.ClickException, OSError, TimeoutError, ValueError) as error:
            raise self._unknown_provider_error(error) from error
        if absence.state != "absent":
            raise self._unknown_provider_error(
                ValueError("Dokploy application absence could not be verified after deletion.")
            )
        try:
            self._finalize_authority()
        except (FileNotFoundError, ProductRetirementBlockedError, ValueError) as error:
            raise self._unknown_provider_error(error) from error
        terminal = self._write_terminal_record(
            outcome="retired",
            provider_operation_key=provider_operation_key,
            provider_absence_verified=True,
        )
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=redacted_product_retirement_response(terminal),
            provider_effect_performed=self._provider_effect_performed,
        )

    def terminal_record(
        self,
        *,
        outcome: str,
        provider_operation_key: str,
        mutation_reservation_id: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> ProductRetirementRecord:
        return self._build_terminal_record(
            outcome=outcome,
            provider_operation_key=provider_operation_key,
            mutation_reservation_id=mutation_reservation_id,
            provider_absence_verified=False,
            error_code=error_code,
            error_message=error_message,
        )

    @property
    def started(self) -> bool:
        return self._started

    def _write_started_record(self, provider_operation_key: str) -> None:
        if self._started:
            return
        lifecycle_before = self._record_store.read_product_profile_record(
            self._request.product
        ).lifecycle_state
        self._lifecycle_before_value = lifecycle_before
        self._record_store.write_product_retirement_record(
            build_started_product_retirement_record(
                request=self._request,
                plan=self._plan,
                identity=self._identity,
                trace_id=self._trace_id,
                idempotency_key=self._idempotency_key,
                requested_at=self._requested_at,
                provider_operation_key=provider_operation_key,
                lifecycle_before=self._lifecycle_before_value,
            )
        )
        self._started = True

    def _unknown_provider_error(self, error: Exception) -> ProviderMutationUnknownError:
        self._terminal_error_message = _redacted_terminal_error_message(str(error))
        return ProviderMutationUnknownError(self._terminal_error_message)

    def _checkpoint(self, lease: ProviderOperationLease, phase: str) -> None:
        lease.checkpoint_effect(phase)
        self._phases.append(phase)

    def _ensure_retiring_profile(self) -> LaunchplaneProductProfileRecord:
        profile = self._record_store.read_product_profile_record(self._request.product)
        if profile.lifecycle_state == "retired":
            return profile
        if profile.lifecycle_state == "retiring":
            return profile
        if (
            canonical_sha256(profile.model_dump(mode="json"))
            != self._plan.authority_snapshot.profile_sha256
        ):
            raise ProductRetirementBlockedError("Product profile changed after planning.")
        replacement = profile.model_copy(
            update={
                "lifecycle_state": "retiring",
                "preview": profile.preview.model_copy(update={"enabled": False}),
                "updated_at": self._requested_at,
                "source": "service:product-retirement",
            }
        )
        result = self._record_store.compare_and_write_product_profile_record(
            expected_record=profile,
            replacement_record=replacement,
        )
        if getattr(result, "status", "written") != "written":
            raise ProductRetirementBlockedError(
                "Product profile changed while entering retirement."
            )
        return replacement

    def _finalize_authority(self) -> None:
        profile = self._record_store.read_product_profile_record(self._request.product)
        if profile.lifecycle_state == "retired":
            return
        if profile.lifecycle_state != "retiring":
            raise ProductRetirementBlockedError(
                "Product retirement authority finalization requires retiring lifecycle state."
            )
        current_runtime = self._record_store.list_runtime_environment_records(
            context_name=self._plan.context,
            instance_name=self._plan.instance,
        )
        planned_runtime = dict(
            zip(
                self._plan.authority_snapshot.runtime_record_refs,
                self._plan.authority_snapshot.runtime_record_sha256,
                strict=True,
            )
        )
        for runtime_record in current_runtime:
            reference = _runtime_ref(runtime_record)
            if planned_runtime.get(reference) != canonical_sha256(
                runtime_record.model_dump(mode="json")
            ):
                raise ProductRetirementBlockedError(
                    "Runtime environment authority changed after planning."
                )
        current_secrets = self._record_store.list_secret_records(
            context_name=self._plan.context,
            instance_name=self._plan.instance,
        )
        if _secret_snapshot(current_secrets) != (
            self._plan.authority_snapshot.secret_record_refs,
            self._plan.authority_snapshot.secret_record_sha256,
        ):
            raise ProductRetirementBlockedError("Managed secret evidence changed after planning.")
        for runtime_record in current_runtime:
            event_id = (
                f"product-retirement:{self._plan.plan_sha256}:"
                f"{provider_identifier_sha256(_runtime_ref(runtime_record))[:24]}"
            )
            delete_status = self._record_store.delete_runtime_environment_record_with_event(
                expected_record=runtime_record,
                event=RuntimeEnvironmentDeleteEvent(
                    event_id=event_id,
                    recorded_at=self._requested_at,
                    actor=self._identity.actor,
                    scope=runtime_record.scope,
                    context=runtime_record.context,
                    instance=runtime_record.instance,
                    source_label="service:product-retirement",
                    env_keys=tuple(sorted(runtime_record.env)),
                    env_value_count=len(runtime_record.env),
                    detail=f"product-retirement-plan:{self._plan.record_id}",
                ),
            )
            if delete_status not in {"deleted", "missing", None}:
                raise ProductRetirementBlockedError(
                    "Runtime environment authority changed while retiring."
                )
            self._runtime_delete_event_ids.append(event_id)
        self._disable_managed_secrets(current_secrets)
        self._delete_target_authority()
        retired_profile = profile.model_copy(
            update={
                "lifecycle_state": "retired",
                "preview": profile.preview.model_copy(update={"enabled": False}),
                "updated_at": self._requested_at,
                "source": "service:product-retirement",
            }
        )
        result = self._record_store.compare_and_write_product_profile_record(
            expected_record=profile,
            replacement_record=retired_profile,
        )
        if getattr(result, "status", "written") != "written":
            raise ProductRetirementBlockedError(
                "Product profile changed while finalizing retirement."
            )

    def _delete_target_authority(self) -> None:
        targets: tuple[
            tuple[
                str,
                Callable[..., ProviderTargetRecord | DokployTargetRecord | DokployTargetIdRecord],
                str,
                Callable[..., object],
            ],
            ...,
        ] = (
            (
                "provider_target",
                self._record_store.read_provider_target_record,
                self._plan.authority_snapshot.provider_target_sha256,
                self._record_store.delete_provider_target_record,
            ),
            (
                "dokploy_target_id",
                self._record_store.read_dokploy_target_id_record,
                self._plan.authority_snapshot.dokploy_target_id_sha256,
                self._record_store.delete_dokploy_target_id_record,
            ),
            (
                "dokploy_target",
                self._record_store.read_dokploy_target_record,
                self._plan.authority_snapshot.dokploy_target_sha256,
                self._record_store.delete_dokploy_target_record,
            ),
        )
        for reference, reader, expected_sha256, deleter in targets:
            try:
                record = reader(
                    context_name=self._plan.context,
                    instance_name=self._plan.instance,
                )
            except FileNotFoundError:
                continue
            if canonical_sha256(record.model_dump(mode="json")) != expected_sha256:
                raise ProductRetirementBlockedError(
                    "Tracked target authority changed after planning."
                )
            delete_status = deleter(expected_record=record)
            if delete_status not in {"deleted", "missing", None}:
                raise ProductRetirementBlockedError(
                    "Tracked target authority changed while retiring."
                )
            self._deleted_authority_refs.append(reference)

    def _disable_managed_secrets(self, records: tuple[SecretRecord, ...]) -> None:
        for record in records:
            if record.status != "disabled":
                self._record_store.write_secret_record(
                    record.model_copy(
                        update={
                            "status": "disabled",
                            "updated_at": self._requested_at,
                            "updated_by": self._identity.actor,
                        }
                    )
                )
            for binding in self._record_store.list_secret_bindings(
                integration=record.integration,
                context_name=record.context,
                instance_name=record.instance,
            ):
                if binding.secret_id == record.secret_id and binding.status != "disabled":
                    self._record_store.write_secret_binding(
                        binding.model_copy(
                            update={"status": "disabled", "updated_at": self._requested_at}
                        )
                    )
            event = SecretAuditEvent(
                event_id=(
                    f"product-retirement:{self._plan.plan_sha256}:"
                    f"secret-disabled:{provider_identifier_sha256(record.secret_id)[:24]}"
                ),
                secret_id=record.secret_id,
                event_type="disabled",
                recorded_at=self._requested_at,
                actor=self._identity.actor,
                detail="Launchplane disabled managed secret authority for product retirement.",
                metadata={"plan_sha256": self._plan.plan_sha256},
            )
            self._record_store.write_secret_audit_event(event)
            self._disabled_secret_record_sha256.append(provider_identifier_sha256(record.secret_id))
            self._secret_disable_event_sha256.append(provider_identifier_sha256(event.event_id))

    def _write_terminal_record(
        self,
        *,
        outcome: str,
        provider_operation_key: str,
        provider_absence_verified: bool,
    ) -> ProductRetirementRecord:
        record = self._build_terminal_record(
            outcome=outcome,
            provider_operation_key=provider_operation_key,
            provider_absence_verified=provider_absence_verified,
        )
        self._record_store.write_product_retirement_record(record)
        return record

    def _build_terminal_record(
        self,
        *,
        outcome: str,
        provider_operation_key: str,
        mutation_reservation_id: str = "",
        provider_absence_verified: bool,
        error_code: str = "",
        error_message: str = "",
    ) -> ProductRetirementRecord:
        lifecycle_after = "retiring"
        try:
            lifecycle_after = self._record_store.read_product_profile_record(
                self._request.product
            ).lifecycle_state
        except FileNotFoundError:
            pass
        return ProductRetirementRecord(
            record_id=build_product_retirement_record_id(
                trace_id=self._trace_id,
                outcome=outcome,
            ),
            plan_record_id=self._plan.record_id,
            mode="apply",
            outcome=cast(ProductRetirementOutcome, outcome),
            product=self._plan.product,
            context=self._plan.context,
            instance=self._plan.instance,
            identity=self._identity,
            reason=self._request.reason,
            related_issue=self._request.related_issue,
            idempotency_key=self._idempotency_key,
            trace_id=self._trace_id,
            requested_at=self._requested_at,
            recorded_at=self._requested_at,
            completed_at=self._requested_at if outcome in {"retired", "already_absent"} else "",
            continuity_sha256=self._request.continuity_sha256,
            plan_sha256=self._plan.plan_sha256,
            reviewed_plan_record_id=self._plan.record_id,
            reviewed_plan_sha256=self._plan.plan_sha256,
            provider_observation=self._plan.provider_observation,
            authority_snapshot=self._plan.authority_snapshot,
            mutation_evidence=ProductRetirementMutationEvidence(
                provider_operation_key=provider_operation_key,
                mutation_reservation_id=mutation_reservation_id,
                reconciliation_key=self.reconciliation_key(),
                provider_effect_phases=tuple(self._phases),
                provider_effect_attempted=bool(self._phases),
                provider_effect_performed=self._provider_effect_performed,
                provider_absence_verified=provider_absence_verified,
                runtime_delete_event_ids=tuple(self._runtime_delete_event_ids),
                deleted_authority_refs=tuple(self._deleted_authority_refs),
                disabled_secret_record_sha256=tuple(self._disabled_secret_record_sha256),
                secret_disable_event_sha256=tuple(self._secret_disable_event_sha256),
                lifecycle_before=self._lifecycle_before_value or "retiring",
                lifecycle_after=cast(
                    "Literal['', 'active', 'retiring', 'retired']",
                    lifecycle_after,
                ),
                error_code=error_code,
                error_message=_redacted_terminal_error_message(
                    self._terminal_error_message or error_message
                ),
            ),
        )


def build_started_product_retirement_record(
    *,
    request: ProductRetirementRequest,
    plan: ProductRetirementRecord,
    identity: ProductRetirementIdentity,
    trace_id: str,
    idempotency_key: str,
    requested_at: str,
    provider_operation_key: str,
    lifecycle_before: Literal["active", "retiring", "retired"],
) -> ProductRetirementRecord:
    return ProductRetirementRecord(
        record_id=build_product_retirement_record_id(trace_id=trace_id, outcome="started"),
        plan_record_id=plan.record_id,
        mode="apply",
        outcome="started",
        product=plan.product,
        context=plan.context,
        instance=plan.instance,
        identity=identity,
        reason=request.reason,
        related_issue=request.related_issue,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        requested_at=requested_at,
        recorded_at=requested_at,
        continuity_sha256=request.continuity_sha256,
        plan_sha256=plan.plan_sha256,
        reviewed_plan_record_id=plan.record_id,
        reviewed_plan_sha256=plan.plan_sha256,
        provider_observation=plan.provider_observation,
        authority_snapshot=plan.authority_snapshot,
        mutation_evidence=ProductRetirementMutationEvidence(
            provider_operation_key=provider_operation_key,
            reconciliation_key=f"product-retirement:{plan.plan_sha256}",
            lifecycle_before=lifecycle_before,
        ),
    )


def redacted_product_retirement_response(record: ProductRetirementRecord) -> dict[str, object]:
    evidence = record.mutation_evidence
    return {
        "trace_id": record.trace_id,
        "records": {
            "product_retirement_plan_id": record.plan_record_id,
            "product_retirement_record_id": record.record_id,
        },
        "result": {
            "mode": record.mode,
            "outcome": record.outcome,
            "product": record.product,
            "context": record.context,
            "instance": record.instance,
            "plan_sha256": record.plan_sha256,
            "target_id_sha256": record.provider_observation.target_id_sha256,
            "provider_observation_sha256": canonical_sha256(
                record.provider_observation.model_dump(
                    mode="json",
                    exclude={"target_id", "domain_ids"},
                )
            ),
            "provider_operation_sha256": (
                provider_identifier_sha256(evidence.provider_operation_key)
                if evidence.provider_operation_key
                else ""
            ),
            "provider_effect_phases": list(evidence.provider_effect_phases),
            "provider_absence_verified": evidence.provider_absence_verified,
            "runtime_delete_event_count": len(evidence.runtime_delete_event_ids),
            "deleted_authority_refs": list(evidence.deleted_authority_refs),
            "disabled_secret_record_count": len(evidence.disabled_secret_record_sha256),
            "lifecycle_after": evidence.lifecycle_after,
            "error_code": evidence.error_code,
        },
    }


def active_preview_ids(
    *, record_store: ProductRetirementStore, profile: LaunchplaneProductProfileRecord
) -> tuple[str, ...]:
    preview_context = profile.preview.context.strip()
    if not preview_context:
        return ()
    return tuple(
        sorted(
            preview_id
            for preview in record_store.list_preview_records(
                context_name=preview_context,
                anchor_repo=profile.repository,
            )
            if getattr(preview, "state", "") in _ACTIVE_PREVIEW_STATES
            and (preview_id := str(getattr(preview, "preview_id", "")).strip())
        )
    )


def _redacted_terminal_error_message(error_message: str) -> str:
    return dokploy_api.redact_dokploy_log_line(error_message).strip()[
        :MAX_PRODUCT_RETIREMENT_ERROR_MESSAGE_LENGTH
    ]


def _observation_allows_reconciliation(
    *,
    planned: ProductRetirementProviderObservation,
    current: ProductRetirementProviderObservation,
) -> bool:
    if current.state != "present" or not current.retirable:
        return False
    if (
        current.target_id != planned.target_id
        or current.application_fingerprint_sha256 != planned.application_fingerprint_sha256
        or current.application_name_sha256 != planned.application_name_sha256
        or current.project_reference_sha256 != planned.project_reference_sha256
    ):
        return False
    planned_domains = set(planned.domain_id_sha256)
    return set(current.domain_id_sha256).issubset(planned_domains)


def _same_planned_observation(
    *,
    planned: ProductRetirementProviderObservation,
    current: ProductRetirementProviderObservation,
) -> bool:
    return planned.model_copy(update={"observed_at": current.observed_at}) == current


def _application_id(payload: Mapping[str, object]) -> str:
    return str(payload.get("applicationId") or payload.get("id") or "").strip()


def _domain_id(payload: Mapping[str, object]) -> str:
    return str(payload.get("domainId") or payload.get("id") or payload.get("uuid") or "").strip()


def _runtime_ref(record: RuntimeEnvironmentRecord) -> str:
    return f"runtime_environment:{record.scope}:{record.context}:{record.instance}"


def _secret_snapshot(
    records: tuple[SecretRecord, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pairs = sorted(
        (
            provider_identifier_sha256(record.secret_id),
            canonical_sha256(
                record.model_dump(
                    mode="json",
                    exclude={"status", "updated_at", "updated_by"},
                )
            ),
        )
        for record in records
    )
    return tuple(item[0] for item in pairs), tuple(item[1] for item in pairs)
