"""Release a failed native testing candidate's private containers and volumes."""

from collections.abc import Callable
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from typing import cast

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.contracts.testing_candidate_release import (
    TestingCandidateReleasePlan,
    TestingCandidateReleaseResponse,
    TestingCandidateReleaseStatus,
)
from control_plane.disposable_lanes import recorded_lane_protection
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.dokploy_target_setup_http import fetch_dokploy_compose_domains_for_target_setup
from control_plane.lane_usage import evaluate_lane_usage
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_usage import read_odoo_testing_usage
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
)
from control_plane.retained_volume_capture import (
    _batched_docker_inspect,
    retained_volume_docker_command,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import (
    candidate_resources,
    require_candidate_target_custody,
)
from control_plane.testing_publication_storage import read_publication

RELEASE_TIMEOUT_SECONDS = 600
RELEASE_PLAN_ERRORS = (ValueError, OSError, click.ClickException, ProviderMutationUnknownError)

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
    if (spec.get('mode') not in {'dry-run','apply'} or not isinstance(resources,dict)
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

def used_bytes(volumes,own):
    try:
        total=0
        for path in volumes.values():
            total+=int(subprocess.run(['du','-sxB1',str(path)],capture_output=True,check=True,timeout=240).stdout.split()[0])
        for row in json_value('container','inspect','--size',*own) if own else ():
            total+=int(row.get('SizeRw') or 0)
        return total
    except Exception:
        return None

def release(spec):
    mode,resources=qualified_request(spec)
    namespace=resources['namespace']
    volumes=owned_volumes(namespace,resources_label(resources))
    own=candidate_containers(namespace,volumes)
    if not volumes and not own:
        return {'status':'absent','reclaimable_bytes':0}
    size=used_bytes(volumes,own)
    if mode=='dry-run':
        return {'status':'eligible','reclaimable_bytes':size}
    if own:
        docker('stop',*own,timeout=120)
        docker('rm',*own,timeout=120)
    for name in volumes:
        docker('volume','rm',name,timeout=120)
    if owned_volumes(namespace,resources_label(resources)) or candidate_containers(namespace,{}):
        raise RuntimeError('Release absence is unverified')
    return {'status':'released','reclaimable_bytes':size}

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


def require_failed_testing_operation(
    operation: OdooStableTargetReplacementOperationRecord, product: str
) -> None:
    if operation.product != product or not operation.is_native_testing_rebuild:
        raise TestingCandidateReleaseRefused(
            "candidate_release_not_testing", "Only a native testing rebuild has a candidate."
        )
    if operation.status != "fail" or not operation.error_code:
        raise TestingCandidateReleaseRefused(
            "candidate_release_outcome_unrecorded",
            "Release waits until the operation has failed with a recorded cause.",
        )


def require_candidate_unselected(
    root: Path, store: PostgresRecordStore, allocation: TestingCandidateAllocation
) -> None:
    try:
        require_candidate_target_custody(store, allocation)
    except ValueError as error:
        raise TestingCandidateReleaseRefused(
            "candidate_release_selected", "A lane target names this candidate."
        ) from error
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
    if publication is not None and publication.state != "reversed":
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
) -> None:
    target = store.read_dokploy_target_record(
        context_name=operation.context, instance_name="testing"
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=operation.context, instance_name="testing"
    )
    safety = store.read_disposable_lane_safety(operation.product, operation.context, "testing")
    protection = recorded_lane_protection(target, safety)
    if protection.only_copy:
        raise TestingCandidateReleaseRefused(
            "testing_needed_copy", "Testing holds the only needed copy of data."
        )
    if protection.shared:
        raise TestingCandidateReleaseRefused(
            "testing_production_resources_shared",
            "Testing shares production resources; nothing is released.",
        )
    usage = read_odoo_testing_usage(
        target=target,
        target_id=target_id,
        control_plane_root=root,
        database_url=store.database_url,
        record_store=store,
        product=operation.product,
    )
    reported = (
        "in_use" if safety is not None and safety.current_usage() == "in_use" else usage.usage
    )
    decision = evaluate_lane_usage(
        store,
        profile=store.read_product_profile_record(operation.product),
        target=target,
        target_id=target_id,
        instance="testing",
        usage=reported,
        require_unused=False,
    )
    if decision.protected:
        raise TestingCandidateReleaseRefused(
            "testing_in_use", "A person is using this testing lane."
        )


def host_release(
    root: Path,
    server_id: str,
    resources: OdooTestingResources,
    mode: str,
    lease: ProviderOperationLease | None = None,
) -> HostRelease:
    host, token = source.read_dokploy_config(control_plane_root=root)
    frames: list[dict[str, object]] = []

    def dispatch(send: Callable[[], None]) -> None:
        if lease is None:
            raise ValueError("A candidate release requires its current operation lease.")
        lease.checkpoint_effect("release-candidate:" + resources.namespace)
        send()

    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=server_id,
        program=TESTING_CANDIDATE_RELEASE_PROGRAM,
        payload={"mode": mode, "resources": resources.model_dump(mode="json")},
        receive=frames.append,
        timeout_seconds=RELEASE_TIMEOUT_SECONDS,
        dispatch_guard=dispatch if mode == "apply" else None,
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
    root: Path, store: PostgresRecordStore, product: str, operation_id: str
) -> CandidateRelease:
    operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
    require_failed_testing_operation(operation, product)
    allocation = store.read_testing_candidate(
        testing_candidate_namespace(product, operation.context, operation_id)
    )
    if allocation is None or not allocation.compose_id:
        raise TestingCandidateReleaseRefused(
            "candidate_release_no_candidate", "This operation never placed a candidate."
        )
    require_candidate_unselected(root, store, allocation)
    require_lane_releasable(root, store, operation)
    resources = candidate_resources(allocation)
    observed = host_release(root, allocation.server_id, resources, "dry-run")
    if observed.status not in {"eligible", "absent"}:
        raise TestingCandidateReleaseRefused(
            "candidate_release_held",
            "The candidate's storage is not provably its own: "
            + (observed.reason_code or "host_evidence_unavailable"),
        )
    plan = TestingCandidateReleasePlan(
        product=product,
        operation_id=operation_id,
        error_code=operation.error_code,
        namespace=allocation.namespace,
        resources_binding_sha256=resources.binding_sha256,
    )
    return CandidateRelease(
        plan,
        resources,
        allocation.server_id,
        cast(TestingCandidateReleaseStatus, observed.status),
        observed.reclaimable_bytes,
    )


def release_response(
    release: CandidateRelease, trace_id: str, status: TestingCandidateReleaseStatus
) -> TestingCandidateReleaseResponse:
    return TestingCandidateReleaseResponse(
        trace_id=trace_id,
        product=release.plan.product,
        operation_id=release.plan.operation_id,
        error_code=release.plan.error_code,
        plan_sha256=release.plan.digest(),
        status=status,
        reclaimable_bytes=release.reclaimable_bytes,
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

    def target_key(self) -> str:
        return "testing-candidate-release:" + self.release.plan.namespace

    def reconciliation_key(self) -> str:
        return json.dumps(
            {
                "plan": self.release.plan.model_dump(mode="json"),
                "resources": self.release.resources.model_dump(mode="json"),
                "server_id": self.release.server_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _payload(self, status: TestingCandidateReleaseStatus) -> dict[str, object]:
        return release_response(self.release, self.trace_id, status).model_dump(
            mode="json", exclude_none=True
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        try:
            observed = host_release(
                self.root, self.release.server_id, self.release.resources, "dry-run"
            )
        except (ValueError, OSError, click.ClickException, ProviderMutationUnknownError):
            return ProviderObservation("unknown")
        if observed.status != "absent":
            return ProviderObservation("unknown")
        return ProviderObservation("present", response_payload=self._payload("released"))

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        plan = self.release.plan
        try:
            fresh = build_candidate_release(self.root, self.store, plan.product, plan.operation_id)
        except RELEASE_PLAN_ERRORS as error:
            raise ProviderMutationRejectedError(error) from error
        if fresh.plan != plan or plan.digest() != self.reviewed_plan_sha256:
            raise ProviderMutationRejectedError(ValueError("The reviewed candidate changed."))
        if fresh.status == "absent":
            return ProviderMutationOutcome(
                202, self._payload("absent"), provider_effect_performed=False
            )
        lease.assert_current()
        try:
            applied = host_release(
                self.root, self.release.server_id, self.release.resources, "apply", lease
            )
        except (ValueError, OSError, click.ClickException) as error:
            raise ProviderMutationUnknownError(
                "Candidate release transport is unverified."
            ) from error
        if applied.status != "released":
            raise ProviderMutationUnknownError("Candidate release is unverified.")
        return ProviderMutationOutcome(202, self._payload("released"))
