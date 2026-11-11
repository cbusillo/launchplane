"""Release a failed native testing candidate's private containers and volumes."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.candidate_custody_evidence import CandidateCustodyEvidence
from control_plane.contracts.testing_candidate_release import (
    AUTOMATIC_RELEASE_REASONS_MAX,
    TESTING_CANDIDATE_RELEASE_ROUTE,
    AutomaticCandidateReleaseView,
    TestingCandidateReleasePlan,
    TestingCandidateReleaseRequest,
    TestingCandidateReleaseResponse,
    TestingCandidateReleaseStatus,
)
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.dokploy_target_setup_http import fetch_dokploy_compose_domains_for_target_setup
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_usage import read_odoo_testing_usage
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.retained_volume_capture import (
    _batched_docker_inspect,
    retained_volume_docker_command,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import (
    candidate_resources,
)
from control_plane.testing_publication_storage import read_publication
from control_plane.contracts.testing_candidate_retention import (
    NativeTestingSweepRequest,
    NativeTestingSweepResponse,
)
from control_plane.testing_candidate_retention import (
    NativeTestingSweepAdapter,
    build_native_testing_sweep,
)

RELEASE_TIMEOUT_SECONDS = 600
RELEASE_LEASE_SECONDS = 900
RELEASE_PLAN_ERRORS = (ValueError, OSError, click.ClickException, ProviderMutationUnknownError)
AUTOMATIC_RELEASE_KEY = "automatic"
AUTOMATIC_RELEASE_ACTOR = "launchplane:failed-testing-candidate"

TESTING_CANDIDATE_RELEASE_PROGRAM = (
    "from collections.abc import Callable,Sequence\n"
    + inspect.getsource(_batched_docker_inspect)
    + inspect.getsource(retained_volume_docker_command)
    + r"""
import base64,hashlib,json,re,subprocess,sys,time
from pathlib import Path

prefix=sys.argv[2]
RESOURCE_FIELDS={'product','context','instance','operation_id','target_id','namespace'}

class Held(Exception):
    pass

def emit(result):
    print(prefix+json.dumps({'type':'complete','result':result},sort_keys=True,separators=(',',':')),flush=True)

def docker(*arguments,timeout=60):
    result=subprocess.run(retained_volume_docker_command(*arguments),capture_output=True,check=False,timeout=timeout)
    if result.returncode:
        raise RuntimeError('Docker command failed')
    return result.stdout.decode()

def json_value(*arguments):
    return json.loads(docker(*arguments))

def qualified_request(spec):
    resources=spec.get('resources')
    if (spec.get('mode') != 'dry-run' or not isinstance(resources,dict)
        or set(resources)!=RESOURCE_FIELDS or not all(isinstance(value,str) and value for value in resources.values())
        or resources['instance']!='testing'
        or re.fullmatch(r'lp-testing-[a-f0-9]{32}',resources['namespace']) is None):
        raise Held('invalid_request')
    return spec['mode'],resources

def resources_label(resources):
    return hashlib.sha256(json.dumps(resources,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def owned_volumes(namespace,label):
    names={namespace+suffix for suffix in ('-data','-db','-logs')}
    present=sorted(names & set(docker('volume','ls','--format','{{.Name}}').splitlines()))
    paths={}
    for name in present:
        found=json_value('volume','inspect',name)
        volume=found[0] if isinstance(found,list) and len(found)==1 else {}
        labels=volume.get('Labels') or {}
        path=Path(str(volume.get('Mountpoint','')))
        if (volume.get('Name')!=name or volume.get('Driver')!='local'
            or volume.get('Options') not in (None,{})
            or labels.get('io.launchplane.testing-resources')!=label
            or not path.is_absolute()):
            raise Held('volume_ownership_unverified')
        path=path.resolve(strict=True)
        if path.name!='_data' or path.parent.name!=name or not path.is_dir():
            raise Held('volume_ownership_unverified')
        paths[name]=path
    return paths

def overlaps(source,paths):
    return any(source==path or source in path.parents or path in source.parents for path in paths)

def candidate_containers(namespace,volumes):
    project=re.compile(re.escape(namespace)+r'(?:-[a-z0-9]{6})?')
    identifiers=docker('ps','-aq','--no-trunc').split()
    own=[]
    for identifier,row in _batched_docker_inspect('container',identifiers,json_value).items():
        labels=(row.get('Config') or {}).get('Labels') or {}
        if project.fullmatch(str(labels.get('com.docker.compose.project',''))):
            own.append(identifier)
            continue
        for mount in row.get('Mounts') or ():
            if mount.get('Type')=='tmpfs':
                continue
            if mount.get('Name') in volumes:
                raise Held('volume_has_other_consumer')
            source=mount.get('Source')
            if not isinstance(source,str) or not source.startswith('/'):
                raise Held('container_backing_unavailable')
            source=Path(source).resolve()
            parent_read_only=mount.get('Type')=='bind' and mount.get('RW') is False and not any(
                source==path or path in source.parents for path in volumes.values())
            if not parent_read_only and overlaps(source,volumes.values()):
                raise Held('volume_has_other_consumer')
    return own

def release(spec):
    mode,resources=qualified_request(spec)
    namespace=resources['namespace']
    volumes=owned_volumes(namespace,resources_label(resources))
    own=candidate_containers(namespace,volumes)
    if not volumes and not own:
        return {'status':'absent','reclaimable_bytes':0}
    return {'status':'eligible','reclaimable_bytes':None}

try:
    emit(release(json.loads(base64.b64decode(sys.argv[1],validate=True))))
except Held as error:
    emit({'status':'held','reason_code':str(error)})
except Exception:
    emit({'status':'unknown','reason_code':'host_evidence_unavailable'})
"""
)


class TestingCandidateReleaseRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HostRelease:
    status: str
    reclaimable_bytes: int | None
    reason_code: str


@dataclass(frozen=True)
class CandidateRelease:
    plan: TestingCandidateReleasePlan
    resources: OdooTestingResources
    server_id: str
    status: TestingCandidateReleaseStatus
    reclaimable_bytes: int | None
    retention: NativeTestingSweepResponse | None = None
    private: dict[str, Any] = field(default_factory=dict)
    request: NativeTestingSweepRequest | None = None
    evidence: CandidateCustodyEvidence | None = None


def require_failed_testing_operation(
    operation: OdooStableTargetReplacementOperationRecord, product: str
) -> None:
    if operation.product != product or not operation.is_native_testing_rebuild:
        raise TestingCandidateReleaseRefused(
            "candidate_release_not_testing", "Only a native testing rebuild has a candidate."
        )
    if operation.status != "fail":
        raise TestingCandidateReleaseRefused(
            "candidate_release_outcome_unrecorded",
            "Release waits until the operation has failed.",
        )


def require_candidate_unselected(
    root: Path, store: PostgresRecordStore, allocation: TestingCandidateAllocation
) -> None:
    if any(
        row.target_id == allocation.compose_id for row in store.list_dokploy_target_id_records()
    ):
        raise TestingCandidateReleaseRefused(
            "candidate_release_selected", "A lane target names this candidate."
        )
    publication = read_publication(
        store,
        canonical_json_sha256(
            {
                "product": allocation.product,
                "context": allocation.context,
                "operation": allocation.operation_id,
            }
        ),
    )
    if publication is not None and publication.state == "applied":
        raise TestingCandidateReleaseRefused(
            "candidate_release_selected", "This candidate was selected to serve testing."
        )
    if candidate_routed(root, store, allocation.compose_id):
        raise TestingCandidateReleaseRefused(
            "candidate_release_selected", "This candidate has a public route."
        )


def candidate_routed(root: Path, store: PostgresRecordStore, compose_id: str) -> bool:
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    try:
        return bool(
            fetch_dokploy_compose_domains_for_target_setup(
                host=host, token=token, compose_id=compose_id
            )
        )
    except api.DokployRequestFailed as error:
        if error.status_code == 404:
            return False
        raise


def require_lane_releasable(
    root: Path, store: PostgresRecordStore, operation: OdooStableTargetReplacementOperationRecord
) -> str:
    target = store.read_dokploy_target_record(
        context_name=operation.context, instance_name="testing"
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=operation.context, instance_name="testing"
    )
    usage = read_odoo_testing_usage(
        target=target,
        target_id=target_id,
        control_plane_root=root,
        database_url=store.database_url,
        record_store=store,
        product=operation.product,
    )
    if usage.recent_signins:
        raise TestingCandidateReleaseRefused(
            "testing_in_use", "This testing lane has a sign-in within the recent-use window."
        )
    if usage.recent_signins is None:
        logging.getLogger(__name__).warning(
            "Testing candidate release: recent sign-in read unavailable (%s)", usage.reason_code
        )

    return usage.reason_code if usage.recent_signins is None else ""


def host_release(
    root: Path,
    server_id: str,
    resources: OdooTestingResources,
    mode: str,
    lease: ProviderOperationLease | None = None,
) -> HostRelease:
    host, token = source.read_dokploy_config(control_plane_root=root)
    frames: list[dict[str, object]] = []

    if mode != "dry-run" or lease is not None:
        raise ValueError("Original release receipts permit observation only.")

    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=server_id,
        program=TESTING_CANDIDATE_RELEASE_PROGRAM,
        payload={"mode": mode, "resources": resources.model_dump(mode="json")},
        receive=frames.append,
        timeout_seconds=RELEASE_TIMEOUT_SECONDS,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ProviderMutationUnknownError("Candidate release host result is incomplete.")
    result = frames[0].get("result")
    if not isinstance(result, dict):
        raise ProviderMutationUnknownError("Candidate release host result is invalid.")
    status, reclaimable = result.get("status"), result.get("reclaimable_bytes")
    if not isinstance(status, str):
        raise ProviderMutationUnknownError("Candidate release host status is invalid.")
    return HostRelease(
        status=status,
        reclaimable_bytes=reclaimable if isinstance(reclaimable, int) else None,
        reason_code=str(result.get("reason_code", "")),
    )


def build_candidate_release(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    operation_id: str,
    receiver_operation_id: str | None = None,
    *,
    include_evidence: bool = False,
) -> CandidateRelease:
    operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
    if operation.product != product or not operation.is_native_testing_rebuild:
        raise TestingCandidateReleaseRefused(
            "candidate_release_not_testing",
            "Only a native testing original can inspect candidate storage.",
        )
    allocation = store.read_testing_candidate(
        testing_candidate_namespace(product, operation.context, operation_id)
    )
    if allocation is None or not allocation.compose_id:
        raise TestingCandidateReleaseRefused(
            "candidate_release_no_candidate", "This operation never placed a candidate."
        )
    request = NativeTestingSweepRequest(
        kind="native-testing",
        product=product,
        context=operation.context,
        operation_id=operation_id,
        receiver_operation_id=receiver_operation_id or "",
        candidates=(allocation.namespace,),
    )
    packets: list[CandidateCustodyEvidence] = []
    observed, private = build_native_testing_sweep(
        store,
        root,
        request,
        "candidate-release-plan",
        **({"evidence": packets} if include_evidence else {}),
    )
    candidate = next(item for item in observed.candidates if item.selected)
    status: TestingCandidateReleaseStatus = (
        "eligible"
        if candidate.status == "eligible"
        else "absent"
        if candidate.resources and all(row.status == "absent" for row in candidate.resources)
        else "held"
    )
    resources = candidate_resources(allocation)
    plan = TestingCandidateReleasePlan(
        product=product,
        operation_id=operation_id,
        error_code=operation.error_code,
        namespace=allocation.namespace,
        resources_binding_sha256=resources.binding_sha256,
        retention_plan_sha256=observed.plan_sha256,
    )
    packet = packets[0] if packets else None
    if packet is not None:
        packet.plan_sha256 = plan.digest()
        packet.completed_at = datetime.now(timezone.utc)
        packet.packet_sha256 = canonical_json_sha256(
            packet.model_dump(mode="json", exclude={"packet_sha256"}, exclude_none=True)
        )
    return CandidateRelease(
        plan, resources, allocation.server_id, status, None, observed, private, request, packet
    )


def release_response(
    release: CandidateRelease,
    trace_id: str,
    status: TestingCandidateReleaseStatus,
    reason_code: str = "",
) -> TestingCandidateReleaseResponse:
    return TestingCandidateReleaseResponse(
        trace_id=trace_id,
        product=release.plan.product,
        operation_id=release.plan.operation_id,
        error_code=release.plan.error_code,
        plan_sha256=release.plan.digest(),
        status=status,
        reclaimable_bytes=release.reclaimable_bytes,
        evidence=release.evidence,
        candidates=release.retention.candidates if release.retention else (),
        capacity=release.retention.capacity if release.retention else (),
        capacity_refusal=release.retention.capacity_refusal if release.retention else "",
        reason_code=reason_code or None,
    )


class TestingCandidateReleaseAdapter:
    def __init__(
        self,
        store: PostgresRecordStore,
        root: Path,
        release: CandidateRelease,
        reviewed_plan_sha256: str,
        trace_id: str,
    ) -> None:
        self.store, self.root, self.release = store, root, release
        self.reviewed_plan_sha256, self.trace_id = reviewed_plan_sha256, trace_id
        self.fresh: CandidateRelease | None = None

    def target_key(self) -> str:
        return "testing-candidate-release:" + self.release.plan.namespace

    def reconciliation_key(self) -> str:
        payload = {
            "plan": self.release.plan.model_dump(
                mode="json",
                exclude={"retention_plan_sha256"}
                if not self.release.plan.retention_plan_sha256
                else set(),
            ),
            "resources": self.release.resources.model_dump(mode="json"),
            "server_id": self.release.server_id,
            "reclaimable_bytes": self.release.reclaimable_bytes,
        }
        if self.release.retention is not None:
            assert self.release.request is not None
            payload.update(
                retention=self.release.retention.model_dump(mode="json"),
                private=self.release.private,
                request=self.release.request.model_dump(mode="json"),
            )
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _payload(self, status: TestingCandidateReleaseStatus) -> dict[str, object]:
        return release_response(self.release, self.trace_id, status).model_dump(
            mode="json", exclude_none=True
        )

    def _use_guard(self, allocation: TestingCandidateAllocation) -> None:
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            allocation.operation_id
        )
        require_failed_testing_operation(operation, self.release.plan.product)
        require_candidate_unselected(self.root, self.store, allocation)
        require_lane_releasable(self.root, self.store, operation)

    def _adapter(self) -> NativeTestingSweepAdapter:
        assert self.release.request is not None and self.release.retention is not None
        request = self.release.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": self.release.retention.plan_sha256}
        )
        return NativeTestingSweepAdapter(
            self.store,
            self.root,
            request,
            self.release.retention,
            self.release.private,
            self._use_guard,
        )

    def _physical_payload(self, payload: dict[str, object]) -> dict[str, object]:
        observed = NativeTestingSweepResponse.model_validate(payload)
        selected = next(item for item in observed.candidates if item.selected)
        status: TestingCandidateReleaseStatus = (
            "released"
            if selected.resources
            and all(row.status in {"absent", "removed"} for row in selected.resources)
            else "held"
        )
        return release_response(
            replace(self.release, retention=observed), self.trace_id, status
        ).model_dump(mode="json", exclude_none=True)

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        if self.release.retention is None:
            try:
                observed = host_release(
                    self.root, self.release.server_id, self.release.resources, "dry-run"
                )
            except RELEASE_PLAN_ERRORS:
                return ProviderObservation("unknown")
            return (
                ProviderObservation("present", response_payload=self._payload("released"))
                if observed.status == "absent"
                else ProviderObservation("unknown")
            )
        adapter = self._adapter()
        observation = adapter.observe(
            provider_operation_key, provider_effect_phase, adapter.reconciliation_key()
        )
        return (
            replace(
                observation, response_payload=self._physical_payload(observation.response_payload)
            )
            if observation.outcome == "present"
            else observation
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        if self.release.retention is None:
            raise ProviderMutationUnknownError(
                "Preserve the original release receipt; current physical custody is unverified."
            )
        if self.store.database_dialect_name != "postgresql":
            raise ProviderMutationRejectedError(
                ValueError("Candidate release requires PostgreSQL executor exclusion.")
            )
        try:
            fresh = build_candidate_release(
                self.root,
                self.store,
                self.release.plan.product,
                self.release.plan.operation_id,
                self.release.request.receiver_operation_id if self.release.request else None,
            )
        except RELEASE_PLAN_ERRORS as error:
            raise ProviderMutationRejectedError(error) from error
        self.fresh = fresh
        if fresh.plan.digest() != self.reviewed_plan_sha256:
            raise ProviderMutationRejectedError(
                TestingCandidateReleaseRefused(
                    "candidate_release_plan_changed", "The reviewed candidate changed."
                )
            )
        if fresh.status == "held":
            raise ProviderMutationRejectedError(
                TestingCandidateReleaseRefused(
                    "candidate_release_held", "Current use or custody protects the candidate."
                )
            )
        if fresh.status == "absent":
            return ProviderMutationOutcome(
                202, self._payload("absent"), provider_effect_performed=False
            )
        result = self._adapter().apply(provider_operation_key, lease)
        return replace(result, response_payload=self._physical_payload(result.response_payload))


def release_from_receipt(reconciliation_key: str) -> CandidateRelease:
    recorded = json.loads(reconciliation_key)
    return CandidateRelease(
        TestingCandidateReleasePlan.model_validate(recorded["plan"]),
        OdooTestingResources.model_validate(recorded["resources"]),
        str(recorded["server_id"]),
        "eligible",
        recorded["reclaimable_bytes"],
        NativeTestingSweepResponse.model_validate(recorded["retention"])
        if "retention" in recorded
        else None,
        recorded.get("private", {}),
        NativeTestingSweepRequest.model_validate(recorded["request"])
        if "request" in recorded
        else None,
    )


def release_scope(product: str, operation_id: str) -> str:
    return f"testing-candidate-release:{product}:{operation_id}"


def release_fingerprint(
    actor: str, product: str, operation_id: str, request: TestingCandidateReleaseRequest
) -> str:
    normalized = request.model_copy(update={"mode": "apply"})
    return hashlib.sha256(
        "\n".join(
            (
                actor,
                product,
                operation_id,
                normalized.model_dump_json(exclude_none=True, exclude={"include_evidence"}),
            )
        ).encode()
    ).hexdigest()


def run_release_operation(
    store: PostgresRecordStore,
    *,
    product: str,
    operation_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    adapter: Any,
    allow_mutation: bool = True,
) -> DurableProviderOperationResult:
    return run_durable_provider_operation(
        store=store,
        scope=release_scope(product, operation_id),
        route_path=TESTING_CANDIDATE_RELEASE_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=trace_id,
        response_trace_id=trace_id,
        adapter=adapter,
        lease_seconds=RELEASE_LEASE_SECONDS,
        allow_mutation=allow_mutation,
    )


def read_automatic_receipt(
    store: PostgresRecordStore, product: str, operation_id: str
) -> LaunchplaneIdempotencyRecord | None:
    return store.read_idempotency_record(
        scope=release_scope(product, operation_id),
        route_path=TESTING_CANDIDATE_RELEASE_ROUTE,
        idempotency_key=AUTOMATIC_RELEASE_KEY,
    )


@dataclass(frozen=True)
class SettledCandidateRelease:
    target: str
    binding: str
    payload: dict[str, object]

    def target_key(self) -> str:
        return self.target

    def reconciliation_key(self) -> str:
        return self.binding

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase
        return ProviderObservation("absent" if reconciliation_key == self.binding else "unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        lease.assert_current()
        return ProviderMutationOutcome(202, self.payload, provider_effect_performed=False)


def _refusal_code(error: Exception) -> str:
    if isinstance(error, TestingCandidateReleaseRefused):
        return error.code
    if isinstance(error, native_copy_stream.NativeSourceAuthorizationDenied):
        return "authorization_denied"
    return "candidate_release_unavailable"


def _failed_original_with_candidate(
    store: PostgresRecordStore, operation_id: str
) -> OdooStableTargetReplacementOperationRecord | None:
    operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
    if not operation.is_native_testing_rebuild or operation.status != "fail":
        return None
    if not operation.error_code:
        return None
    allocation = store.read_testing_candidate(
        testing_candidate_namespace(operation.product, operation.context, operation_id)
    )
    return operation if allocation is not None and allocation.compose_id else None


def release_failed_candidate(store: PostgresRecordStore, root: Path, operation_id: str) -> None:
    operation = _failed_original_with_candidate(store, operation_id)
    if operation is None:
        return
    product, trace_id = operation.product, f"automatic-release-{operation_id}"
    if read_automatic_receipt(store, product, operation_id) is not None:
        return
    namespace = testing_candidate_namespace(product, operation.context, operation_id)

    def run(adapter: Any, plan_sha256: str) -> None:
        run_release_operation(
            store,
            product=product,
            operation_id=operation_id,
            idempotency_key=AUTOMATIC_RELEASE_KEY,
            request_fingerprint=release_fingerprint(
                AUTOMATIC_RELEASE_ACTOR,
                product,
                operation_id,
                TestingCandidateReleaseRequest(mode="apply", reviewed_plan_sha256=plan_sha256),
            ),
            trace_id=trace_id,
            adapter=adapter,
        )

    try:
        release = build_candidate_release(root, store, product, operation_id)
    except (TestingCandidateReleaseRefused, *RELEASE_PLAN_ERRORS) as error:
        binding = json.dumps(
            {"product": product, "operation_id": operation_id, "refusal": _refusal_code(error)},
            sort_keys=True,
            separators=(",", ":"),
        )
        reviewed = hashlib.sha256(binding.encode()).hexdigest()
        payload = TestingCandidateReleaseResponse(
            trace_id=trace_id,
            product=product,
            operation_id=operation_id,
            error_code=operation.error_code,
            plan_sha256=reviewed,
            status="held",
            reason_code=_refusal_code(error),
        ).model_dump(mode="json", exclude_none=True)
        run(
            SettledCandidateRelease("testing-candidate-release:" + namespace, binding, payload),
            reviewed,
        )
        return
    reviewed = release.plan.digest()
    effect = TestingCandidateReleaseAdapter(store, root, release, reviewed, trace_id)
    settled_status: TestingCandidateReleaseStatus = release.status
    reason_code, observed = "", release
    if release.status == "eligible":
        try:
            run(effect, reviewed)
            return
        except (TestingCandidateReleaseRefused, *RELEASE_PLAN_ERRORS) as error:
            settled_status, reason_code = "held", _refusal_code(error)
            if effect.fresh is not None:
                observed = replace(release, retention=effect.fresh.retention)
    payload = release_response(observed, trace_id, settled_status, reason_code).model_dump(
        mode="json", exclude_none=True
    )
    run(
        SettledCandidateRelease(effect.target_key(), effect.reconciliation_key(), payload), reviewed
    )


def _held_reasons(response: TestingCandidateReleaseResponse) -> tuple[str, ...]:
    reasons = [response.reason_code] if response.reason_code else []
    for candidate in response.candidates:
        if not candidate.selected:
            continue
        reasons.extend(candidate.reasons)
        reasons.extend(row.reason for row in candidate.resources if row.status == "held")
    return tuple(dict.fromkeys(reason for reason in reasons if reason))[
        :AUTOMATIC_RELEASE_REASONS_MAX
    ]


def _reviewed_plan_sha256(reconciliation_key: str) -> str:
    try:
        return release_from_receipt(reconciliation_key).plan.digest()
    except (ValueError, KeyError, TypeError):
        return hashlib.sha256(reconciliation_key.encode()).hexdigest()


def automatic_release_view(
    record: LaunchplaneIdempotencyRecord | None,
) -> AutomaticCandidateReleaseView | None:
    if record is None:
        return None
    if record.state != "completed":
        return AutomaticCandidateReleaseView(
            status="unknown",
            reasons=("mutation_reconciliation_required",),
            plan_sha256=_reviewed_plan_sha256(record.reconciliation_key),
            idempotency_key=AUTOMATIC_RELEASE_KEY,
        )
    try:
        response = TestingCandidateReleaseResponse.model_validate(record.response_payload)
    except ValueError:
        logging.getLogger(__name__).warning("Automatic candidate release receipt is unreadable.")
        return AutomaticCandidateReleaseView(
            status="unknown",
            reasons=("release_receipt_unreadable",),
            idempotency_key=AUTOMATIC_RELEASE_KEY,
        )
    status = response.status if response.status != "eligible" else "unknown"
    return AutomaticCandidateReleaseView(
        status=status,
        reasons=_held_reasons(response) if status == "held" else (),
        plan_sha256=response.plan_sha256,
        idempotency_key=AUTOMATIC_RELEASE_KEY,
    )


def read_automatic_release(
    store: PostgresRecordStore, product: str, operation_id: str
) -> AutomaticCandidateReleaseView | None:
    return automatic_release_view(read_automatic_receipt(store, product, operation_id))
