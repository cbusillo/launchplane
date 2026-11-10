from __future__ import annotations

from contextlib import closing, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.contracts.retained_volume_sweep import (
    NativeTestingBackingCapacity,
    NativeTestingSweepCandidate,
    NativeTestingSweepRequest,
    NativeTestingSweepResponse,
    NativeTestingSweepResource,
)
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.odoo_testing_native_source import (
    _cache_directory,
    capture_cache_binding,
    recorded_native_testing_capture,
    verify_native_capture_files,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationUnknownError,
    ProviderMutationOutcome,
    ProviderOperationLease,
    ProviderObservation,
    run_durable_provider_operation,
)
from control_plane.retained_volume_sweep_execution import (
    SweepStatus,
    _host_result,
    _inventory_clear,
    _volume_referenced,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import (
    candidate_resources,
    require_candidate_target_custody,
)
from control_plane.testing_candidate_retention_host import NATIVE_TESTING_RETENTION_INSPECTION
from control_plane.testing_candidate_retention_lifecycle import (
    CandidateRetentionLifecycle,
    candidate_lifecycle_key,
)


def candidate_record_refusals(
    candidate: TestingCandidateAllocation,
    operation: OdooStableTargetReplacementOperationRecord,
    *,
    product: str,
    context: str,
    receiver_operation_id: str,
    selected_targets: tuple[str, ...],
) -> tuple[str, ...]:
    reasons = []
    if not operation.is_native_testing_rebuild:
        reasons.append("original_not_native_testing")
    if (candidate.product, candidate.context) != (product, context) or (
        operation.product,
        operation.context,
        operation.instance,
    ) != (product, context, "testing"):
        reasons.append("foreign_original")
    if candidate.operation_id == receiver_operation_id:
        reasons.append("receiver_candidate")
    if candidate.compose_id in selected_targets:
        reasons.append("selected_target")
    abandoned = candidate.application is not None and candidate.application.phase == "abandoned"
    if candidate.application is not None and candidate.application.prepared is not None:
        reasons.append("completed_copy_requires_disposition")
    if operation.status not in {"fail", "cancelled", "superseded"} and not (
        operation.status == "reconciliation_required" and abandoned
    ):
        reasons.append("original_not_terminal")
    if operation.lease_expires_at:
        try:
            expiry = datetime.fromisoformat(operation.lease_expires_at.replace("Z", "+00:00"))
            if expiry.utcoffset() is None or expiry > datetime.now(timezone.utc):
                reasons.append("original_lease_active")
        except ValueError:
            reasons.append("original_lease_unknown")
    elif operation.lease_owner:
        reasons.append("original_lease_unknown")
    return tuple(reasons)


def candidate_authority(
    store: PostgresRecordStore,
    request: NativeTestingSweepRequest,
    candidate: TestingCandidateAllocation,
    allowed_provider_records: frozenset[str] = frozenset(),
) -> tuple[str, tuple[str, ...]]:
    operation = store.read_odoo_stable_target_replacement_operation_record(candidate.operation_id)
    profile = store.read_product_profile_record(request.product)
    profiles = store.list_product_profile_records()
    if (
        profile.driver_id != "odoo"
        or not any(
            (lane.context, lane.instance) == (request.context, "testing") for lane in profile.lanes
        )
        or any(
            other.product != request.product
            and any(lane.context == request.context for lane in other.lanes)
            for other in profiles
        )
    ):
        raise ValueError("Testing context ownership is unavailable.")
    targets = store.list_dokploy_target_id_records()
    reasons = list(
        candidate_record_refusals(
            candidate,
            operation,
            product=request.product,
            context=request.context,
            receiver_operation_id=request.operation_id,
            selected_targets=tuple(row.target_id for row in targets),
        )
    )
    snapshot: list[object] = [
        {
            "product": profile.product,
            "driver": profile.driver_id,
            "lane": [
                lane.model_dump(mode="json")
                for lane in profile.lanes
                if (lane.context, lane.instance) == (request.context, "testing")
            ],
        },
        operation.model_dump(mode="json"),
        candidate.model_dump(mode="json"),
    ]
    snapshot.extend(
        row.model_dump(mode="json", exclude={"updated_at"})
        for row in targets
        if row.target_id == candidate.compose_id
        or (row.context, row.instance) == (request.context, "testing")
    )
    for target in store.list_provider_target_records():
        if target.target_id == candidate.compose_id:
            snapshot.append(target.model_dump(mode="json"))
            if (target.context, target.instance, target.target_category) != (
                candidate.namespace,
                "testing-candidate",
                "compose",
            ):
                reasons.append("current_or_foreign_target")
    observation = store.read_disposable_lane_safety(candidate.product, candidate.context, "testing")
    if observation is not None and observation.replaced_target_id == candidate.compose_id:
        snapshot.append(observation.model_dump(mode="json"))
        if observation.usage != "unused":
            reasons.append("recorded_use_or_unknown")
        if observation.data == "only_copy":
            reasons.append("recorded_only_needed_copy")
        if observation.production_resources == "shared":
            reasons.append("recorded_production_sharing")
    if not reasons:
        require_candidate_target_custody(store, candidate)
    names = (
        *candidate_resources(candidate).volumes.values(),
        candidate.compose_id,
        candidate.namespace,
    )
    collections = (
        store.list_runtime_environment_records(),
        store.list_preview_records(),
        store.list_preview_generation_records(),
        store.list_private_health_endpoint_records(),
        store.list_production_backup_policy_records(status="active"),
        store.list_odoo_prod_backup_restore_operation_records(),
        store.list_odoo_prod_retained_volume_backup_import_operation_records(),
    )
    for rows in collections:
        for row in rows:
            payload = row.model_dump(mode="json")
            if any(name and _volume_referenced(name, payload, exact=True) for name in names):
                reasons.append("current_or_recovery_reference")
                snapshot.append(payload)
    with closing(store.retained_volume_recovery_payloads()) as payloads:
        for payload in payloads:
            if payload == candidate.model_dump(mode="json") or payload == operation.model_dump(
                mode="json"
            ):
                continue
            if any(name and _volume_referenced(name, payload, exact=True) for name in names):
                reasons.append("needed_copy_or_evidence_reference")
                snapshot.append(payload)
    for reservation in store.list_held_provider_target_reservations():
        if reservation.record_id in allowed_provider_records:
            continue
        if any(name and name in reservation.provider_target_key for name in names):
            reasons.append("active_provider_operation")
            snapshot.append(reservation.model_dump(mode="json"))
    return canonical_json_sha256(snapshot), tuple(dict.fromkeys(reasons))


def candidate_provider_snapshot(payload: dict[str, Any]) -> str:
    if payload.get("composeStatus") not in {"idle", "error", "done"}:
        raise ValueError("Candidate provider work is active or unknown.")
    return canonical_json_sha256(
        {key: value for key, value in payload.items() if key != "composeStatus"}
    )


def inspect_candidate_resources(
    root: Path, store: PostgresRecordStore, candidate: TestingCandidateAllocation
) -> tuple[dict[str, Any], str]:
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=candidate.compose_id
    )
    provider_snapshot = candidate_provider_snapshot(payload)
    project = payload.get("appName")
    from control_plane.contracts.testing_candidate_application import (
        candidate_compose_project_matches,
    )
    from control_plane.odoo_testing_restore import generation_binding
    from control_plane.dokploy.compose import _odoo_web_command
    from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

    if (
        not isinstance(project, str)
        or not candidate_compose_project_matches(candidate.namespace, project)
        or (
            payload.get("composeId"),
            payload.get("environmentId"),
            payload.get("serverId"),
            payload.get("composeType"),
        )
        != (candidate.compose_id, candidate.environment_id, candidate.server_id, "docker-compose")
    ):
        raise ValueError("Candidate provider placement is unbound.")
    frames: list[dict[str, Any]] = []
    app = candidate.application
    generation = app.plan.generation if app is not None else None
    environment = api.parse_dokploy_env_text(str(payload.get("env") or ""))
    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=candidate.server_id,
        program=NATIVE_TESTING_RETENTION_INSPECTION,
        payload={
            "resources": candidate_resources(candidate).model_dump(mode="json"),
            "project": project,
            "generation": generation.model_dump(mode="json") if generation else None,
            "database_name": environment.get("ODOO_DB_NAME", ""),
            "copy_binding_sha256": generation_binding(generation) if generation else "",
            "identity": app.plan.runtime_identity.model_dump(mode="json") if app else None,
            "failed_candidate": {
                "resources": candidate_resources(candidate).model_dump(mode="json"),
                "web_command": [
                    "/bin/sh",
                    "-lc",
                    _odoo_web_command(hold_until_integration_readback=True).replace("$$", "$"),
                ],
                "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
            },
        },
        receive=frames.append,
        timeout_seconds=90,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Candidate resource inventory is incomplete.")
    current = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=candidate.compose_id
    )
    if candidate_provider_snapshot(current) != provider_snapshot:
        raise ValueError("Candidate provider changed during inspection.")
    reason = ""
    if (
        payload.get("autoDeploy") is not False
        or payload.get("domains")
        or payload.get("serviceNetworks")
    ):
        reason = "provider_automatic_or_public_use"
    deployments = api.list_deployments_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=candidate.compose_id,
        require_complete=True,
    )
    if any(
        api.deployment_status(row)
        not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
        for row in deployments
    ):
        reason = "provider_job_active_or_unknown"
    schedules = api.list_dokploy_schedules(
        host=host, token=token, target_id=candidate.compose_id, schedule_type="compose"
    )
    for schedule in schedules:
        if (
            app is None
            or not app.restore_schedule_id
            or api.schedule_key(schedule) != app.restore_schedule_id
            or schedule.get("enabled") is not False
        ):
            reason = "provider_schedule_reference"
            continue
        from control_plane.testing_candidate_application import candidate_restore_terminal

        candidate_restore_terminal(host=host, token=token, record=candidate, require_success=False)
    return {
        **frames[0],
        "project": project,
        "provider_sha256": provider_snapshot,
    }, reason


def candidate_lifecycle_custody(
    root: Path,
    store: PostgresRecordStore,
    candidate: TestingCandidateAllocation,
    project: str,
) -> dict[str, Any]:
    from control_plane.dokploy.testing_retained_copy import retained_copy_operation
    from control_plane.dokploy.compose import _odoo_web_command
    from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

    app = candidate.application
    if app is None or app.phase != "abandoned":
        raise ValueError("Container reclamation needs its terminal abandoned candidate.")
    payload: dict[str, Any] = {
        "context": candidate.context,
        "project": project,
        "mode": "read",
        "allow_unavailable": True,
        "identity": app.plan.runtime_identity.model_dump(mode="json"),
        "failed_candidate": {
            "resources": candidate_resources(candidate).model_dump(mode="json"),
            "image": app.plan.image_reference,
            "web_command": [
                "/bin/sh",
                "-lc",
                _odoo_web_command(hold_until_integration_readback=True).replace("$$", "$"),
            ],
            "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
        },
    }
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    proof = retained_copy_operation(
        host=host, token=token, server_id=candidate.server_id, payload=payload
    )
    payload.update(
        {
            key: proof[key]
            for key in (
                "container_ids",
                "container_snapshot",
                "resource_binding",
                "physical_binding",
            )
        }
    )
    return {
        "namespace": candidate.namespace,
        "server": candidate.server_id,
        "target": candidate.compose_id,
        "payload": payload,
    }


def _source_preserved(
    store: PostgresRecordStore,
    candidate: TestingCandidateAllocation,
    proof: dict[str, Any],
    verified: dict[str, dict[str, Any]] | None = None,
) -> bool:
    app = candidate.application
    if app is None or app.plan.generation is None:
        return app is not None and app.plan.source == "empty"
    generation = app.plan.generation
    if generation.source_mode != "container-export":
        return False
    capture = recorded_native_testing_capture(store, generation)
    if capture is None:
        return False
    directory = _cache_directory(capture_cache_binding(capture), create=False) / capture.capture_id
    paths = [directory / part for part in ("database", "filestore")]

    def snapshot() -> dict[str, Any]:
        files = []
        for path in paths:
            if path.resolve(strict=True) != path or not path.is_file():
                raise ValueError("The preserved source is unavailable or aliased.")
            stat = path.stat()
            files.append(
                [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            )
        return {"capture": canonical_json_sha256(capture.model_dump(mode="json")), "files": files}

    current = snapshot()
    if proof:
        return current == proof
    if verified is None or verified.get(current["capture"]) != current:
        verify_native_capture_files(capture)
    if snapshot() != current:
        raise ValueError("The preserved source changed during verification.")
    proof.update(current)
    if verified is not None:
        verified[current["capture"]] = current
    return True


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


def build_native_testing_sweep(
    store: PostgresRecordStore, root: Path, request: NativeTestingSweepRequest, trace_id: str
) -> tuple[NativeTestingSweepResponse, dict[str, Any]]:
    receiver = store.read_odoo_stable_target_replacement_operation_record(request.operation_id)
    if (receiver.product, receiver.context, receiver.instance) != (
        request.product,
        request.context,
        "testing",
    ):
        raise ValueError("The receiver original belongs to another product or instance.")
    allocations = store.list_testing_candidates()
    owned = [
        row
        for row in allocations
        if (row.product, row.context) == (request.product, request.context)
    ]
    if set(request.candidates) - {row.namespace for row in owned}:
        raise ValueError("A selected candidate has foreign or unknown original custody.")
    receiver_namespace = testing_candidate_namespace(
        request.product, request.context, request.operation_id
    )
    items = []
    private: dict[str, Any] = {}
    capacities = []
    capacity_refusal = "receiver_candidate_unavailable"
    verified_sources: dict[str, dict[str, Any]] = {}
    for candidate in owned:
        selected = candidate.namespace in request.candidates
        try:
            ownership, reasons = candidate_authority(store, request, candidate)
            source_proof: dict[str, Any] = {}
            try:
                source_preserved = not reasons and _source_preserved(
                    store, candidate, source_proof, verified_sources
                )
            except (ValueError, FileNotFoundError, OSError):
                source_preserved = False
                source_proof.clear()
            observed, provider_reason = inspect_candidate_resources(root, store, candidate)
            if candidate.namespace == receiver_namespace:
                capacity_refusal = observed["capacity_refusal"]
                capacities = project_capacity(candidate.server_id, observed)
            reasons = (*reasons, *((provider_reason,) if provider_reason else ()))
            lifecycle: dict[str, Any] = {}
            if (
                not reasons
                and source_preserved
                and any(row.get("reason") == "container_reference" for row in observed["volumes"])
            ):
                try:
                    lifecycle = candidate_lifecycle_custody(
                        root, store, candidate, observed["project"]
                    )
                except (ValueError, OSError):
                    pass
            resources = []
            bindings = []
            for row in observed["volumes"]:
                reason = next(iter(reasons), "") or row.get("reason", "")
                if reason == "container_reference" and lifecycle:
                    reason = ""
                status: SweepStatus = "held"
                if row["status"] == "absent":
                    status = "absent"
                elif row["status"] == "observed" and not reason:
                    if not row["empty"] and not source_preserved:
                        reason = "only_needed_copy_unqualified"
                    else:
                        binding = RetainedVolumeBinding(
                            volume_name=row["name"],
                            server_id=candidate.server_id,
                            application_id=candidate.compose_id,
                            application_name=candidate.namespace,
                            volume_identity_sha256=row["identity"],
                            engine_identity_sha256=row["engine_identity"],
                        )
                        permit = {
                            "binding": ownership,
                            "fresh_until": datetime.now(timezone.utc).timestamp() + 300,
                            "boot_id": row["boot_id"],
                            "monotonic_deadline": row["monotonic_deadline"],
                        }
                        if not _inventory_clear(
                            root, binding, ignored_compose_id=candidate.compose_id, exact=True
                        ):
                            reason = "foreign_provider_reference"
                        else:
                            status, reason = _host_result(
                                root,
                                binding,
                                "dry-run",
                                "native-inspection",
                                retirement=permit,
                                planned_container_removals={
                                    "ids": lifecycle["payload"]["container_ids"],
                                    "snapshot": lifecycle["payload"]["container_snapshot"],
                                }
                                if lifecycle
                                else None,
                            )
                        bindings.append(
                            {"binding": binding.model_dump(mode="json"), "permit": permit}
                        )
                resources.append(
                    NativeTestingSweepResource(
                        role=row["role"],
                        backing_sha256=canonical_json_sha256(
                            {"server": candidate.server_id, "backing": row["backing"]}
                        )
                        if "backing" in row
                        else "",
                        free_bytes=row.get("free_bytes"),
                        volume_identity_sha256=row.get("identity", ""),
                        status=status,
                        reason=reason or "physical_ownership_unknown",
                    )
                )
            if not resources:
                reasons = (*reasons, "physical_inventory_unknown")
            if lifecycle and any(
                item.status not in {"eligible", "absent"}
                and item.reason != "source_inputs_preserved"
                for item in resources
            ):
                lifecycle = {}
                reasons = (*reasons, "candidate_lifecycle_custody_incomplete")
                resources = [
                    item.model_copy(
                        update={
                            "status": "held",
                            "reason": "candidate_lifecycle_custody_incomplete",
                        }
                    )
                    if item.status == "eligible"
                    else item
                    for item in resources
                ]
            item = NativeTestingSweepCandidate(
                namespace=candidate.namespace,
                operation_id=candidate.operation_id,
                product=candidate.product,
                context=candidate.context,
                target_id=candidate.compose_id,
                selected=selected,
                status="eligible" if any(row.status == "eligible" for row in resources) else "held",
                reasons=tuple(dict.fromkeys(reasons)),
                ownership_sha256=ownership,
                source_evidence_sha256=canonical_json_sha256(source_proof) if source_proof else "",
                provider_evidence_sha256=observed.get("provider_sha256", ""),
                lifecycle_snapshot_sha256=canonical_json_sha256(lifecycle) if lifecycle else "",
                container_count=len(lifecycle["payload"]["container_ids"]) if lifecycle else 0,
                resources=tuple(resources),
            )
            private[candidate.namespace] = {
                "allocation": candidate.model_dump(mode="json"),
                "bindings": bindings,
                "source_proof": source_proof,
                "lifecycle": lifecycle,
            }
        except (ValueError, OSError, click.ClickException, ProviderMutationUnknownError):
            item = NativeTestingSweepCandidate(
                namespace=candidate.namespace,
                operation_id=candidate.operation_id,
                product=candidate.product,
                context=candidate.context,
                target_id=candidate.compose_id,
                selected=selected,
                status="held",
                reasons=("current_custody_unavailable",),
                ownership_sha256="",
            )
        items.append(item)
    selected_items = [
        item.model_dump(mode="json", exclude={"resources": {"__all__": {"free_bytes"}}})
        for item in items
        if item.selected
    ]
    digest = canonical_json_sha256(
        {
            "request": request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "candidates": selected_items,
        }
    )
    return NativeTestingSweepResponse(
        trace_id=trace_id,
        records={"receiver_operation": request.operation_id},
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
    ) -> None:
        self.store, self.root, self.request = store, root, request
        self.plan, self.private = plan, private

    def target_key(self) -> str:
        return f"native-testing-retention:{self.request.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        import json

        return json.dumps(
            {"plan": self.plan.model_dump(mode="json"), "private": self.private},
            sort_keys=True,
            separators=(",", ":"),
        )

    def _result(self, candidates: list[NativeTestingSweepCandidate]) -> dict[str, Any]:
        capacity: tuple[NativeTestingBackingCapacity, ...] = ()
        refusal = "receiver_candidate_unavailable"
        try:
            receiver = self.store.read_testing_candidate(
                testing_candidate_namespace(
                    self.request.product, self.request.context, self.request.operation_id
                )
            )
            if receiver is not None:
                observed, _ = inspect_candidate_resources(self.root, self.store, receiver)
                capacity = tuple(project_capacity(receiver.server_id, observed))
                refusal = observed["capacity_refusal"]
        except (ValueError, OSError, click.ClickException, ProviderMutationUnknownError):
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
    ) -> "ProviderObservation":
        from control_plane.provider_operations import ProviderObservation

        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        candidates = []
        for candidate in self.plan.candidates:
            if not candidate.selected or candidate.status != "eligible":
                candidates.append(candidate)
                continue
            lifecycle = self.private[candidate.namespace].get("lifecycle")
            if lifecycle:
                for phase in ("stop", "remove"):
                    CandidateRetentionLifecycle(
                        self.store, self.root, lifecycle, phase, lambda: None
                    ).execute(provider_operation_key, trace_id=self.plan.trace_id, mutate=False)
            items = []
            physical: dict[str, dict[str, Any]] = {}
            bindings = {
                row["binding"]["volume_identity_sha256"]: row
                for row in self.private[candidate.namespace]["bindings"]
            }
            for item in candidate.resources:
                if item.status != "eligible":
                    items.append(item)
                    continue
                row = bindings[item.volume_identity_sha256]
                observation: dict[str, object] = {}
                status, reason = _host_result(
                    self.root,
                    RetainedVolumeBinding.model_validate(row["binding"]),
                    "readback",
                    provider_operation_key,
                    retirement=row["permit"],
                    physical=observation,
                    original_retirement_binding=True,
                )
                if (
                    status not in {"absent", "held"}
                    or status == "held"
                    and reason not in {"never_started", "no_effect"}
                ):
                    return ProviderObservation("unknown")
                physical[item.volume_identity_sha256] = observation
                items.append(item.model_copy(update={"status": status, "reason": reason}))
            candidates.append(
                candidate.model_copy(
                    update={
                        "status": "removed"
                        if all(item.status in {"absent", "removed"} for item in items)
                        else "held",
                        "resources": tuple(items),
                        "physical_readback": physical,
                    }
                )
            )
        return ProviderObservation("present", response_payload=self._result(candidates))

    def apply(
        self, provider_operation_key: str, lease: "ProviderOperationLease"
    ) -> "ProviderMutationOutcome":
        from control_plane.provider_operations import ProviderMutationRejectedError

        self._effect_started = False
        try:
            return self._apply(provider_operation_key, lease)
        except (ValueError, OSError, click.ClickException) as error:
            if self._effect_started:
                raise ProviderMutationUnknownError(
                    "Candidate cleanup may be partial; read back its original request and key."
                ) from error
            raise ProviderMutationRejectedError(error) from error

    def _apply(
        self, provider_operation_key: str, lease: "ProviderOperationLease"
    ) -> "ProviderMutationOutcome":
        from control_plane.provider_operations import (
            ProviderMutationOutcome,
            ProviderMutationRejectedError,
        )
        from control_plane.production_recipient_retirement import RetirementDispatchLease

        if self.plan.plan_sha256 != self.request.reviewed_plan_sha256:
            raise ProviderMutationRejectedError(
                ValueError("Reviewed candidate ownership or resources changed.")
            )
        candidates = []
        performed = False
        for candidate in self.plan.candidates:
            if not candidate.selected or candidate.status != "eligible":
                candidates.append(candidate)
                continue
            with (
                self.store.testing_candidate_retention_guard(candidate.operation_id) as check,
                ExitStack() as effect_guards,
            ):
                original = TestingCandidateAllocation.model_validate(
                    self.private[candidate.namespace]["allocation"]
                )
                lifecycle = self.private[candidate.namespace].get("lifecycle")

                def original_guard() -> None:
                    check()
                    current = self.store.read_testing_candidate(candidate.namespace)
                    from control_plane.contracts.retained_volume_sweep import (
                        RETAINED_VOLUME_SWEEP_ROUTE,
                    )

                    own_records = frozenset(
                        record.record_id
                        for phase in ("stop", "remove")
                        if (
                            record := self.store.read_idempotency_record(
                                scope="native-testing-retention-lifecycle",
                                route_path=RETAINED_VOLUME_SWEEP_ROUTE,
                                idempotency_key=candidate_lifecycle_key(
                                    provider_operation_key, candidate.namespace, phase
                                ),
                            )
                        )
                        is not None
                    )
                    ownership, reasons = candidate_authority(
                        self.store, self.request, original, allowed_provider_records=own_records
                    )
                    if current != original or reasons or ownership != candidate.ownership_sha256:
                        raise ValueError("Candidate ownership or current references changed.")

                if isinstance(lease, ProviderEvidenceLease):
                    effect_guards.enter_context(
                        self.store.provider_evidence_guard(
                            lease.evidence_reservation,
                            effect_admission_checkpoint=original_guard,
                        )
                    )

                if lifecycle:

                    def lifecycle_guard() -> None:
                        original_guard()
                        if not _source_preserved(
                            self.store, original, self.private[candidate.namespace]["source_proof"]
                        ):
                            raise ValueError("Candidate source is no longer preserved.")
                        observed, reason = inspect_candidate_resources(
                            self.root, self.store, original
                        )
                        if (
                            reason
                            or observed.get("provider_sha256", "")
                            != candidate.provider_evidence_sha256
                            or any(
                                row.get("reason")
                                in {
                                    "candidate_web_active_or_unknown",
                                    "candidate_published_ports",
                                    "completed_or_unknown_web_gate",
                                    "physical_ownership_unknown",
                                }
                                for row in observed["volumes"]
                            )
                        ):
                            raise ValueError("Candidate use or physical custody changed.")
                        current = candidate_lifecycle_custody(
                            self.root, self.store, original, observed["project"]
                        )
                        if current != lifecycle:
                            raise ValueError("Candidate container identity changed.")
                        for row in self.private[candidate.namespace]["bindings"]:
                            binding = RetainedVolumeBinding.model_validate(row["binding"])
                            if not _inventory_clear(
                                self.root,
                                binding,
                                ignored_compose_id=original.compose_id,
                                exact=True,
                            ):
                                raise ValueError("Candidate has a foreign provider reference.")

                    lifecycle_guard()
                    self._effect_started = True
                    lease.checkpoint_effect("failed_candidate_lifecycle")
                    complete = True
                    for phase in ("stop", "remove"):
                        complete = CandidateRetentionLifecycle(
                            self.store, self.root, lifecycle, phase, lifecycle_guard
                        ).execute(provider_operation_key, trace_id=self.plan.trace_id, mutate=True)
                        if not complete:
                            break
                    if not complete:
                        candidates.append(
                            candidate.model_copy(
                                update={
                                    "status": "held",
                                    "reasons": ("candidate_lifecycle_incomplete",),
                                    "resources": tuple(
                                        item.model_copy(
                                            update={
                                                "status": "held",
                                                "reason": "candidate_lifecycle_incomplete",
                                            }
                                        )
                                        if item.status == "eligible"
                                        else item
                                        for item in candidate.resources
                                    ),
                                }
                            )
                        )
                        continue
                    performed = True
                bindings = {
                    row["binding"]["volume_identity_sha256"]: row
                    for row in self.private[candidate.namespace]["bindings"]
                }
                items = []
                physical: dict[str, dict[str, Any]] = {}
                for item in candidate.resources:
                    if item.status != "eligible":
                        items.append(item)
                        continue
                    row = bindings[item.volume_identity_sha256]
                    binding = RetainedVolumeBinding.model_validate(row["binding"])
                    effect_permit: dict[str, Any] = {}

                    def guard() -> None:
                        original_guard()
                        if not _inventory_clear(
                            self.root, binding, ignored_compose_id=original.compose_id, exact=True
                        ):
                            raise ValueError("Candidate has a current provider reference.")
                        observed, provider_reason = inspect_candidate_resources(
                            self.root, self.store, original
                        )
                        if (
                            provider_reason
                            or observed.get("provider_sha256", "")
                            != candidate.provider_evidence_sha256
                            or not any(
                                resource.get("identity") == binding.volume_identity_sha256
                                and not resource.get("reason")
                                for resource in observed["volumes"]
                            )
                        ):
                            raise ValueError("Candidate physical or use evidence changed.")
                        if not _source_preserved(
                            self.store, original, self.private[candidate.namespace]["source_proof"]
                        ) and not any(
                            resource.get("identity") == binding.volume_identity_sha256
                            and resource.get("empty") is True
                            for resource in observed["volumes"]
                        ):
                            raise ValueError("Candidate source is no longer preserved.")

                        resource = next(
                            resource
                            for resource in observed["volumes"]
                            if resource.get("identity") == binding.volume_identity_sha256
                        )
                        effect_permit.update(
                            {
                                "binding": candidate.ownership_sha256,
                                "fresh_until": datetime.now(timezone.utc).timestamp() + 300,
                                "boot_id": resource["boot_id"],
                                "monotonic_deadline": resource["monotonic_deadline"],
                            }
                        )

                    guard()
                    self._effect_started = True
                    observation: dict[str, object] = {}
                    status, reason = _host_result(
                        self.root,
                        binding,
                        "apply",
                        provider_operation_key,
                        RetirementDispatchLease(lease, guard),
                        retirement=effect_permit,
                        physical=observation,
                    )
                    if status not in {"removed", "absent"}:
                        raise ProviderMutationUnknownError(
                            "Candidate removal needs its original readback."
                        )
                    performed = True
                    status, reason = _host_result(
                        self.root,
                        binding,
                        "readback",
                        provider_operation_key,
                        retirement=row["permit"],
                        physical=observation,
                        original_retirement_binding=True,
                    )
                    if status != "absent":
                        raise ProviderMutationUnknownError("Physical absence readback is unknown.")
                    physical[item.volume_identity_sha256] = observation
                    items.append(item.model_copy(update={"status": status, "reason": reason}))
                candidates.append(
                    candidate.model_copy(
                        update={
                            "status": "removed"
                            if all(item.status in {"removed", "absent"} for item in items)
                            else "held",
                            "resources": tuple(items),
                            "physical_readback": physical,
                        }
                    )
                )
        return ProviderMutationOutcome(
            202, self._result(candidates), provider_effect_performed=performed
        )


def execute_native_testing_sweep_request(
    store: PostgresRecordStore,
    root: Path,
    request: NativeTestingSweepRequest,
    *,
    actor: str,
    key: str,
    trace_id: str,
) -> NativeTestingSweepResponse:
    import json
    from control_plane.contracts.retained_volume_sweep import RETAINED_VOLUME_SWEEP_ROUTE

    if request.mode == "dry-run":
        plan, _ = build_native_testing_sweep(store, root, request, trace_id)
        return plan
    if not key or len(key) > 200:
        raise ValueError("Apply/readback require the original bounded idempotency key.")
    if request.mode == "apply" and store.database_dialect_name != "postgresql":
        raise ValueError("Candidate reclamation requires PostgreSQL executor exclusion.")
    scope = f"native-testing-retention:{request.product}:{request.context}:{request.operation_id}"
    fingerprint = canonical_json_sha256(
        {
            "actor": actor,
            "request": request.model_copy(update={"mode": "apply"}).model_dump(mode="json"),
        }
    )
    original = store.read_idempotency_record(
        scope=scope, route_path=RETAINED_VOLUME_SWEEP_ROUTE, idempotency_key=key
    )
    if original is not None:
        if original.request_fingerprint != fingerprint:
            raise ValueError("The original cleanup key belongs to another request.")
        if original.state == "completed":
            return NativeTestingSweepResponse.model_validate(original.response_payload).model_copy(
                update={"trace_id": trace_id, "replayed": True}
            )
        saved = json.loads(original.reconciliation_key)
        plan = NativeTestingSweepResponse.model_validate(saved["plan"])
        private = saved["private"]
    else:
        if request.mode == "readback":
            raise ValueError("The original cleanup receipt is unavailable.")
        plan, private = build_native_testing_sweep(store, root, request, trace_id)
        if plan.plan_sha256 != request.reviewed_plan_sha256:
            raise ValueError("The reviewed native candidate plan changed.")
    result = run_durable_provider_operation(
        store=store,
        scope=scope,
        route_path=RETAINED_VOLUME_SWEEP_ROUTE,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        lease_owner=trace_id,
        response_trace_id=trace_id,
        adapter=NativeTestingSweepAdapter(store, root, request, plan, private),
        allow_mutation=request.mode == "apply" and original is None,
    )
    if result.status not in {"completed", "replayed", "adopted"}:
        raise ProviderMutationUnknownError(
            "Preserve the original cleanup request and key for readback."
        )
    return NativeTestingSweepResponse.model_validate(result.response_payload).model_copy(
        update={"trace_id": trace_id, "replayed": result.status == "replayed"}
    )
