from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Literal, cast

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.candidate_custody_evidence import (
    CandidateCustodyEvidence,
    Outcome,
    Predicate,
    LifecycleEvidence,
    VolumeCustodyEvidence,
)
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.contracts.testing_candidate_retention import (
    NativeTestingBackingCapacity,
    NativeTestingSweepCandidate,
    NativeTestingSweepReference,
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


class CandidateAuthority(NamedTuple):
    ownership_sha256: str
    reasons: tuple[str, ...]
    references: tuple[NativeTestingSweepReference, ...]


def completed_copy(candidate: TestingCandidateAllocation) -> bool:
    return candidate.application is not None and candidate.application.prepared is not None


def candidate_authority(
    store: PostgresRecordStore,
    request: NativeTestingSweepRequest,
    candidate: TestingCandidateAllocation,
    allowed_provider_records: frozenset[str] = frozenset(),
) -> CandidateAuthority:
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
            receiver_operation_id=(
                request.operation_id
                if request.receiver_operation_id is None
                else request.receiver_operation_id
            ),
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
    references: list[NativeTestingSweepReference] = []

    def names_candidate(evidence: object) -> bool:
        return any(name and _volume_referenced(name, evidence, exact=True) for name in names)

    def hold(reason: str, owner: str, evidence: dict[str, Any], record_id: str = "") -> None:
        reasons.append(reason)
        snapshot.append(evidence)
        references.append(
            NativeTestingSweepReference(
                reason=reason,
                kind=owner,
                record_id=record_id,
                record_sha256="" if record_id else canonical_json_sha256(evidence),
            )
        )

    collections = (
        ("runtime_environment", store.list_runtime_environment_records()),
        ("preview", store.list_preview_records()),
        ("preview_generation", store.list_preview_generation_records()),
        ("private_health_endpoint", store.list_private_health_endpoint_records()),
        ("production_backup_policy", store.list_production_backup_policy_records(status="active")),
        ("production_restore", store.list_odoo_prod_backup_restore_operation_records()),
        (
            "production_volume_import",
            store.list_odoo_prod_retained_volume_backup_import_operation_records(),
        ),
    )
    for kind, rows in collections:
        for row in rows:
            payload = row.model_dump(mode="json")
            if names_candidate(payload):
                hold("current_or_recovery_reference", kind, payload)
    own_records = {
        ("testing_candidate", candidate.namespace),
        ("testing_operation", candidate.operation_id),
    }
    with closing(store.retained_volume_recovery_records()) as records:
        for record in records:
            if (record.kind, record.record_id) in own_records:
                continue
            if names_candidate(record.payload):
                hold(
                    "needed_copy_or_evidence_reference",
                    record.kind,
                    record.payload,
                    record.record_id,
                )
    for reservation in store.list_held_provider_target_reservations():
        if reservation.record_id in allowed_provider_records:
            continue
        if any(name and name in reservation.provider_target_key for name in names):
            hold(
                "active_provider_operation",
                "provider_reservation",
                reservation.model_dump(mode="json"),
                reservation.record_id,
            )
    return CandidateAuthority(
        canonical_json_sha256(snapshot), tuple(dict.fromkeys(reasons)), tuple(references)
    )


def gate_volume_last(
    resources: tuple[NativeTestingSweepResource, ...],
) -> list[NativeTestingSweepResource]:
    return sorted(resources, key=lambda item: item.role == "ODOO_DATA_VOLUME")


def candidate_provider_snapshot(payload: dict[str, Any]) -> str:
    if payload.get("composeStatus") not in {"idle", "error", "done"}:
        raise ValueError("Candidate provider work is active or unknown.")
    return canonical_json_sha256(
        {key: value for key, value in payload.items() if key != "composeStatus"}
    )


def inspect_candidate_resources(
    root: Path,
    store: PostgresRecordStore,
    candidate: TestingCandidateAllocation,
    *,
    evidence: CandidateCustodyEvidence | None = None,
) -> tuple[dict[str, Any], str]:
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=candidate.compose_id
    )
    if evidence is not None:
        evidence.endpoint_sha256 = canonical_json_sha256(
            {"host": host, "server": payload.get("serverId")}
        )
        evidence.checks["placement"] = (
            "pass" if payload.get("serverId") == candidate.server_id else "fail"
        )
    project = payload.get("appName")
    from control_plane.contracts.testing_candidate_application import (
        candidate_compose_project_matches,
    )
    from control_plane.odoo_testing_restore import generation_binding
    from control_plane.dokploy.compose import held_odoo_web_container_command
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
    reasons = []
    try:
        provider_snapshot = candidate_provider_snapshot(payload)
    except ValueError:
        provider_snapshot = ""
        reasons.append("provider_job_active_or_unknown")
    app = candidate.application
    if (
        payload.get("autoDeploy") is not False
        or payload.get("domains")
        or payload.get("serviceNetworks")
    ):
        reasons.append("provider_automatic_or_public_use")
    try:
        deployments = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=candidate.compose_id,
            require_complete=True,
        )
        if any(not api.deployment_is_terminal(row) for row in deployments):
            reasons.append("provider_job_active_or_unknown")
    except (ValueError, OSError, click.ClickException) as error:
        if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied) or (
            isinstance(error, api.DokployRequestFailed) and error.status_code in {401, 403}
        ):
            raise
        reasons.append("provider_job_inventory_unavailable")
    try:
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
                reasons.append("provider_schedule_reference")
                continue
            from control_plane.testing_candidate_application import (
                candidate_restore_terminal,
                CandidateObservationUnavailable,
            )

            try:
                candidate_restore_terminal(
                    host=host, token=token, record=candidate, require_success=False
                )
            except CandidateObservationUnavailable:
                reasons.append("provider_restore_nonterminal_or_unavailable")
    except (ValueError, OSError, click.ClickException) as error:
        if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied) or (
            isinstance(error, api.DokployRequestFailed) and error.status_code in {401, 403}
        ):
            raise
        reasons.append("provider_schedule_inventory_unavailable")
    frames: list[dict[str, Any]] = []
    observed: dict[str, Any] = {
        "volumes": [],
        "capacity": [],
        "capacity_refusal": "physical_inventory_unknown",
    }
    if provider_snapshot:
        generation = app.plan.generation if app is not None else None
        try:
            environment = api.parse_dokploy_env_text(str(payload.get("env") or ""))
            native_copy_stream.run_managed_server_program(
                host=host,
                token=token,
                server_id=candidate.server_id,
                program=NATIVE_TESTING_RETENTION_INSPECTION,
                payload={
                    "resources": candidate_resources(candidate).model_dump(mode="json"),
                    **({"custody_evidence": True} if evidence is not None else {}),
                    "project": project,
                    "generation": generation.model_dump(mode="json") if generation else None,
                    "database_name": environment.get("ODOO_DB_NAME", ""),
                    "copy_binding_sha256": generation_binding(generation) if generation else "",
                    "identity": app.plan.runtime_identity.model_dump(mode="json") if app else None,
                    "failed_candidate": {
                        "resources": candidate_resources(candidate).model_dump(mode="json"),
                        "web_command": held_odoo_web_container_command(),
                        "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
                        **({"completed": True} if completed_copy(candidate) else {}),
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
            observed = frames[0]
        except (ValueError, OSError, click.ClickException) as error:
            if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied) or (
                isinstance(error, api.DokployRequestFailed) and error.status_code in {401, 403}
            ):
                raise
            reasons.append("current_custody_unavailable")
    return {
        **observed,
        "project": project,
        "provider_sha256": provider_snapshot,
        "reasons": list(dict.fromkeys(reasons)),
    }, reasons[-1] if reasons else ""


def candidate_lifecycle_custody(
    root: Path,
    store: PostgresRecordStore,
    candidate: TestingCandidateAllocation,
    project: str,
    *,
    evidence: LifecycleEvidence | None = None,
) -> dict[str, Any]:
    from control_plane.dokploy.testing_retained_copy import retained_copy_operation
    from control_plane.dokploy.compose import held_odoo_web_container_command
    from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

    app = candidate.application
    if app is None or app.phase not in {"abandoned", "prepared"}:
        raise ValueError("Container reclamation needs its terminal failed candidate.")
    payload: dict[str, Any] = {
        "context": candidate.context,
        "project": project,
        "mode": "read",
        "allow_unavailable": True,
        "identity": app.plan.runtime_identity.model_dump(mode="json"),
        "failed_candidate": {
            "resources": candidate_resources(candidate).model_dump(mode="json"),
            "image": app.plan.image_reference,
            "web_command": held_odoo_web_container_command(),
            "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
            **({"completed": True} if completed_copy(candidate) else {}),
        },
    }
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    observation: dict[str, Any] = {}
    try:
        proof = retained_copy_operation(
            host=host,
            token=token,
            server_id=candidate.server_id,
            payload={**payload, **({"custody_evidence": True} if evidence is not None else {})},
            evidence=observation if evidence is not None else None,
        )
    finally:
        if evidence is not None and observation:
            parsed = LifecycleEvidence.model_validate(observation)
            evidence.containers = parsed.containers
            for predicate, outcome in parsed.checks.items():
                evidence.checks[cast(Predicate, predicate)] = cast(Outcome, outcome)
    if evidence is not None:
        evidence.snapshot_sha256 = proof.get("container_snapshot")
        evidence.resource_binding_sha256 = proof.get("resource_binding")
        evidence.physical_binding_sha256 = proof.get("physical_binding")
        evidence.container_count = len(proof["container_ids"])

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


def candidate_resource_observation(
    candidate: TestingCandidateAllocation, row: dict[str, Any]
) -> NativeTestingSweepResource:
    resource_readback: dict[ReadbackPredicate, Any] = {
        ReadbackPredicate.NATIVE_READ: "pass"
        if row["status"] in {"observed", "absent"}
        else "unavailable",
        ReadbackPredicate.STORAGE_CONSUMERS: "skipped",
        ReadbackPredicate.RESOURCES: "skipped",
    }
    return NativeTestingSweepResource(
        role=row["role"],
        backing_sha256=canonical_json_sha256(
            {"server": candidate.server_id, "backing": row["backing"]}
        )
        if "backing" in row
        else "",
        free_bytes=row.get("free_bytes"),
        volume_identity_sha256=row.get("identity", ""),
        status="held",
        reason=row.get("reason") or "physical_ownership_unknown",
        readback=resource_readback,
    )


def build_native_testing_sweep(
    store: PostgresRecordStore,
    root: Path,
    request: NativeTestingSweepRequest,
    trace_id: str,
    allowed_provider_records: frozenset[str] = frozenset(),
    *,
    evidence: list[CandidateCustodyEvidence] | None = None,
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
    receiver_id = (
        request.operation_id
        if request.receiver_operation_id is None
        else request.receiver_operation_id
    )
    if receiver_id:
        receiver = store.read_odoo_stable_target_replacement_operation_record(receiver_id)
        if (receiver.product, receiver.context, receiver.instance) != (
            request.product,
            request.context,
            "testing",
        ):
            raise ValueError("The receiver original belongs to another product or instance.")
    receiver_namespace = (
        testing_candidate_namespace(request.product, request.context, receiver_id)
        if receiver_id
        else ""
    )
    items = []
    private: dict[str, Any] = {}
    capacities = []
    capacity_refusal = "receiver_candidate_unavailable"
    verified_sources: dict[str, dict[str, Any]] = {}
    for candidate in owned:
        selected = candidate.namespace in request.candidates
        if evidence is not None and not selected:
            continue
        packet = (
            CandidateCustodyEvidence(
                observed_at=datetime.now(timezone.utc),
                candidate_sha256=canonical_json_sha256(candidate.model_dump(mode="json")),
                server_sha256=canonical_json_sha256({"server": candidate.server_id}),
                checks={"native_inventory": "not_reached", "source_preserved": "not_reached"},
                lifecycle=LifecycleEvidence(
                    checks={
                        "lifecycle_acquisition": "not_reached",
                        "lifecycle_usable": "not_reached",
                    }
                ),
            )
            if evidence is not None
            else None
        )
        if evidence is not None and packet is not None:
            evidence.append(packet)
            packet.volumes = [
                VolumeCustodyEvidence(
                    role=cast(
                        Literal["ODOO_DB_VOLUME", "ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME"], role
                    ),
                    name_sha256=canonical_json_sha256(name),
                    checks={
                        "native_inventory": "not_reached",
                        "provider_inventory": "not_reached",
                        "physical": "not_reached",
                    },
                )
                for role, name in candidate_resources(candidate).volumes.items()
            ]
        readback: dict[ReadbackPredicate, Any] = {
            ReadbackPredicate.FILES_READ: "skipped",
            ReadbackPredicate.CUSTODY: "skipped",
            ReadbackPredicate.NATIVE_READ: "skipped",
        }
        references: tuple[NativeTestingSweepReference, ...] = ()
        resources: list[NativeTestingSweepResource] = []
        reasons: tuple[str, ...] = ()
        try:
            ownership, reasons, references = candidate_authority(
                store, request, candidate, allowed_provider_records
            )
            source_proof: dict[str, Any] = {}
            source_preserved = False
            readable_custody = not set(reasons).intersection(
                {"original_not_native_testing", "foreign_original", "current_or_foreign_target"}
            )
            try:
                require_candidate_target_custody(store, candidate)
            except ValueError:
                readable_custody = False
                reasons = (*reasons, "current_custody_unavailable")
            if readable_custody:
                readback[ReadbackPredicate.FILES_READ] = "unavailable"
                try:
                    source_preserved = _source_preserved(
                        store, candidate, source_proof, verified_sources
                    )
                    readback[ReadbackPredicate.FILES_READ] = (
                        "pass" if source_preserved else "unavailable"
                    )
                except (OSError, native_copy_stream.NativeSourceUnavailable):
                    readback[ReadbackPredicate.FILES_READ] = "unavailable"
                    source_proof.clear()
                except ValueError:
                    readback[ReadbackPredicate.FILES_READ] = "fail"
                    source_proof.clear()
            if completed_copy(candidate) and not source_preserved:
                reasons = (*reasons, "completed_copy_requires_disposition")
            readback[ReadbackPredicate.NATIVE_READ] = "unavailable"
            if packet is not None:
                packet.checks["source_preserved"] = "pass" if source_preserved else "unavailable"
                packet.checks["native_inventory"] = "unavailable"
            observed, provider_reason = inspect_candidate_resources(
                root, store, candidate, **({"evidence": packet} if packet is not None else {})
            )
            if packet is not None:
                packet.checks["native_inventory"] = "pass" if observed["volumes"] else "unavailable"

            readback[ReadbackPredicate.NATIVE_READ] = (
                "pass" if observed["volumes"] else "unavailable"
            )
            if candidate.namespace == receiver_namespace:
                capacity_refusal = observed["capacity_refusal"]
                capacities = project_capacity(candidate.server_id, observed)
            reasons = (*reasons, *((provider_reason,) if provider_reason else ()))
            reasons = tuple(dict.fromkeys((*reasons, *observed.get("reasons", ()))))
            lifecycle: dict[str, Any] = {}
            if readable_custody and any(
                row.get("referenced") or row.get("reason") == "container_reference"
                for row in observed["volumes"]
            ):
                readback[ReadbackPredicate.CUSTODY] = "unavailable"
                if packet is not None:
                    packet.lifecycle.checks["lifecycle_acquisition"] = "unavailable"
                try:
                    lifecycle = candidate_lifecycle_custody(
                        root,
                        store,
                        candidate,
                        observed.get("project", ""),
                        **({"evidence": packet.lifecycle} if packet is not None else {}),
                    )
                    readback[ReadbackPredicate.CUSTODY] = "pass"
                    if packet is not None:
                        packet.lifecycle.checks["lifecycle_acquisition"] = "pass"
                except native_copy_stream.NativeProgramFailed as error:
                    readback[ReadbackPredicate.CUSTODY] = (
                        "unavailable" if error.unavailable else "fail"
                    )
                    readback[error.predicate] = "unavailable" if error.unavailable else "fail"
                except (ValueError, OSError, click.ClickException) as error:
                    if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied) or (
                        isinstance(error, api.DokployRequestFailed)
                        and error.status_code in {401, 403}
                    ):
                        raise
                    readback[ReadbackPredicate.CUSTODY] = "unavailable"
            resources = [
                candidate_resource_observation(candidate, row) for row in observed["volumes"]
            ]
            references = (
                *references,
                *(
                    NativeTestingSweepReference(
                        reason=reason, kind="retained_volume", record_id=row["name"]
                    )
                    for row in observed["volumes"]
                    for reason in dict.fromkeys(
                        [
                            *row.get("reasons", ()),
                            *((row["reason"],) if row.get("reason") else ()),
                            *(("source_inputs_preserved",) if row.get("source_inputs") else ()),
                            *(("container_reference",) if row.get("referenced") else ()),
                        ]
                    )
                    if reason != "provider_absent"
                ),
            )
            bindings = []
            lifecycle_blocked = False
            for index, row in enumerate(observed["volumes"]):
                volume_evidence = (
                    VolumeCustodyEvidence(
                        role=row["role"],
                        name_sha256=canonical_json_sha256(row["name"]),
                        identity_sha256=row.get("identity"),
                        engine_sha256=row.get("engine_identity"),
                        backing_sha256=resources[index].backing_sha256 or None,
                        referenced=row.get("referenced"),
                        empty=row.get("empty"),
                        source_inputs=row.get("source_inputs"),
                        gate_unexpected=row.get("gate_unexpected"),
                        checks={"provider_inventory": "not_reached", "physical": "not_reached"},
                    )
                    if packet is not None
                    else None
                )
                if packet is not None and volume_evidence is not None:
                    packet.volumes = [
                        volume_evidence if item.role == volume_evidence.role else item
                        for item in packet.volumes
                    ]
                resource_readback = resources[index].readback
                qualified_reason = "physical_ownership_unknown"
                holds = list(
                    dict.fromkeys((*reasons, *row.get("reasons", ()), row.get("reason", "")))
                )
                holds = [reason for reason in holds if reason]
                if lifecycle and source_preserved and "container_reference" in holds:
                    holds.remove("container_reference")
                status: SweepStatus = "held"
                if row["status"] == "absent":
                    status = "absent"
                elif row["status"] == "observed":
                    if not row["empty"] and not source_preserved:
                        holds.append("only_needed_copy_unqualified")
                        references = (
                            *references,
                            NativeTestingSweepReference(
                                reason="only_needed_copy_unqualified",
                                kind="retained_volume",
                                record_id=row["name"],
                            ),
                        )
                    if readable_custody:
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
                        inventory_references: list[dict[str, str]] = []
                        resource_readback[ReadbackPredicate.STORAGE_CONSUMERS] = "unavailable"
                        try:
                            inventory_clear = _inventory_clear(
                                root,
                                binding,
                                ignored_compose_id=candidate.compose_id,
                                exact=True,
                                references=inventory_references,
                                **(
                                    {"evidence": volume_evidence.consumers}
                                    if volume_evidence is not None
                                    else {}
                                ),
                            )
                        except (
                            ValueError,
                            OSError,
                            click.ClickException,
                            ProviderMutationUnknownError,
                        ) as error:
                            if isinstance(
                                error, native_copy_stream.NativeSourceAuthorizationDenied
                            ):
                                raise
                            if isinstance(
                                error, api.DokployRequestFailed
                            ) and error.status_code in {401, 403}:
                                raise
                            inventory_clear = False
                            inventory_references.append(
                                {
                                    "reason": "provider_inventory_unavailable",
                                    "kind": "provider_inventory",
                                }
                            )
                        finally:
                            for reference in inventory_references:
                                item_reference = NativeTestingSweepReference.model_validate(
                                    reference
                                )
                                if item_reference not in references:
                                    references = (*references, item_reference)
                        if volume_evidence is not None:
                            volume_evidence.checks["provider_inventory"] = (
                                "pass" if inventory_clear else "unavailable"
                            )
                        if not inventory_clear:
                            inventory_holds = [item["reason"] for item in inventory_references] or [
                                "provider_inventory_unavailable"
                            ]
                            holds.extend(inventory_holds)
                            resource_readback[ReadbackPredicate.STORAGE_CONSUMERS] = (
                                "fail"
                                if "foreign_provider_reference" in inventory_holds
                                else "unavailable"
                            )
                        else:
                            resource_readback[ReadbackPredicate.STORAGE_CONSUMERS] = "pass"
                        resource_readback[ReadbackPredicate.RESOURCES] = "unavailable"
                        if volume_evidence is not None:
                            volume_evidence.checks["physical"] = "unavailable"
                            volume_evidence.planned_removal_sha256 = (
                                canonical_json_sha256(
                                    {
                                        "ids": lifecycle["payload"]["container_ids"],
                                        "snapshot": lifecycle["payload"]["container_snapshot"],
                                    }
                                )
                                if lifecycle
                                else None
                            )
                        try:
                            physical_status, physical_reason = _host_result(
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
                            resource_readback[ReadbackPredicate.RESOURCES] = (
                                "pass"
                                if physical_status in {"eligible", "absent"}
                                else "unavailable"
                                if physical_status == "unknown"
                                else "fail"
                            )
                            if volume_evidence is not None:
                                volume_evidence.checks["physical"] = (
                                    "pass"
                                    if physical_status in {"eligible", "absent"}
                                    else "unavailable"
                                    if physical_status == "unknown"
                                    else "fail"
                                )
                            if physical_status not in {"eligible", "absent"}:
                                holds.append(physical_reason or "physical_ownership_unknown")
                            elif not holds:
                                status = physical_status
                                qualified_reason = physical_reason
                        except (
                            ValueError,
                            OSError,
                            click.ClickException,
                            ProviderMutationUnknownError,
                        ) as error:
                            if isinstance(
                                error, native_copy_stream.NativeSourceAuthorizationDenied
                            ) or (
                                isinstance(error, api.DokployRequestFailed)
                                and error.status_code in {401, 403}
                            ):
                                raise
                            holds.append("physical_ownership_unknown")
                        bindings.append(
                            {"binding": binding.model_dump(mode="json"), "permit": permit}
                        )
                for reason in dict.fromkeys(holds):
                    if reason in reasons or reason == "provider_absent":
                        continue
                    resource_reference = NativeTestingSweepReference(
                        reason=reason, kind="retained_volume", record_id=row["name"]
                    )
                    if resource_reference not in references:
                        references = (*references, resource_reference)
                if status not in {"eligible", "absent"} and (
                    not holds or any(reason != "source_inputs_preserved" for reason in holds)
                ):
                    lifecycle_blocked = True
                resources[index] = resources[index].model_copy(
                    update={
                        "status": status,
                        "reason": next(iter(holds), qualified_reason),
                    }
                )
            if not resources:
                reasons = (*reasons, "physical_inventory_unknown")
            if lifecycle and lifecycle_blocked:
                lifecycle = {}
                if not reasons:
                    reasons = ("candidate_lifecycle_custody_incomplete",)
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
            if packet is not None:
                packet.lifecycle.checks["lifecycle_usable"] = (
                    "pass"
                    if lifecycle
                    else "fail"
                    if packet.lifecycle.checks.get("lifecycle_acquisition") == "pass"
                    else "unavailable"
                )
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
                references=references,
                readback=readback,
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
                reasons=tuple(dict.fromkeys((*reasons, "current_custody_unavailable"))),
                ownership_sha256="",
                resources=tuple(
                    resource.model_copy(update={"status": "held"}) for resource in resources
                ),
                readback=readback,
                references=references,
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
        self.store, self.root, self.request = store, root, request
        self.plan, self.private = plan, private
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

    def _result(self, candidates: list[NativeTestingSweepCandidate]) -> dict[str, Any]:
        capacity: tuple[NativeTestingBackingCapacity, ...] = ()
        refusal = "receiver_candidate_unavailable"
        try:
            receiver = self.store.read_testing_candidate(
                testing_candidate_namespace(
                    self.request.product,
                    self.request.context,
                    self.request.operation_id
                    if self.request.receiver_operation_id is None
                    else self.request.receiver_operation_id,
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
        parent_record = (
            lease.evidence_reservation().record_id
            if isinstance(lease, ProviderEvidenceLease)
            else ""
        )
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
                    from control_plane.contracts.testing_candidate_release import (
                        TESTING_CANDIDATE_RELEASE_ROUTE,
                    )

                    own_records = frozenset(
                        record.record_id
                        for phase in ("stop", "remove")
                        if (
                            record := self.store.read_idempotency_record(
                                scope="native-testing-retention-lifecycle",
                                route_path=TESTING_CANDIDATE_RELEASE_ROUTE,
                                idempotency_key=candidate_lifecycle_key(
                                    provider_operation_key, candidate.namespace, phase
                                ),
                            )
                        )
                        is not None
                    )
                    if parent_record:
                        own_records = own_records | {parent_record}
                    ownership, reasons, _ = candidate_authority(
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
                        if self.check_current_use is not None:
                            self.check_current_use(original)
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
                for item in gate_volume_last(candidate.resources):
                    if item.status != "eligible":
                        items.append(item)
                        continue
                    row = bindings[item.volume_identity_sha256]
                    binding = RetainedVolumeBinding.model_validate(row["binding"])
                    effect_permit: dict[str, Any] = {}

                    def guard() -> None:
                        if self.check_current_use is not None:
                            self.check_current_use(original)
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
                roles = [item.role for item in candidate.resources]
                items.sort(key=lambda item: roles.index(item.role))
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
