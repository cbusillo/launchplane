from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel

from control_plane.contracts.detached_application_retirement import (
    MAX_DETACHED_APPLICATION_RETIREMENT_ERROR_MESSAGE_LENGTH,
    DetachedApplicationAuthorityAbsenceProof,
    DetachedApplicationAuthorityAbsenceSource,
    DetachedApplicationProtectedTargetSnapshot,
    DetachedApplicationProviderObservation,
    DetachedApplicationRetirementIdentity,
    DetachedApplicationRetirementMutationEvidence,
    DetachedApplicationRetirementRecord,
    DetachedApplicationRetirementRequest,
    build_detached_application_retirement_record_id,
)
from control_plane.contracts.product_retirement import canonical_sha256, provider_identifier_sha256
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


class DetachedApplicationRetirementBlockedError(ValueError):
    pass


class DetachedApplicationRetirementStore(Protocol):
    def write_detached_application_retirement_record(
        self, record: DetachedApplicationRetirementRecord
    ) -> object: ...

    def read_detached_application_retirement_record(
        self, record_id: str
    ) -> DetachedApplicationRetirementRecord: ...

    def list_detached_application_retirement_records(
        self,
        *,
        candidate_target_sha256: str = "",
        actor: str = "",
        mode: str = "",
        idempotency_key: str = "",
        limit: int | None = None,
    ) -> tuple[DetachedApplicationRetirementRecord, ...]: ...

    def list_provider_target_records(self, *, provider_id: str = "") -> tuple[object, ...]: ...

    def list_dokploy_target_records(self) -> tuple[object, ...]: ...

    def list_dokploy_target_id_records(self) -> tuple[object, ...]: ...

    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[object, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_preview_enablement_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_preview_generation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_preview_inventory_scan_records(
        self,
        *,
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_preview_desired_state_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_preview_lifecycle_plan_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_preview_lifecycle_cleanup_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_environment_inventory(self) -> tuple[object, ...]: ...

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_edge_endpoint_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]: ...

    def list_product_profile_records(self, *, driver_id: str = "") -> tuple[object, ...]: ...


@dataclass(frozen=True)
class _DiscoveredApplication:
    project_id: str
    project_name: str
    environment_id: str
    environment_name: str
    application_id: str
    application_name: str


@dataclass(frozen=True)
class DetachedApplicationDiscovery:
    candidate: DetachedApplicationProviderObservation
    protected_targets: tuple[DetachedApplicationProtectedTargetSnapshot, ...]


@dataclass(frozen=True)
class _ParsedProjects:
    projects: tuple[tuple[str, str], ...]
    environments: tuple[tuple[str, str, str, str], ...]
    applications: tuple[_DiscoveredApplication, ...]


_AUTHORITATIVE_PROVIDER_NAME_KEYS = frozenset(
    {
        "application",
        "application_name",
        "app_name",
        "display_name",
        "dokploy_application",
        "dokploy_application_name",
        "provider_application",
        "provider_application_name",
    }
)
_AUTHORITY_SOURCES: tuple[
    tuple[str, Callable[[DetachedApplicationRetirementStore], tuple[object, ...]]], ...
] = (
    ("deployment", lambda store: store.list_deployment_records()),
    ("dokploy_target", lambda store: store.list_dokploy_target_records()),
    ("dokploy_target_id", lambda store: store.list_dokploy_target_id_records()),
    ("edge_endpoint", lambda store: store.list_edge_endpoint_records()),
    ("environment_inventory", lambda store: store.list_environment_inventory()),
    ("product_profile", lambda store: store.list_product_profile_records()),
    ("provider_target", lambda store: store.list_provider_target_records()),
    ("preview", lambda store: store.list_preview_records()),
    ("preview_desired_state", lambda store: store.list_preview_desired_state_records()),
    ("preview_enablement", lambda store: store.list_preview_enablement_records()),
    ("preview_generation", lambda store: store.list_preview_generation_records()),
    ("preview_inventory", lambda store: store.list_preview_inventory_scan_records()),
    ("preview_lifecycle_cleanup", lambda store: store.list_preview_lifecycle_cleanup_records()),
    ("preview_lifecycle_plan", lambda store: store.list_preview_lifecycle_plan_records()),
    ("route_binding", lambda store: store.list_route_binding_records()),
    ("runtime_environment", lambda store: store.list_runtime_environment_records()),
    ("secret", lambda store: store.list_secret_records()),
    ("secret_binding", lambda store: store.list_secret_bindings()),
)


def discover_detached_application(
    *,
    control_plane_root: Path,
    request: DetachedApplicationRetirementRequest,
    observed_at: str,
    absent_application_id: str = "",
) -> DetachedApplicationDiscovery:
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    projects = dokploy_api.fetch_dokploy_projects(host=host, token=token)
    parsed = _parse_projects(projects)
    applications = parsed.applications
    matching_projects = {
        project_id
        for project_id, project_name in parsed.projects
        if project_name == request.project_name
    }
    if len(matching_projects) != 1:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application retirement requires exactly one project name match."
        )
    matching_environments = {
        environment_id
        for project_id, project_name, environment_id, environment_name in parsed.environments
        if project_id in matching_projects
        and project_name == request.project_name
        and environment_name == request.environment_name
    }
    if len(matching_environments) != 1:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application retirement requires exactly one environment name match."
        )
    global_name_matches = tuple(
        application
        for application in applications
        if application.application_name == request.application_name
    )
    if len(global_name_matches) > 1:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application retirement requires a globally unique application name."
        )
    exact_matches = tuple(
        application
        for application in global_name_matches
        if application.project_name == request.project_name
        and application.environment_name == request.environment_name
    )
    if len(exact_matches) == 1:
        candidate_reference = exact_matches[0]
        if (
            absent_application_id.strip()
            and candidate_reference.application_id != absent_application_id.strip()
        ):
            raise DetachedApplicationRetirementBlockedError(
                "Detached application name now resolves to a different provider target."
            )
        candidate = _observe_present_application(
            host=host,
            token=token,
            reference=candidate_reference,
            observed_at=observed_at,
            candidate=True,
        )
        if candidate.target_id_sha256 != request.candidate_target_sha256:
            raise DetachedApplicationRetirementBlockedError(
                "Detached application candidate digest does not match provider discovery."
            )
        protected_references = tuple(
            application
            for application in applications
            if application.project_id == candidate_reference.project_id
            and application.application_id != candidate_reference.application_id
        )
    elif global_name_matches:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application name moved outside the reviewed project environment."
        )
    else:
        normalized_absent_id = absent_application_id.strip()
        if not normalized_absent_id:
            raise DetachedApplicationRetirementBlockedError(
                "Detached application retirement candidate is absent during planning."
            )
        if provider_identifier_sha256(normalized_absent_id) != request.candidate_target_sha256:
            raise DetachedApplicationRetirementBlockedError(
                "Stored detached application identifier does not match reviewed digest."
            )
        try:
            dokploy_api.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type="application",
                target_id=normalized_absent_id,
            )
        except dokploy_api.DokployRequestFailed as error:
            if error.status_code != 404:
                raise
        else:
            raise DetachedApplicationRetirementBlockedError(
                "Detached application name disappeared but its reviewed target still exists."
            )
        project_id = next(iter(matching_projects))
        environment_id = next(iter(matching_environments))
        candidate = DetachedApplicationProviderObservation(
            observed_at=observed_at,
            project_id=project_id,
            project_name=request.project_name,
            environment_id=environment_id,
            environment_name=request.environment_name,
            application_id=normalized_absent_id,
            application_name=request.application_name,
            target_id_sha256=request.candidate_target_sha256,
            state="absent",
        )
        protected_references = tuple(
            application for application in applications if application.project_id == project_id
        )
    protected_target_sha256 = tuple(
        sorted(provider_identifier_sha256(item.application_id) for item in protected_references)
    )
    if protected_target_sha256 != request.expected_protected_target_sha256:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application protected target set changed or is incomplete."
        )
    protected_targets = tuple(
        sorted(
            (
                _observe_protected_application(host=host, token=token, reference=reference)
                for reference in protected_references
            ),
            key=lambda target: target.target_id_sha256,
        )
    )
    return DetachedApplicationDiscovery(candidate=candidate, protected_targets=protected_targets)


def prove_detached_application_authority_absence(
    *,
    record_store: DetachedApplicationRetirementStore,
    candidate_target_id: str,
    candidate_application_name: str,
) -> DetachedApplicationAuthorityAbsenceProof:
    normalized_target_id = candidate_target_id.strip()
    normalized_application_name = candidate_application_name.strip()
    if not normalized_target_id or not normalized_application_name:
        raise ValueError("Detached application authority proof requires candidate identity.")
    sources: list[DetachedApplicationAuthorityAbsenceSource] = []
    total_record_count = 0
    for source_name, loader in _AUTHORITY_SOURCES:
        records = loader(record_store)
        payloads = tuple(
            sorted(
                (_record_payload(record) for record in records),
                key=canonical_sha256,
            )
        )
        for payload in payloads:
            if _payload_references_candidate(
                payload,
                candidate_target_id=normalized_target_id,
                candidate_application_name=normalized_application_name,
            ):
                raise DetachedApplicationRetirementBlockedError(
                    f"Detached application remains referenced by Launchplane {source_name} authority."
                )
        total_record_count += len(payloads)
        sources.append(
            DetachedApplicationAuthorityAbsenceSource(
                source=source_name,
                record_count=len(payloads),
                records_sha256=canonical_sha256(payloads),
            )
        )
    return DetachedApplicationAuthorityAbsenceProof(
        candidate_target_sha256=provider_identifier_sha256(normalized_target_id),
        candidate_application_name_sha256=provider_identifier_sha256(normalized_application_name),
        source_count=len(sources),
        record_count=total_record_count,
        sources=tuple(sorted(sources, key=lambda source: source.source)),
    )


def build_detached_application_retirement_plan_record(
    *,
    request: DetachedApplicationRetirementRequest,
    identity: DetachedApplicationRetirementIdentity,
    trace_id: str,
    idempotency_key: str,
    requested_at: str,
    discovery: DetachedApplicationDiscovery,
    authority_absence_proof: DetachedApplicationAuthorityAbsenceProof,
) -> DetachedApplicationRetirementRecord:
    if request.mode != "plan":
        raise ValueError("Detached application retirement plan requires plan mode.")
    if not discovery.candidate.safe_to_retire:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application retirement requires idle, domainless, no-history evidence."
        )
    if authority_absence_proof.candidate_target_sha256 != request.candidate_target_sha256:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application authority proof does not bind the candidate."
        )
    plan_payload = {
        "continuity_sha256": request.continuity_sha256,
        "candidate_observation": discovery.candidate.model_dump(mode="json"),
        "authority_absence_proof": authority_absence_proof.model_dump(mode="json"),
        "protected_targets": tuple(
            target.model_dump(mode="json") for target in discovery.protected_targets
        ),
    }
    record_id = (
        "detached-application-retirement-plan-"
        + canonical_sha256(
            {
                "candidate_target_sha256": request.candidate_target_sha256,
                "actor": identity.actor,
                "idempotency_key": idempotency_key,
            }
        )[:32]
    )
    return DetachedApplicationRetirementRecord(
        record_id=record_id,
        plan_record_id=record_id,
        mode="plan",
        outcome="planned",
        project_name=request.project_name,
        environment_name=request.environment_name,
        application_name=request.application_name,
        identity=identity,
        reason=request.reason,
        related_issue=request.related_issue,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        requested_at=requested_at,
        recorded_at=requested_at,
        continuity_sha256=request.continuity_sha256,
        plan_sha256=canonical_sha256(plan_payload),
        candidate_observation=discovery.candidate,
        authority_absence_proof=authority_absence_proof,
        protected_targets=discovery.protected_targets,
    )


def validate_reviewed_detached_application_retirement_plan(
    *,
    request: DetachedApplicationRetirementRequest,
    plan: DetachedApplicationRetirementRecord,
) -> None:
    if plan.mode != "plan" or plan.outcome != "planned":
        raise DetachedApplicationRetirementBlockedError(
            "Reviewed detached application retirement record is not a plan."
        )
    if request.reviewed_plan_record_id != plan.record_id:
        raise DetachedApplicationRetirementBlockedError(
            "Reviewed detached application retirement plan id changed."
        )
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        raise DetachedApplicationRetirementBlockedError(
            "Reviewed detached application retirement plan digest changed."
        )
    if request.continuity_sha256 != plan.continuity_sha256:
        raise DetachedApplicationRetirementBlockedError(
            "Detached application retirement apply does not match reviewed intent."
        )


class DokployDetachedApplicationRetirementAdapter:
    def __init__(
        self,
        *,
        control_plane_root: Path,
        record_store: DetachedApplicationRetirementStore,
        request: DetachedApplicationRetirementRequest,
        plan: DetachedApplicationRetirementRecord,
        identity: DetachedApplicationRetirementIdentity,
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
        self._provider_effect_attempted = False
        self._provider_effect_performed = False
        self._started = False
        self._latest_discovery: DetachedApplicationDiscovery | None = None
        self._latest_absence_proof: DetachedApplicationAuthorityAbsenceProof | None = None

    def target_key(self) -> str:
        return (
            f"provider-target:dokploy:application:{self._plan.candidate_observation.application_id}"
        )

    def reconciliation_key(self) -> str:
        return f"detached-application-retirement:{self._plan.plan_sha256}"

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
        del provider_operation_key, provider_effect_phase, reconciliation_key
        try:
            discovery, proof = self._rediscover()
        except (click.ClickException, OSError, TimeoutError, ValueError):
            return ProviderObservation(outcome="unknown")
        if not self._protected_and_authority_unchanged(discovery=discovery, proof=proof):
            return ProviderObservation(outcome="unknown")
        if discovery.candidate.state == "absent":
            return ProviderObservation(outcome="absent", retry_safe=True)
        if _same_candidate_evidence(
            planned=self._plan.candidate_observation,
            current=discovery.candidate,
        ):
            return ProviderObservation(outcome="absent", retry_safe=True)
        return ProviderObservation(outcome="unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        self._started = True
        try:
            discovery, proof = self._rediscover()
            if not self._protected_and_authority_unchanged(discovery=discovery, proof=proof):
                raise DetachedApplicationRetirementBlockedError(
                    "Detached application retirement evidence changed after planning."
                )
            if discovery.candidate.state == "present" and not _same_candidate_evidence(
                planned=self._plan.candidate_observation,
                current=discovery.candidate,
            ):
                raise DetachedApplicationRetirementBlockedError(
                    "Detached application provider evidence changed after planning."
                )
        except (
            click.ClickException,
            FileNotFoundError,
            OSError,
            TimeoutError,
            ValueError,
        ) as error:
            raise ProviderMutationRejectedError(error) from error
        if discovery.candidate.state == "absent":
            terminal = self._write_terminal_record(
                outcome="already_absent",
                provider_operation_key=provider_operation_key,
                provider_absence_verified=True,
                protected_targets_unchanged=True,
            )
            return ProviderMutationOutcome(
                response_status_code=202,
                response_payload=redacted_detached_application_retirement_response(terminal),
                provider_effect_performed=False,
            )
        self._checkpoint(lease, "delete_application")
        self._provider_effect_attempted = True
        try:
            host, token = dokploy_source.read_dokploy_config(
                control_plane_root=self._control_plane_root
            )
            dokploy_api.delete_dokploy_application(
                host=host,
                token=token,
                application_id=discovery.candidate.application_id,
            )
            self._provider_effect_performed = True
        except dokploy_api.DokployRequestFailed as error:
            if error.status_code != 404:
                raise self._unknown_provider_error() from error
        except (
            click.ClickException,
            FileNotFoundError,
            OSError,
            TimeoutError,
            ValueError,
        ) as error:
            raise self._unknown_provider_error() from error
        lease.assert_current()
        try:
            after_discovery, after_proof = self._rediscover()
        except (
            click.ClickException,
            FileNotFoundError,
            OSError,
            TimeoutError,
            ValueError,
        ) as error:
            raise self._unknown_provider_error() from error
        if after_discovery.candidate.state != "absent":
            raise self._unknown_provider_error()
        if not self._protected_and_authority_unchanged(
            discovery=after_discovery,
            proof=after_proof,
        ):
            raise self._unknown_provider_error()
        terminal = self._write_terminal_record(
            outcome="retired",
            provider_operation_key=provider_operation_key,
            provider_absence_verified=True,
            protected_targets_unchanged=True,
        )
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=redacted_detached_application_retirement_response(terminal),
            provider_effect_performed=self._provider_effect_performed,
        )

    def terminal_record(
        self,
        *,
        outcome: Literal["reconcile_required", "failed"],
        provider_operation_key: str,
        error_code: str,
        error_message: str,
    ) -> DetachedApplicationRetirementRecord:
        return self._build_terminal_record(
            outcome=outcome,
            provider_operation_key=provider_operation_key,
            provider_absence_verified=False,
            protected_targets_unchanged=False,
            error_code=error_code,
            error_message=_redacted_error_message(error_message),
        )

    @property
    def started(self) -> bool:
        return self._started

    def _rediscover(
        self,
    ) -> tuple[DetachedApplicationDiscovery, DetachedApplicationAuthorityAbsenceProof]:
        discovery = discover_detached_application(
            control_plane_root=self._control_plane_root,
            request=self._request,
            observed_at=self._requested_at,
            absent_application_id=self._plan.candidate_observation.application_id,
        )
        proof = prove_detached_application_authority_absence(
            record_store=self._record_store,
            candidate_target_id=self._plan.candidate_observation.application_id,
            candidate_application_name=self._plan.application_name,
        )
        self._latest_discovery = discovery
        self._latest_absence_proof = proof
        return discovery, proof

    def _protected_and_authority_unchanged(
        self,
        *,
        discovery: DetachedApplicationDiscovery,
        proof: DetachedApplicationAuthorityAbsenceProof,
    ) -> bool:
        return (
            discovery.protected_targets == self._plan.protected_targets
            and proof == self._plan.authority_absence_proof
        )

    def _checkpoint(self, lease: ProviderOperationLease, phase: str) -> None:
        lease.checkpoint_effect(phase)
        self._phases.append(phase)

    def _unknown_provider_error(self) -> ProviderMutationUnknownError:
        return ProviderMutationUnknownError(
            "Dokploy application deletion outcome requires reconciliation."
        )

    def _write_terminal_record(
        self,
        *,
        outcome: Literal["retired", "already_absent"],
        provider_operation_key: str,
        provider_absence_verified: bool,
        protected_targets_unchanged: bool,
    ) -> DetachedApplicationRetirementRecord:
        record = self._build_terminal_record(
            outcome=outcome,
            provider_operation_key=provider_operation_key,
            provider_absence_verified=provider_absence_verified,
            protected_targets_unchanged=protected_targets_unchanged,
        )
        self._record_store.write_detached_application_retirement_record(record)
        return record

    def _build_terminal_record(
        self,
        *,
        outcome: Literal[
            "retired",
            "already_absent",
            "reconcile_required",
            "failed",
        ],
        provider_operation_key: str,
        provider_absence_verified: bool,
        protected_targets_unchanged: bool,
        error_code: str = "",
        error_message: str = "",
    ) -> DetachedApplicationRetirementRecord:
        discovery = self._latest_discovery
        proof = self._latest_absence_proof
        return DetachedApplicationRetirementRecord(
            record_id=build_detached_application_retirement_record_id(
                trace_id=self._trace_id,
                outcome=outcome,
            ),
            plan_record_id=self._plan.record_id,
            mode="apply",
            outcome=outcome,
            project_name=self._plan.project_name,
            environment_name=self._plan.environment_name,
            application_name=self._plan.application_name,
            identity=self._identity,
            reason=self._request.reason,
            related_issue=self._request.related_issue,
            idempotency_key=self._idempotency_key,
            trace_id=self._trace_id,
            requested_at=self._requested_at,
            recorded_at=self._requested_at,
            completed_at=self._requested_at,
            continuity_sha256=self._plan.continuity_sha256,
            plan_sha256=self._plan.plan_sha256,
            reviewed_plan_record_id=self._plan.record_id,
            reviewed_plan_sha256=self._plan.plan_sha256,
            candidate_observation=(
                discovery.candidate if discovery is not None else self._plan.candidate_observation
            ),
            authority_absence_proof=(
                proof if proof is not None else self._plan.authority_absence_proof
            ),
            protected_targets=(
                discovery.protected_targets
                if discovery is not None
                else self._plan.protected_targets
            ),
            mutation_evidence=DetachedApplicationRetirementMutationEvidence(
                provider_operation_key=provider_operation_key,
                reconciliation_key=self.reconciliation_key(),
                provider_effect_phases=tuple(self._phases),
                provider_effect_attempted=self._provider_effect_attempted,
                provider_effect_performed=self._provider_effect_performed,
                provider_absence_verified=provider_absence_verified,
                protected_targets_unchanged=protected_targets_unchanged,
                error_code=error_code.strip(),
                error_message=_redacted_error_message(error_message),
            ),
        )


def redacted_detached_application_retirement_response(
    record: DetachedApplicationRetirementRecord,
) -> dict[str, object]:
    mutation = record.mutation_evidence
    return {
        "trace_id": record.trace_id,
        "records": {
            "detached_application_retirement_plan_id": record.plan_record_id,
            "detached_application_retirement_record_id": record.record_id,
        },
        "result": {
            "mode": record.mode,
            "outcome": record.outcome,
            "plan_sha256": record.plan_sha256,
            "continuity_sha256": record.continuity_sha256,
            "candidate_target_sha256": record.candidate_observation.target_id_sha256,
            "protected_target_sha256": tuple(
                target.target_id_sha256 for target in record.protected_targets
            ),
            "protected_target_count": len(record.protected_targets),
            "authority_source_count": record.authority_absence_proof.source_count,
            "authority_record_count": record.authority_absence_proof.record_count,
            "authority_source_digests": tuple(
                {
                    "source": source.source,
                    "count": source.record_count,
                    "sha256": source.records_sha256,
                }
                for source in record.authority_absence_proof.sources
            ),
            "authority_write_count": record.authority_write_count,
            "provider_operation_sha256": (
                provider_identifier_sha256(mutation.provider_operation_key)
                if mutation.provider_operation_key
                else ""
            ),
            "provider_effect_phases": mutation.provider_effect_phases,
            "provider_effect_attempted": mutation.provider_effect_attempted,
            "provider_effect_performed": mutation.provider_effect_performed,
            "provider_absence_verified": mutation.provider_absence_verified,
            "protected_targets_unchanged": mutation.protected_targets_unchanged,
            "error_code": mutation.error_code,
            "error_message": mutation.error_message,
        },
    }


def _parse_projects(projects: Sequence[Mapping[str, object]]) -> _ParsedProjects:
    applications: list[_DiscoveredApplication] = []
    parsed_projects: list[tuple[str, str]] = []
    parsed_environments: list[tuple[str, str, str, str]] = []
    project_keys: set[tuple[str, str]] = set()
    environment_keys: set[tuple[str, str, str]] = set()
    application_ids: set[str] = set()
    for project in projects:
        project_id = _required_identifier(project, "projectId", "id", label="project id")
        project_name = _required_string(project, "name", label="project name")
        project_key = (project_id, project_name)
        if project_key in project_keys:
            raise DetachedApplicationRetirementBlockedError(
                "Dokploy project.all contains duplicate project evidence."
            )
        project_keys.add(project_key)
        parsed_projects.append(project_key)
        raw_environments = project.get("environments")
        if not isinstance(raw_environments, list):
            raise DetachedApplicationRetirementBlockedError(
                "Dokploy project.all project environments are malformed."
            )
        for raw_environment in raw_environments:
            if not isinstance(raw_environment, Mapping):
                raise DetachedApplicationRetirementBlockedError(
                    "Dokploy project.all contains a malformed environment."
                )
            environment = cast(Mapping[str, object], raw_environment)
            environment_id = _required_identifier(
                environment,
                "environmentId",
                "id",
                label="environment id",
            )
            environment_name = _required_string(
                environment,
                "name",
                label="environment name",
            )
            environment_key = (project_id, environment_id, environment_name)
            if environment_key in environment_keys:
                raise DetachedApplicationRetirementBlockedError(
                    "Dokploy project.all contains duplicate environment evidence."
                )
            environment_keys.add(environment_key)
            parsed_environments.append((project_id, project_name, environment_id, environment_name))
            raw_applications = environment.get("applications")
            if not isinstance(raw_applications, list):
                raise DetachedApplicationRetirementBlockedError(
                    "Dokploy project.all environment applications are malformed."
                )
            for raw_application in raw_applications:
                if not isinstance(raw_application, Mapping):
                    raise DetachedApplicationRetirementBlockedError(
                        "Dokploy project.all contains a malformed application."
                    )
                application = cast(Mapping[str, object], raw_application)
                application_id = _required_identifier(
                    application,
                    "applicationId",
                    "id",
                    label="application id",
                )
                application_name = _required_application_name(application)
                if application_id in application_ids:
                    raise DetachedApplicationRetirementBlockedError(
                        "Dokploy project.all contains duplicate application identifiers."
                    )
                application_ids.add(application_id)
                nested_environment_id = _optional_string(application, "environmentId")
                if nested_environment_id and nested_environment_id != environment_id:
                    raise DetachedApplicationRetirementBlockedError(
                        "Dokploy application environment evidence is inconsistent."
                    )
                nested_project_id = _optional_string(application, "projectId")
                if nested_project_id and nested_project_id != project_id:
                    raise DetachedApplicationRetirementBlockedError(
                        "Dokploy application project evidence is inconsistent."
                    )
                applications.append(
                    _DiscoveredApplication(
                        project_id=project_id,
                        project_name=project_name,
                        environment_id=environment_id,
                        environment_name=environment_name,
                        application_id=application_id,
                        application_name=application_name,
                    )
                )
    return _ParsedProjects(
        projects=tuple(parsed_projects),
        environments=tuple(parsed_environments),
        applications=tuple(applications),
    )


def _observe_present_application(
    *,
    host: str,
    token: str,
    reference: _DiscoveredApplication,
    observed_at: str,
    candidate: bool,
) -> DetachedApplicationProviderObservation:
    payload = dokploy_api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=reference.application_id,
    )
    _validate_application_payload(payload=payload, reference=reference)
    domains = _fetch_strict_application_domains(
        host=host,
        token=token,
        application_id=reference.application_id,
    )
    deployment_history = dokploy_api.deployment_history_for_target(
        host=host,
        token=token,
        target_type="application",
        target_id=reference.application_id,
    )
    if deployment_history.state == "unknown":
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy deployment history evidence is unknown."
        )
    application_state = _application_state(payload)
    domain_pairs = _normalized_domains(domains)
    latest_deployment_sha256 = (
        canonical_sha256(deployment_history.latest_deployment)
        if deployment_history.latest_deployment is not None
        else ""
    )
    safe_to_retire = bool(
        candidate
        and application_state == "idle"
        and deployment_history.state == "no_history"
        and deployment_history.latest_deployment is None
        and not domain_pairs
    )
    return DetachedApplicationProviderObservation(
        observed_at=observed_at,
        project_id=reference.project_id,
        project_name=reference.project_name,
        environment_id=reference.environment_id,
        environment_name=reference.environment_name,
        application_id=reference.application_id,
        application_name=reference.application_name,
        target_id_sha256=provider_identifier_sha256(reference.application_id),
        state="present",
        application_state=application_state,
        application_fingerprint_sha256=canonical_sha256(payload),
        domain_ids=tuple(domain_id for domain_id, _host in domain_pairs),
        domain_id_sha256=tuple(
            provider_identifier_sha256(domain_id) for domain_id, _host in domain_pairs
        ),
        domain_host_sha256=tuple(
            provider_identifier_sha256(host_name) for _domain_id, host_name in domain_pairs
        ),
        deployment_history_state=deployment_history.state,
        latest_deployment_sha256=latest_deployment_sha256,
        safe_to_retire=safe_to_retire,
    )


def _observe_protected_application(
    *,
    host: str,
    token: str,
    reference: _DiscoveredApplication,
) -> DetachedApplicationProtectedTargetSnapshot:
    payload = dokploy_api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=reference.application_id,
    )
    _validate_application_payload(payload=payload, reference=reference)
    domains = _fetch_strict_application_domains(
        host=host,
        token=token,
        application_id=reference.application_id,
    )
    deployment_history = dokploy_api.deployment_history_for_target(
        host=host,
        token=token,
        target_type="application",
        target_id=reference.application_id,
    )
    if deployment_history.state == "unknown":
        raise DetachedApplicationRetirementBlockedError(
            "Protected Dokploy deployment history evidence is unknown."
        )
    _application_state(payload)
    domain_pairs = _normalized_domains(domains)
    return DetachedApplicationProtectedTargetSnapshot(
        application_id=reference.application_id,
        target_id_sha256=provider_identifier_sha256(reference.application_id),
        application_name=reference.application_name,
        application_fingerprint_sha256=canonical_sha256(payload),
        domain_count=len(domain_pairs),
        domains_sha256=canonical_sha256(domain_pairs),
        deployment_history_state=deployment_history.state,
        deployment_history_sha256=canonical_sha256(
            {
                "state": deployment_history.state,
                "latest_deployment": deployment_history.latest_deployment,
            }
        ),
    )


def _fetch_strict_application_domains(
    *, host: str, token: str, application_id: str
) -> tuple[dokploy_api.JsonObject, ...]:
    payload = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byApplicationId",
        query={"applicationId": application_id},
    )
    if not isinstance(payload, list):
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy application domains returned malformed evidence."
        )
    domains: list[dokploy_api.JsonObject] = []
    for item in payload:
        domain = dokploy_api.as_json_object(item)
        if domain is None:
            raise DetachedApplicationRetirementBlockedError(
                "Dokploy application domains contain malformed evidence."
            )
        domains.append(domain)
    return tuple(domains)


def _validate_application_payload(
    *, payload: Mapping[str, object], reference: _DiscoveredApplication
) -> None:
    application_id = _required_identifier(
        payload,
        "applicationId",
        "id",
        label="application id",
    )
    application_name = _required_application_name(payload)
    if application_id != reference.application_id or application_name != reference.application_name:
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy application.one does not match project discovery."
        )
    environment_id = _optional_string(payload, "environmentId")
    if environment_id and environment_id != reference.environment_id:
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy application.one environment evidence changed."
        )
    project_id = _optional_string(payload, "projectId")
    if project_id and project_id != reference.project_id:
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy application.one project evidence changed."
        )


def _application_state(payload: Mapping[str, object]) -> str:
    state = (
        str(
            payload.get("applicationStatus")
            or payload.get("application_status")
            or payload.get("status")
            or ""
        )
        .strip()
        .lower()
    )
    if not state:
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy application.one is missing application state."
        )
    return state


def _record_payload(record: object) -> object:
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json", exclude_none=True)
    if isinstance(record, Mapping):
        return dict(record)
    raise DetachedApplicationRetirementBlockedError(
        "Launchplane authority source returned an unsupported record type."
    )


def _payload_references_candidate(
    payload: object,
    *,
    candidate_target_id: str,
    candidate_application_name: str,
    key: str = "",
) -> bool:
    if isinstance(payload, Mapping):
        return any(
            _payload_references_candidate(
                value,
                candidate_target_id=candidate_target_id,
                candidate_application_name=candidate_application_name,
                key=str(raw_key),
            )
            for raw_key, value in payload.items()
        )
    if isinstance(payload, Sequence) and not isinstance(payload, str | bytes | bytearray):
        return any(
            _payload_references_candidate(
                value,
                candidate_target_id=candidate_target_id,
                candidate_application_name=candidate_application_name,
                key=key,
            )
            for value in payload
        )
    if not isinstance(payload, str):
        return False
    normalized_value = payload.strip()
    if normalized_value == candidate_target_id:
        return True
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return (
        normalized_key in _AUTHORITATIVE_PROVIDER_NAME_KEYS
        and normalized_value == candidate_application_name
    )


def _same_candidate_evidence(
    *,
    planned: DetachedApplicationProviderObservation,
    current: DetachedApplicationProviderObservation,
) -> bool:
    if current.state != "present" or not current.safe_to_retire:
        return False
    return planned.model_copy(update={"observed_at": current.observed_at}) == current


def _required_identifier(
    payload: Mapping[str, object], primary: str, fallback: str, *, label: str
) -> str:
    primary_value = _optional_string(payload, primary)
    fallback_value = _optional_string(payload, fallback)
    if primary_value and fallback_value and primary_value != fallback_value:
        raise DetachedApplicationRetirementBlockedError(f"Dokploy {label} fields are inconsistent.")
    value = primary_value or fallback_value
    if not value:
        raise DetachedApplicationRetirementBlockedError(f"Dokploy evidence is missing {label}.")
    return value


def _required_string(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = _optional_string(payload, key)
    if not value:
        raise DetachedApplicationRetirementBlockedError(f"Dokploy evidence is missing {label}.")
    return value


def _required_application_name(payload: Mapping[str, object]) -> str:
    name = _optional_string(payload, "name")
    app_name = _optional_string(payload, "appName")
    value = name or app_name
    if not value:
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy evidence is missing application name."
        )
    return value


def _required_domain_host(payload: Mapping[str, object]) -> str:
    host = _optional_string(payload, "host") or _optional_string(payload, "domain")
    if not host:
        raise DetachedApplicationRetirementBlockedError(
            "Dokploy domain evidence is missing host identity."
        )
    return host.lower()


def _normalized_domains(
    domains: Sequence[Mapping[str, object]],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                _required_identifier(domain, "domainId", "id", label="domain id"),
                _required_domain_host(domain),
            )
            for domain in domains
        )
    )


def _optional_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DetachedApplicationRetirementBlockedError(
            f"Dokploy evidence field {key} must be a string."
        )
    return value.strip()


def _redacted_error_message(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    return normalized[:MAX_DETACHED_APPLICATION_RETIREMENT_ERROR_MESSAGE_LENGTH]
