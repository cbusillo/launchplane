from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.contracts.testing_candidate_retention import (
    NativeTestingBackingCapacity,
    NativeTestingSweepCandidate,
    NativeTestingSweepReference,
    NativeTestingSweepRequest,
    NativeTestingSweepResponse,
    NativeTestingSweepResource,
)
from control_plane.contracts.candidate_custody_evidence import (
    CandidateCustodyEvidence,
    LifecycleEvidence,
    VolumeCustodyEvidence,
)
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationUnknownError,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderOperationLease,
    ProviderObservation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import candidate_resources
from control_plane.testing_candidate_retention_host import NATIVE_TESTING_RETENTION_INSPECTION


def candidate_record_refusals(
    candidate: TestingCandidateAllocation,
    operation: Any,
    *,
    product: str,
    context: str,
    selected_targets: tuple[str, ...],
) -> tuple[str, ...]:
    reasons = []
    if not operation.is_native_testing_rebuild or (
        candidate.product,
        candidate.context,
        operation.product,
        operation.context,
        operation.instance,
    ) != (product, context, product, context, "testing"):
        reasons.append("foreign_original")
    if candidate.compose_id in selected_targets:
        reasons.append("candidate_release_selected")
    if operation.status != "fail":
        reasons.append("original_not_failed")
    return tuple(reasons)


def inspect_candidate_resources(
    root: Path,
    store: PostgresRecordStore,
    candidate: TestingCandidateAllocation,
    *,
    mode: str = "dry-run",
    dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
    operation_key: str = "",
) -> tuple[dict[str, Any], str]:
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    frames: list[dict[str, Any]] = []
    try:
        provider = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=candidate.compose_id
        )
        database_name = api.parse_dokploy_env_text(str(provider.get("env") or "")).get(
            "ODOO_DB_NAME", ""
        )
    except (ValueError, OSError, click.ClickException) as error:
        if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied) or (
            isinstance(error, api.DokployRequestFailed) and error.status_code in {401, 403}
        ):
            raise
        database_name = ""

    app = candidate.application
    generation = app.plan.generation if app is not None else None
    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=candidate.server_id,
        program=NATIVE_TESTING_RETENTION_INSPECTION,
        payload={
            "mode": mode,
            "operation_key": operation_key,
            "resources": candidate_resources(candidate).model_dump(mode="json"),
            "project": candidate.namespace,
            "generation": generation.model_dump(mode="json") if generation else None,
            "database_name": database_name,
        },
        receive=frames.append,
        timeout_seconds=300,
        dispatch_guard=dispatch_guard,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Candidate storage readback is incomplete.")
    observed = frames[0]
    return observed, "" if observed.get("volumes") else "physical_inventory_unknown"


def build_native_testing_sweep(
    store: PostgresRecordStore,
    root: Path,
    request: NativeTestingSweepRequest,
    trace_id: str,
    *,
    evidence: list[CandidateCustodyEvidence] | None = None,
) -> tuple[NativeTestingSweepResponse, dict[str, Any]]:
    receiver_id = (
        request.operation_id
        if request.receiver_operation_id is None
        else request.receiver_operation_id
    )
    operation = store.read_odoo_stable_target_replacement_operation_record(request.operation_id)
    for identifier in {request.operation_id, receiver_id} - {""}:
        operation = store.read_odoo_stable_target_replacement_operation_record(identifier)
        if (operation.product, operation.context, operation.instance) != (
            request.product,
            request.context,
            "testing",
        ):
            raise ValueError("The original belongs to another product or instance.")
    owned = [
        candidate
        for candidate in store.list_testing_candidates()
        if (candidate.product, candidate.context) == (request.product, request.context)
        and candidate.compose_id
    ]
    if set(request.candidates) - {candidate.namespace for candidate in owned}:
        raise ValueError("A selected candidate has foreign or unknown original custody.")
    items = []
    private: dict[str, Any] = {}
    capacities: list[NativeTestingBackingCapacity] = []
    capacity_refusal = "receiver_candidate_unavailable"
    from control_plane.testing_candidate_release import (
        TestingCandidateReleaseRefused,
        require_candidate_unselected,
        require_lane_releasable,
    )

    usage_unavailable = ""
    lane_reason = ""
    try:
        usage_unavailable = require_lane_releasable(root, store, operation)
    except TestingCandidateReleaseRefused as error:
        lane_reason = error.code
    for candidate in owned:
        selected = candidate.namespace in request.candidates
        if evidence is not None and not selected:
            continue
        reasons: tuple[str, ...] = ()
        if selected:
            original = store.read_odoo_stable_target_replacement_operation_record(
                candidate.operation_id
            )
            reasons = candidate_record_refusals(
                candidate,
                original,
                product=request.product,
                context=request.context,
                selected_targets=tuple(
                    row.target_id for row in store.list_dokploy_target_id_records()
                ),
            )
        if lane_reason:
            reasons = (*reasons, lane_reason)
        if selected:
            try:
                require_candidate_unselected(root, store, candidate)
            except TestingCandidateReleaseRefused as error:
                reasons = (*reasons, error.code)
        observed: dict[str, Any] = {"volumes": [], "capacity": []}
        references: list[NativeTestingSweepReference] = []
        if usage_unavailable:
            references.append(
                NativeTestingSweepReference(
                    reason="recent_signin_read_unavailable", kind="observation"
                )
            )
        try:
            unavailable = ""
            if selected or candidate.operation_id == receiver_id:
                observed, unavailable = inspect_candidate_resources(root, store, candidate)
        except (ValueError, OSError, click.ClickException) as error:
            if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied) or (
                isinstance(error, api.DokployRequestFailed) and error.status_code in {401, 403}
            ):
                raise
            unavailable = "physical_inventory_unknown"
        if observed.get("serving"):
            reasons = (*reasons, "candidate_release_selected")
        if observed.get("release_status") == "held" and observed.get("reason_code") in {
            "production_boundary",
            "candidate_release_selected",
        }:
            reasons = (*reasons, observed["reason_code"])
        if unavailable:
            references.append(NativeTestingSweepReference(reason=unavailable, kind="observation"))
        if candidate.operation_id == receiver_id:
            capacities = project_capacity(candidate.server_id, observed)
            capacity_refusal = observed.get("capacity_refusal", "receiver_backing_readback_unknown")
        by_name = {row["name"]: row for row in observed["volumes"]}
        resources = []
        for role, name in candidate_resources(candidate).volumes.items():
            row = by_name.get(name, {})
            resources.append(
                NativeTestingSweepResource(
                    role=role,
                    volume_identity_sha256=row.get("identity") or "",
                    backing_sha256=canonical_json_sha256(
                        {"server": candidate.server_id, "backing": row["backing"]}
                    )
                    if "backing" in row
                    else "",
                    free_bytes=row.get("free_bytes"),
                    status="unknown"
                    if not selected and not reasons
                    else "held"
                    if reasons
                    else "absent"
                    if row.get("status") == "absent"
                    else "eligible",
                    reason=next(
                        iter(reasons), "provider_absent" if row.get("status") == "absent" else ""
                    ),
                    readback={ReadbackPredicate.NATIVE_READ: "pass" if row else "unavailable"},
                )
            )
        ownership = canonical_json_sha256(candidate_resources(candidate).model_dump(mode="json"))
        item = NativeTestingSweepCandidate(
            namespace=candidate.namespace,
            operation_id=candidate.operation_id,
            product=candidate.product,
            context=candidate.context,
            target_id=candidate.compose_id,
            selected=selected,
            status="unknown"
            if not selected and not reasons
            else "held"
            if reasons
            else "absent"
            if all(row.status == "absent" for row in resources)
            and not observed.get("container_count")
            else "eligible",
            reasons=tuple(dict.fromkeys(reasons)),
            ownership_sha256=ownership,
            container_count=observed.get("container_count", 0),
            resources=tuple(resources),
            references=tuple(references),
        )
        items.append(item)
        private[candidate.namespace] = {
            "allocation": candidate.model_dump(mode="json"),
            "release_rule": "current-testing-use",
        }
        if evidence is not None:
            evidence.append(
                CandidateCustodyEvidence(
                    observed_at=datetime.now(timezone.utc),
                    candidate_sha256=ownership,
                    server_sha256=canonical_json_sha256({"server": candidate.server_id}),
                    checks={"native_inventory": "unavailable" if unavailable else "pass"},
                    lifecycle=LifecycleEvidence(checks={}),
                    volumes=[
                        VolumeCustodyEvidence(
                            role=cast(
                                Literal["ODOO_DB_VOLUME", "ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME"],
                                row.role,
                            ),
                            name_sha256=canonical_json_sha256(
                                candidate_resources(candidate).volumes[row.role]
                            ),
                            checks={"native_inventory": "unavailable" if unavailable else "pass"},
                        )
                        for row in resources
                    ],
                )
            )
    digest = canonical_json_sha256(
        {
            "request": request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "candidates": [
                {"namespace": item.namespace, "ownership": item.ownership_sha256}
                for item in items
                if item.selected
            ],
        }
    )
    return NativeTestingSweepResponse(
        trace_id=trace_id,
        records={"receiver_operation": receiver_id} if receiver_id else {},
        plan_sha256=digest,
        candidates=tuple(items),
        capacity=tuple(capacities),
        capacity_refusal=capacity_refusal,
    ), private


class NativeTestingSweepAdapter:
    def __init__(
        self,
        store: PostgresRecordStore,
        root: Path,
        request: NativeTestingSweepRequest,
        plan: NativeTestingSweepResponse,
        private: dict[str, Any],
        check_current_use: Callable[[TestingCandidateAllocation], None] | None = None,
    ) -> None:
        self.store, self.root, self.request, self.plan, self.private = (
            store,
            root,
            request,
            plan,
            private,
        )
        self.check_current_use = check_current_use

    def target_key(self) -> str:
        return f"native-testing-retention:{self.request.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        import json

        return json.dumps(
            {"plan": self.plan.model_dump(mode="json"), "private": self.private},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _observe_candidate(
        self,
        candidate: NativeTestingSweepCandidate,
        mode: str,
        dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
        operation_key: str = "",
    ) -> NativeTestingSweepCandidate:
        saved = self.private[candidate.namespace]
        if saved.get("release_rule") != "current-testing-use":
            raise ProviderMutationUnknownError("Preserve the original retention receipt.")
        allocation = TestingCandidateAllocation.model_validate(saved["allocation"])
        observed, unavailable = inspect_candidate_resources(
            self.root,
            self.store,
            allocation,
            mode=mode,
            dispatch_guard=dispatch_guard,
            operation_key=operation_key,
        )
        if observed.get("release_status") == "held":
            reason = observed.get("reason_code", "release_refused")
            statuses = {row["role"]: row["status"] for row in observed.get("volumes", ())}
            return candidate.model_copy(
                update={
                    "status": "held",
                    "reasons": (reason,),
                    "container_count": observed.get("container_count", candidate.container_count),
                    "physical_readback": observed.get("physical_readback", {}),
                    "resources": tuple(
                        row.model_copy(
                            update={
                                "status": "absent"
                                if statuses.get(row.role) == "absent"
                                else "held",
                                "reason": "provider_absent"
                                if statuses.get(row.role) == "absent"
                                else reason,
                            }
                        )
                        for row in candidate.resources
                    ),
                }
            )
        if (
            unavailable
            or observed.get("container_count")
            or not all(row.get("status") == "absent" for row in observed["volumes"])
        ):
            raise ProviderMutationUnknownError("Candidate removal needs its original readback.")
        return candidate.model_copy(
            update={
                "status": "absent",
                "container_count": 0,
                "resources": tuple(
                    row.model_copy(update={"status": "absent", "reason": "removed"})
                    for row in candidate.resources
                ),
                "physical_readback": observed.get("physical_readback")
                or {
                    row.volume_identity_sha256: {"reason": "backing_readback_unknown"}
                    for row in candidate.resources
                    if row.volume_identity_sha256
                },
            }
        )

    def _result(self, candidates: list[NativeTestingSweepCandidate]) -> dict[str, Any]:
        capacity: tuple[NativeTestingBackingCapacity, ...] = ()
        refusal = "receiver_candidate_unavailable"
        receiver_id = (
            self.request.operation_id
            if self.request.receiver_operation_id is None
            else self.request.receiver_operation_id
        )
        if receiver_id:
            receiver = self.store.read_testing_candidate(
                testing_candidate_namespace(self.request.product, self.request.context, receiver_id)
            )
            if receiver is not None:
                try:
                    observed, _ = inspect_candidate_resources(self.root, self.store, receiver)
                    capacity = tuple(project_capacity(receiver.server_id, observed))
                    refusal = observed.get("capacity_refusal", "receiver_backing_readback_unknown")
                except (ValueError, OSError, click.ClickException):
                    refusal = "receiver_backing_readback_unknown"
        return self.plan.model_copy(
            update={
                "candidates": tuple(candidates),
                "capacity": capacity,
                "capacity_refusal": refusal,
            }
        ).model_dump(mode="json")

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        if not provider_effect_phase:
            return ProviderObservation("absent", retry_safe=True)
        candidates = [
            self._observe_candidate(candidate, "readback", operation_key=provider_operation_key)
            if candidate.selected and candidate.status == "eligible"
            else candidate
            for candidate in self.plan.candidates
        ]
        return ProviderObservation("present", response_payload=self._result(candidates))

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        if self.plan.plan_sha256 != self.request.reviewed_plan_sha256:
            raise ProviderMutationRejectedError(ValueError("The reviewed candidate changed."))
        candidates = []
        dispatched = False
        performed = False
        try:
            for candidate in self.plan.candidates:
                if not candidate.selected or candidate.status != "eligible":
                    candidates.append(candidate)
                    continue
                with self.store.testing_candidate_retention_guard(candidate.operation_id) as check:
                    original = TestingCandidateAllocation.model_validate(
                        self.private[candidate.namespace]["allocation"]
                    )

                    def guard() -> None:
                        check()
                        if self.store.read_testing_candidate(candidate.namespace) != original:
                            raise ValueError("Candidate resource addressing changed.")
                        if self.check_current_use is not None:
                            self.check_current_use(original)

                    def dispatch(effect: Callable[[], None]) -> None:
                        nonlocal dispatched
                        guard()
                        lease.checkpoint_effect("release_failed_testing_copy")
                        dispatched = True
                        effect()

                    guard()
                    if isinstance(lease, ProviderEvidenceLease):
                        with self.store.provider_evidence_guard(
                            lease.evidence_reservation, effect_admission_checkpoint=guard
                        ):
                            result = self._observe_candidate(
                                candidate, "apply", dispatch, provider_operation_key
                            )
                    else:
                        result = self._observe_candidate(
                            candidate, "apply", dispatch, provider_operation_key
                        )
                    candidates.append(result)
                    performed = True
        except (ValueError, OSError, click.ClickException, ProviderMutationUnknownError) as error:
            if dispatched:
                raise ProviderMutationUnknownError(
                    "Read back the original candidate release."
                ) from error
            raise ProviderMutationRejectedError(error) from error
        return ProviderMutationOutcome(
            202, self._result(candidates), provider_effect_performed=performed and dispatched
        )


def project_capacity(
    server_id: str, observed: dict[str, Any]
) -> list[NativeTestingBackingCapacity]:
    return [
        NativeTestingBackingCapacity(
            backing_sha256=canonical_json_sha256({"server": server_id, "backing": row["backing"]}),
            **{
                key: row[key]
                for key in (
                    "free_bytes",
                    "required_additional_bytes",
                    "reserve_bytes",
                    "deficit_bytes",
                )
            },
        )
        for row in observed["capacity"]
    ]
