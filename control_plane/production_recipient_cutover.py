"""Accepted-release worker integration for independent private production recipients."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import (
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
    ArtifactIdentityReference,
    BackupGateEvidence,
)
from control_plane.contracts.deployment_record import ResolvedTargetEvidence, DeploymentRecord
from control_plane.contracts.ship_request import ShipRequest
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import (
    custody,
    ProductionRecipientAdapter,
    provider_binding_digest,
    qualified_render,
    runtime_values,
)
from control_plane.production_recipient_host import run_recipient_program
from control_plane.production_recipient_resources import inspect_resources
from control_plane.production_recipient_publication import (
    RecipientPublication,
    commit_publication,
    rollback_publication,
    republication_snapshot,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
    ProviderEvidenceLease,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.dokploy_deploy import execute_dokploy_artifact_deploy
from control_plane.workflows.generic_web_promotion import _verify_health_evidence_with_identity
from control_plane.workflows.production_promotion_backup import (
    GENERIC_WEB_PROMOTION_BACKUP_ACTION,
    _read_operation,
    _source_key,
    require_production_promotion_backup,
)
from control_plane.dokploy import api, source

from control_plane.production_recipient_evidence import (
    WRITER_ACTION_MARKER,
    WRITER_BINDING_MARKER,
    RUNTIME_OK_MARKER,
)

CUTOVER_ROUTE = "/v1/products/{product}/production-recipient/cutover"


def cutover_reconciliation_key(preparation: str) -> str:
    return "production-recipient-cutover:" + preparation


class RecipientCutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    health_urls: dict[str, str]
    backup_sources: dict[str, str]
    writer_binding_id: str = Field(min_length=1)
    destination_backup_operation_id: str = Field(min_length=1)
    readiness_url: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientCutoverPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    plan_sha256: str
    armed: bool
    bootstrap_eligible: Literal[False] = False
    release_required: Literal[True] = True


def cutover_intent(
    root: Path,
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    request: RecipientCutoverRequest,
    *,
    check_freshness: bool = True,
) -> dict[str, Any]:
    if request.preparation_plan_sha256 != record.plan_sha256 or request.context != record.context:
        raise ValueError("Cutover belongs to a different preparation.")
    if (
        not record.qualified_at
        or not record.recovery_proof
        or not record.authority_snapshot["inventory"]
    ):
        raise ValueError("Bootstrap requires recipient recovery and retained production inventory.")
    before = record.authority_snapshot
    retained_identity = RuntimeIdentity.model_validate(before["inventory"].get("runtime_identity"))
    if (retained_identity.product, retained_identity.context, retained_identity.instance) != (
        record.product,
        record.context,
        "prod",
    ):
        raise ValueError("The retained production writer requires exact runtime identity.")
    endpoints = [
        PrivateHealthEndpointRecord.model_validate(row) for row in before["health_endpoints"]
    ]
    if not endpoints or set(request.health_urls) != {row.endpoint_key for row in endpoints}:
        raise ValueError("Review every production private monitor endpoint.")
    # Validate private HTTP destinations at the record boundary, not in a caller assertion.
    for endpoint in endpoints:
        PrivateHealthEndpointRecord.model_validate(
            {**endpoint.model_dump(mode="json"), "url": request.health_urls[endpoint.endpoint_key]}
        )
    PrivateHealthEndpointRecord.model_validate(
        {**endpoints[0].model_dump(mode="json"), "url": request.readiness_url}
    )
    policies = [
        ProductionBackupPolicyRecord.model_validate(row)
        for row in before["backup_policies"]
        if row["status"] == "active"
    ]
    if not policies or set(request.backup_sources) != {row.policy_id for row in policies}:
        raise ValueError("Review every production backup source revision.")
    source_records = []
    for target_id in sorted(set(request.backup_sources.values())):
        rows = store.list_production_backup_target_records(target_id=target_id, status="active")
        if (
            len(rows) != 1
            or rows[0].destination_kind != "proxmox_guest"
            or (
                check_freshness
                and datetime.fromisoformat(rows[0].review_after.replace("Z", "+00:00"))
                <= datetime.now(UTC)
            )
        ):
            raise ValueError("An existing current destination backup source is required.")
        source_records.append(rows[0].model_dump(mode="json"))
    if len(source_records) != 1:
        raise ValueError(
            "This cutover requires one independently captured destination guest for every backup gate."
        )
    operation = store.read_verireel_prod_backup_gate_operation_record(
        request.destination_backup_operation_id
    )
    if operation.binding is None or operation.result is None:
        raise ValueError("A completed service-owned destination capture is required.")
    bound = operation.binding
    verified = _read_operation(
        store,
        bound.request.product,
        bound.request.context,
        bound.request.instance,
        bound.request.promotion_action,
        bound.request.backup_record_id,
    )
    if verified != operation or bound.source_target.model_dump(mode="json") not in source_records:
        raise ValueError("Destination capture belongs to another exact source revision.")
    capture = store.read_backup_gate_record(operation.backup_record_id)
    if (
        capture.source != "launchplane-production-backup-gate"
        or capture.status != "pass"
        or capture.evidence != operation.result.evidence
        or capture.evidence.get("capture_status") != "verified"
        or not capture.evidence.get("independent_backup_id")
    ):
        raise ValueError("Destination capture is incomplete or unverified.")
    now = datetime.now(UTC)
    for key, maximum in (
        ("snapshot_finished_at", bound.policy.fast_snapshot.max_evidence_age_seconds),
        (
            "independent_backup_finished_at",
            bound.policy.independent_backup.max_evidence_age_seconds,
        ),
    ):
        observed = datetime.fromisoformat(capture.evidence.get(key, "").replace("Z", "+00:00"))
        if check_freshness and not 0 <= (now - observed).total_seconds() <= maximum:
            raise ValueError("Destination capture is stale or has a future observation.")
    binding = next(
        (
            row
            for row in store.list_secret_bindings()
            if row.binding_id == request.writer_binding_id
        ),
        None,
    )
    if binding is None or (binding.context, binding.instance) != (record.context, "prod"):
        raise ValueError("Use the existing production API writer binding.")
    secret = store.read_secret_record(binding.secret_id)
    values = runtime_values(store, root, record)
    rendered = qualified_render(record, values)
    application = rendered["services"][record.request.application_service].get("environment", {})
    database = rendered["services"][record.request.database_service].get("environment", {})
    if (
        not isinstance(application, dict)
        or not isinstance(database, dict)
        or not values.get(binding.binding_key)
        or application.get(binding.binding_key) != values[binding.binding_key]
        or values[binding.binding_key] in database.values()
        or any(
            binding.binding_key in upstream.keys.values() for upstream in record.request.upstreams
        )
    ):
        raise ValueError(
            "The API writer binding must be delivered only to the application, independently of data credentials."
        )
    from control_plane.production_recipient_budget import writer_bindings, binding_identities

    consumers = binding_identities(
        writer_bindings(
            store, record.model_copy(update={"activation": {"writer_secret_id": binding.secret_id}})
        )
    )
    intent = request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"})
    return {
        "request": intent,
        "source_records": source_records,
        "destination_backup": operation.model_dump(mode="json"),
        "writer_secret_id": binding.secret_id,
        "writer_version_id": secret.current_version_id,
        "binding": binding_identities([binding.model_dump(mode="json")])[0],
        "writer_bindings": consumers,
        "plan_sha256": canonical_sha256(
            {
                "request": intent,
                "preparation": record.model_dump(
                    mode="json",
                    exclude={
                        "activation",
                        "activation_history",
                        "execution",
                        "phase",
                        "writer_custody",
                        "publication",
                    },
                ),
                "sources": source_records,
                "destination_backup": operation.model_dump(mode="json"),
                "binding": binding_identities([binding.model_dump(mode="json")])[0],
                "writer_bindings": consumers,
                "version": secret.current_version_id,
            }
        ),
    }


def save_cutover_intent(
    store: PostgresRecordStore, record: ProductionRecipientRecord, intent: dict[str, Any]
) -> None:
    if record.execution and record.activation != intent:
        raise ValueError("An executed cutover must retain its admitted intent.")
    store.write_production_recipient(
        expected=record.model_dump(mode="json"),
        replacement=record.model_copy(
            update={
                "activation": intent,
                "activation_history": record.activation_history
                + (
                    [record.activation] if record.activation and record.activation != intent else []
                ),
            }
        ).model_dump(mode="json"),
    )


def require_writer_exclusive(store: PostgresRecordStore, record: ProductionRecipientRecord) -> None:
    from control_plane.production_recipient_budget import writer_bindings, binding_identities

    if binding_identities(writer_bindings(store, record)) != binding_identities(
        record.activation["writer_bindings"]
    ):
        raise ValueError("Shared API writer consumers changed after review.")
    if (
        store.read_secret_record(record.activation["writer_secret_id"]).current_version_id
        != record.activation["writer_version_id"]
    ):
        raise ValueError("API writer authority changed.")


def require_peers_parked(
    root: Path,
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    checkpoint: Callable[[str], None],
) -> None:
    inspected = set()
    for binding in record.activation["writer_bindings"]:
        if binding["instance"] == "prod":
            continue
        peer = store.read_provider_target_record(
            context_name=binding["context"], instance_name=binding["instance"]
        )
        if peer.provider_id != "dokploy" or peer.target_category != "compose":
            raise ValueError("Peer API writer cannot be qualified through the Compose boundary.")
        host, token = source.read_dokploy_config(control_plane_root=root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=peer.target_id
        )
        projected = record.model_copy(
            update={"target_id": peer.target_id, "app_name": str(payload.get("appName") or "")}
        )
        writer_action(root, projected, old=False, action="parked", checkpoint=checkpoint)
        inspected.add(
            (
                peer.target_id,
                str(payload.get("serverId") or ""),
                str(payload.get("appName") or ""),
                record.request.application_service,
            )
        )
    for entry in record.writer_custody:
        retained = entry.get("host")
        if retained is None or "service" not in retained:
            raise ValueError("A retained peer has incomplete host custody.")
        key = (
            retained["target_id"],
            retained["server_id"],
            retained["app_name"],
            retained["service"],
        )
        if key in inspected:
            continue
        inspected.add(key)
        projected = record.model_copy(
            update={
                "target_id": retained["target_id"],
                "app_name": retained["app_name"],
                "request": record.request.model_copy(
                    update={"destination_server_id": retained["server_id"]}
                ),
            }
        )
        evidence = run_recipient_program(
            root,
            projected,
            {
                "project": retained["app_name"],
                "service": retained["service"],
                "binding": record.plan_sha256,
                "action": "parked",
                "inspect": True,
            },
            WRITER_PROGRAM,
            checkpoint,
            custodied_read=True,
        )
        if (
            evidence.get(WRITER_ACTION_MARKER) != "parked"
            or evidence.get(WRITER_BINDING_MARKER) != record.plan_sha256
        ):
            raise ValueError("A retained historical API writer is not verified parked.")


def writer_action(
    root: Path,
    record: ProductionRecipientRecord,
    *,
    old: bool,
    action: Literal[
        "pause",
        "resume",
        "inspect",
        "parked",
        "receipt_pause",
        "receipt_resume",
        "receipt_resume_retryable",
        "fence",
    ],
    checkpoint: Callable[[str], None],
    binding: str | None = None,
    server_id: str | None = None,
) -> None:
    host, token = source.read_dokploy_config(control_plane_root=root)
    target_id = record.retained_target_id.target_id if old else record.target_id
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id
    )
    if (
        server_id is not None
        and str(payload.get("serverId") or "") != server_id
        or payload.get("composeId") != target_id
        or (old and provider_binding_digest(payload) != record.retained_provider_sha256)
        or (not old and payload.get("appName") != record.app_name)
    ):
        raise ValueError("Writer recipient identity changed.")
    if any(
        api.deployment_status(row) in {"", "pending", "queued", "running"}
        for row in api.list_deployments_for_target(
            host=host, token=token, target_type="compose", target_id=target_id
        )
    ):
        raise ValueError("A pending provider deployment can restart the writer.")
    projected = record.model_copy(
        update={
            "target_id": target_id,
            "request": record.request.model_copy(
                update={"destination_server_id": str(payload.get("serverId") or "")}
            ),
        }
    )
    witness_binding = binding or record.execution.get("writer_binding", record.plan_sha256)
    evidence = run_recipient_program(
        root,
        projected,
        {
            "project": str(payload.get("appName") or ""),
            "service": record.request.application_service,
            "action": action,
            "binding": witness_binding,
            "old": old,
            "inspect": action.startswith("receipt_"),
            "expected_identity": (record.authority_snapshot.get("inventory") or {}).get(
                "runtime_identity"
            )
            if old
            else record.execution.get("runtime_identity"),
            "image": record.image_reference,
            "owned_replacement": not old and record.app_name.startswith(record.namespace),
        },
        WRITER_PROGRAM,
        checkpoint,
    )
    if (
        evidence.get(WRITER_ACTION_MARKER) != action
        or evidence.get(WRITER_BINDING_MARKER) != witness_binding
    ):
        raise ValueError("Writer coordination was not verified.")


def resume_receipt(
    root: Path,
    record: ProductionRecipientRecord,
    *,
    old: bool,
    checkpoint: Callable[[str], None],
    binding: str | None = None,
) -> None:
    try:
        writer_action(
            root, record, old=old, action="receipt_resume", checkpoint=checkpoint, binding=binding
        )
    except Exception:
        writer_action(
            root,
            record,
            old=old,
            action="receipt_resume_retryable",
            checkpoint=checkpoint,
            binding=binding,
        )


WRITER_LIBRARY = (
    "WRITER_ACTION_MARKER="
    + repr(WRITER_ACTION_MARKER)
    + "\nWRITER_BINDING_MARKER="
    + repr(WRITER_BINDING_MARKER)
    + "\n"
    + r"""
import base64,fcntl,hashlib,json,os,re,subprocess,sys,uuid
from pathlib import Path
WRITER_CUSTODY_ROOT=Path('/var/lib/launchplane/recipient-writers')
def coordinate_writer(spec, *, base=None):
    base=base or WRITER_CUSTODY_ROOT
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+',spec[key]) for key in ('project','service')) or not re.fullmatch(r'[a-f0-9]{64}',spec['binding']):
        raise ValueError('invalid writer identity')
    writer=hashlib.sha256(json.dumps([spec['project'],spec['service']]).encode()).hexdigest()
    directory=base/'active'/writer
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
        raise ValueError('invalid active writer custody')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(directory/'lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _coordinate_writer(spec,base=base,owner_path=directory/'binding.json')
    finally:
        os.close(descriptor)
def writer_fence(spec):
    return {'fenced':True,'binding':spec['binding'],'project':spec['project'],'service':spec['service']}
def fence_writer(spec, *, base):
    # Runs under the active writer lock. Without the original witness this
    # binding had no effect. The fence occupies the witness path itself: every
    # dispatched writer program refuses a witness that is not its container
    # before any effect, including jobs queued before this fence existed.
    writer=hashlib.sha256(json.dumps([spec['project'],spec['service']]).encode()).hexdigest()
    directory=base/spec['binding']/writer
    if directory.is_symlink() or any(item.is_symlink() for item in directory.parents):
        raise ValueError('invalid writer custody')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/'before.json'
    expected=writer_fence(spec)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or json.loads(path.read_text())!=expected:
            raise ValueError('the original writer operation was witnessed')
        return
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as output:
        json.dump(expected,output)
        output.flush()
        os.fsync(output.fileno())
    descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
def _coordinate_writer(spec, *, base, owner_path):
    requested=spec['action']
    receipt=requested.startswith('receipt_')
    action=requested.removeprefix('receipt_')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',spec['project']) or not re.fullmatch(r'[A-Za-z0-9_-]+',spec['service']) or requested not in ('pause','resume','inspect','parked','receipt_pause','receipt_resume','receipt_resume_retryable','fence') or not re.fullmatch(r'[a-f0-9]{64}',spec['binding']):
        raise ValueError('invalid writer identity')
    if requested=='fence':
        fence_writer(spec,base=base)
        print(WRITER_ACTION_MARKER+'='+requested)
        print(WRITER_BINDING_MARKER+'='+spec['binding'])
        return
    def docker(*args):
        result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=60)
        if result.returncode or len(result.stdout)>1024*1024:
            raise ValueError('writer operation failed')
        return result.stdout
    ids=docker('ps','-aq','--filter','label=com.docker.compose.project='+spec['project'],'--filter','label=com.docker.compose.service='+spec['service']).splitlines()
    if not ids and action=='parked':
        print(WRITER_ACTION_MARKER+'=parked')
        print(WRITER_BINDING_MARKER+'='+spec['binding'])
        sys.exit(0)
    if len(ids)!=1:
        raise ValueError('writer identity is absent or ambiguous')
    rows=json.loads(docker('inspect',ids[0]))
    if len(rows)!=1 or rows[0].get('Config',{}).get('Labels',{}).get('com.docker.compose.project')!=spec['project'] or rows[0].get('Config',{}).get('Labels',{}).get('com.docker.compose.service')!=spec['service']:
        raise ValueError('writer identity changed')
    row=rows[0]
    if action not in ('parked','inspect'):
        expected_identity=spec.get('expected_identity')
        environment=dict(item.split('=',1) for item in row.get('Config',{}).get('Env',[]) if '=' in item)
        observed=json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))
        if not isinstance(expected_identity,dict) or observed!=expected_identity:
            if not (spec.get('owned_replacement') and observed is None and row.get('Config',{}).get('Image')==spec['image']):
                raise ValueError('writer runtime identity changed')
    expected={'id':ids[0],'image':row['Image'],'project':spec['project'],'service':spec['service']}
    witness=None
    def persist():
        temporary=path.with_name('witness-'+uuid.uuid4().hex+'.json')
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as output:
            json.dump(witness,output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,path)
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if action not in ('inspect','parked'):
        writer=hashlib.sha256(json.dumps([spec['project'],spec['service']]).encode()).hexdigest()
        directory=base/spec['binding']/writer
        if directory.is_symlink() or any(item.is_symlink() for item in directory.parents):
            raise ValueError('invalid writer custody')
        if not receipt:
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=directory/'before.json'
        if path.exists() and not path.is_symlink() and json.loads(path.read_text()).get('fenced') is True:
            raise ValueError('this writer operation is permanently fenced')
        if path.exists():
            if path.is_symlink():
                raise ValueError('invalid writer witness')
            witness=json.loads(path.read_text())
            if any(witness.get(key)!=value for key,value in expected.items()) or type(witness.get('was_running')) is not bool:
                raise ValueError('retained writer changed')
        elif action=='pause' and not receipt:
            witness={**expected,'was_running':row.get('State',{}).get('Running') is True}
            persist()
        else:
            raise ValueError('the original writer state was not witnessed')
    if action=='resume_retryable':
        if not witness.get('released') or witness.get('resumed') or json.loads(owner_path.read_text()).get('binding')!=spec['binding']:
            raise ValueError('this resume cannot be retried')
        print(WRITER_ACTION_MARKER+'='+requested)
        print(WRITER_BINDING_MARKER+'='+spec['binding'])
        return
    if not receipt:
        if action=='pause':
            if witness.get('released'):
                raise ValueError('this writer pause was permanently released')
            temporary=owner_path.with_name('owner-'+uuid.uuid4().hex+'.json')
            with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as output:
                json.dump({'binding':spec['binding']},output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,owner_path)
            descriptor=os.open(owner_path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            docker('stop','--time','30',ids[0])
        elif action=='resume':
            if not witness.get('paused'):
                raise ValueError('the original stop did not complete')
            if json.loads(owner_path.read_text()).get('binding')!=spec['binding']:
                raise ValueError('a later pause owns this writer')
            if not witness.get('released'):
                witness['released']=True
                persist()
            if not witness.get('resumed') and witness['was_running'] and row.get('State',{}).get('Running') is not True:
                docker('start',ids[0])
    state=json.loads(docker('inspect',ids[0]))[0]
    wanted=action=='inspect' or action=='resume' and witness['was_running']
    if state.get('State',{}).get('Running') is not wanted or state.get('Image')!=expected['image']:
        raise ValueError('writer state is unverified')
    if action in ('pause','resume'):
        terminal='paused' if action=='pause' else 'resumed'
        if receipt:
            if not witness.get(terminal) or action=='pause' and witness.get('released'):
                raise ValueError('no terminal witness for the requested action')
        else:
            witness[terminal]=True
            persist()
    print(WRITER_ACTION_MARKER+'='+requested)
    print(WRITER_BINDING_MARKER+'='+spec['binding'])
"""
)

WRITER_PROGRAM = (
    WRITER_LIBRARY
    + "\ncoordinate_writer(json.loads(base64.b64decode(sys.argv[1],validate=True)))\n"
)


WRITER_RECEIPT_LIBRARY = (
    WRITER_LIBRARY
    + r"""
def read_writer_receipt(spec, *, base=None):
    base=base or WRITER_CUSTODY_ROOT
    if not re.fullmatch(r'[a-f0-9]{64}',spec['binding']) or any(not re.fullmatch(r'[A-Za-z0-9_-]+',spec[key]) for key in ('project','service')) or spec['action'] not in ('receipt_pause','receipt_resume','receipt_resume_retryable','receipt_fenced'):
        raise ValueError('invalid retained writer receipt')
    writer=hashlib.sha256(json.dumps([spec['project'],spec['service']]).encode()).hexdigest()
    directory=base/spec['binding']/writer
    path=directory/'before.json'
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('invalid retained writer custody')
    if spec['action']=='receipt_fenced':
        # The fence was created exclusively in place of the witness, so no
        # witness can exist beside it or follow it.
        if json.loads(path.read_text())!=writer_fence(spec):
            raise ValueError('no retained writer fence')
        print(WRITER_ACTION_MARKER+'='+spec['action'])
        print(WRITER_BINDING_MARKER+'='+spec['binding'])
        return
    witness=json.loads(path.read_text())
    if any(witness.get(key)!=spec[key] for key in ('project','service')) or type(witness.get('was_running')) is not bool or not witness.get('id') or not witness.get('image'):
        raise ValueError('retained writer witness changed')
    if spec['action']=='receipt_resume_retryable':
        owner=base/'active'/writer
        if any(parent.is_symlink() for parent in (owner,*owner.parents)):
            raise ValueError('invalid active writer custody')
        descriptor=os.open(owner/'lock',os.O_RDONLY|os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_SH|fcntl.LOCK_NB)
            if not witness.get('paused') or not witness.get('released') or witness.get('resumed') or json.loads((owner/'binding.json').read_text()).get('binding')!=spec['binding']:
                raise ValueError('this resume cannot be retried')
        finally:
            os.close(descriptor)
        print(WRITER_ACTION_MARKER+'='+spec['action'])
        print(WRITER_BINDING_MARKER+'='+spec['binding'])
        return
    terminal='paused' if spec['action']=='receipt_pause' else 'resumed'
    if witness.get(terminal) is not True or terminal=='resumed' and witness.get('released') is not True:
        raise ValueError('no terminal retained writer witness')
    print(WRITER_ACTION_MARKER+'='+spec['action'])
    print(WRITER_BINDING_MARKER+'='+spec['binding'])
"""
)
WRITER_RECEIPT_PROGRAM = (
    WRITER_RECEIPT_LIBRARY
    + "\nread_writer_receipt(json.loads(base64.b64decode(sys.argv[1],validate=True)))\n"
)


@dataclass
class RecipientCutoverAdapter:
    root: Path
    store: PostgresRecordStore
    record: ProductionRecipientRecord
    trace_id: str
    backup_record_id: str
    validate_release: Callable[[], None]
    rollback_observed: bool = False
    rollback_resume_pending: bool = False

    def target_key(self) -> str:
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.record.context,
            instance="prod",
            target_type="compose",
            target_id=self.record.retained_target_id.target_id,
        )

    def reconciliation_key(self) -> str:
        return cutover_reconciliation_key(self.record.plan_sha256)

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        current = custody(self.store, self.record.product, self.record.context)
        if (
            current
            and current.plan_sha256 == self.record.plan_sha256
            and current.phase == "published"
        ):
            return ProviderObservation(
                "present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id,
                    records={},
                    result={
                        "promotion_status": "pass",
                        "recipient_plan_sha256": current.plan_sha256,
                    },
                ),
            )
        if current and current.plan_sha256 == self.record.plan_sha256:
            self.record = current
            attempt = canonical_sha256(provider_operation_key)
            if current.execution.get(attempt + ":old_pause_fenced"):
                # The reviewed host fence proved this attempt never paused a writer.
                return ProviderObservation(
                    "present",
                    response_payload=self.result("fail", writer_effect="none").response_payload,
                )
            if current.phase == "rolled_back":
                if current.execution.get("native_inverse") or current.execution.get(
                    attempt + ":rollback_completed_at"
                ):
                    return ProviderObservation(
                        "present",
                        response_payload=self.result(
                            "fail", rollback_status="pass"
                        ).response_payload,
                    )
                return ProviderObservation("absent", retry_safe=True)
            if self.rollback_witnessed(current, provider_operation_key):
                self.rollback_observed = True
                return ProviderObservation("absent", retry_safe=True)
            if self.rollback_resume_retryable(current, provider_operation_key):
                self.rollback_observed = True
                self.rollback_resume_pending = True
                return ProviderObservation("absent", retry_safe=True)
            if current.execution:
                if current.execution.get(attempt + ":old_pause_dispatched_at"):
                    try:
                        writer_action(
                            self.root,
                            current,
                            old=True,
                            action="receipt_pause",
                            checkpoint=lambda _phase: None,
                            binding=canonical_sha256(
                                {
                                    "preparation": current.plan_sha256,
                                    "operation": provider_operation_key,
                                }
                            ),
                        )
                        if current.execution.get("writer_binding:" + attempt):
                            resume_receipt(
                                self.root,
                                current,
                                old=False,
                                checkpoint=lambda _phase: None,
                                binding=current.publication.get("inverse_writer_binding")
                                or current.publication.get("writer_binding")
                                or None,
                            )
                    except Exception:
                        return ProviderObservation("unknown")
                if (
                    not current.execution.get("trigger_requested_at")
                    or self.terminal_deployment(
                        current.execution["runtime_identity"]["deployment_record_id"]
                    )
                    is not None
                ):
                    return ProviderObservation("absent", retry_safe=True)
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        if self.record.phase in {"reverting", "inverse_published"}:
            raise ProviderMutationRejectedError(
                ValueError("Finish the original inverse before another forward operation.")
            )
        if self.rollback_observed:
            if self.rollback_resume_pending:
                if not isinstance(
                    lease, ProviderEvidenceLease
                ) or not self.rollback_resume_retryable(self.record, provider_operation_key):
                    raise ProviderMutationUnknownError("Original writer resume is not retryable.")
                try:
                    writer_action(
                        self.root,
                        self.record,
                        old=True,
                        action="resume",
                        checkpoint=lease.checkpoint_effect,
                        binding=canonical_sha256(
                            {
                                "preparation": self.record.plan_sha256,
                                "operation": provider_operation_key,
                            }
                        ),
                    )
                except Exception as error:
                    raise ProviderMutationUnknownError(
                        "Keep the interrupted original writer resume."
                    ) from error
            if not isinstance(lease, ProviderEvidenceLease) or not self.rollback_witnessed(
                self.record, provider_operation_key
            ):
                raise ProviderMutationUnknownError("Original rollback completion is not verified.")
            lease.assert_current()
            record = self.record.model_copy(
                update={
                    "phase": "rolled_back",
                    "execution": {
                        **self.record.execution,
                        canonical_sha256(provider_operation_key)
                        + ":rollback_completed_at": utc_now_timestamp(),
                    },
                }
            )
            self.store.write_production_recipient(
                expected=self.record.model_dump(mode="json"),
                replacement=record.model_dump(mode="json"),
                reservation=lease.evidence_reservation(),
            )
            self.record = record
            self.fail_pending_promotion(provider_operation_key, lease)
            return self.result(
                "fail",
                rollback_status="pass",
                provider_effect_performed=self.rollback_resume_pending,
            )
        if self.record.execution.get("native_inverse"):
            raise ProviderMutationRejectedError(
                ValueError(
                    "The operator inverted this publication; review a new generation before another release."
                )
            )
        if not self.record.execution:
            self.validate_release()
        if not isinstance(lease, ProviderEvidenceLease):
            raise ProviderMutationRejectedError(ValueError("An evidence-bound lease is required."))
        operation = _read_operation(
            self.store,
            self.record.product,
            self.record.context,
            "prod",
            GENERIC_WEB_PROMOTION_BACKUP_ACTION,
            self.backup_record_id,
        )
        with self.store.production_backup_source_lock(_source_key(operation)) as locked:
            if locked is None:
                raise ProviderMutationRejectedError(ValueError("Production backup source is busy."))

            def backup_check() -> BackupGateEvidence:
                locked()
                return require_production_promotion_backup(
                    record_store=self.store,
                    product=self.record.product,
                    context=self.record.context,
                    instance="prod",
                    promotion_action=GENERIC_WEB_PROMOTION_BACKUP_ACTION,
                    backup_record_id=self.backup_record_id,
                    active_promotion_record_id="promotion-recipient-"
                    + canonical_sha256(provider_operation_key)[:24],
                )

            with self.store.provider_evidence_guard(lease.evidence_reservation):
                return self._apply(provider_operation_key, lease, backup_check)

    def _apply(
        self,
        provider_operation_key: str,
        lease: ProviderOperationLease,
        backup_check: Callable[[], BackupGateEvidence],
    ) -> ProviderMutationOutcome:
        record = self.record
        repromote = bool(record.publication.get("inverse_backup_policies")) and record.phase in {
            "rolled_back",
            "preflight_refused",
            "bootstrapping",
        }
        baseline = (
            republication_snapshot(self.store, record) if repromote else record.authority_snapshot
        )
        writer_binding = canonical_sha256(
            {"preparation": record.plan_sha256, "operation": provider_operation_key}
        )
        attempt_prefix = canonical_sha256(provider_operation_key) + ":"
        started = False
        terminal_deployment = False
        old_paused = False
        preparation = ProductionRecipientAdapter(
            self.root,
            self.store,
            record.product,
            record.request,
            record.actor,
            record.scope,
            self.trace_id,
            record.original_key,
        )

        def save(**updates: Any) -> None:
            nonlocal record
            lease.assert_current()
            replacement = record.model_copy(update=updates)
            self.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=replacement.model_dump(mode="json"),
                reservation=lease.evidence_reservation()
                if isinstance(lease, ProviderEvidenceLease)
                else None,
            )
            record = replacement
            self.record = record

        def check_local() -> None:
            self.validate_release()
            require_writer_exclusive(self.store, record)
            backup_check()

        def checkpoint(phase: str) -> None:
            nonlocal started
            lease.assert_current()
            check_local()
            preparation.require_unchanged(
                record.model_copy(update={"authority_snapshot": baseline})
            )
            if phase == "deploy_trigger" and not record.execution.get("trigger_requested_at"):
                save(execution={**record.execution, "trigger_requested_at": utc_now_timestamp()})
            lease.checkpoint_effect(phase)
            started = True

        def recovery_checkpoint(phase: str) -> None:
            lease.assert_current()
            lease.checkpoint_effect("rollback_" + phase)

        pending: PromotionRecord | None = None
        try:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Cutover requires an evidence-bound execution lease.")
            check_local()
            preparation.require_unchanged(
                record.model_copy(update={"authority_snapshot": baseline})
            )
            if (
                cutover_intent(
                    self.root,
                    self.store,
                    record,
                    RecipientCutoverRequest.model_validate(record.activation["request"]),
                    check_freshness=not bool(record.execution.get("old_pause_dispatched_at")),
                )
                != record.activation
            ):
                raise ValueError("Reviewed cutover authority changed.")
            deployment_id = "recipient-release-" + canonical_sha256(provider_operation_key)[:24]
            promotion_id = "promotion-recipient-" + canonical_sha256(provider_operation_key)[:24]
            if record.execution:
                identity = RuntimeIdentity.model_validate(record.execution["runtime_identity"])
                if (
                    identity.image_reference != record.image_reference
                    or (not repromote and identity.deployment_record_id != deployment_id)
                    or (repromote and record.execution.get("release_trace") != self.trace_id)
                ):
                    raise ValueError("The saved release execution belongs to another operation.")
                deployment_id = identity.deployment_record_id
                if not repromote and record.phase != "bootstrapping":
                    save(phase="bootstrapping")
                if repromote:
                    save(
                        phase="bootstrapping",
                        execution={
                            **record.execution,
                            "writer_binding:"
                            + canonical_sha256(provider_operation_key): writer_binding,
                        },
                    )
            else:
                identity = RuntimeIdentity(
                    product=record.product,
                    context=record.context,
                    instance="prod",
                    environment_kind="stable",
                    deployment_record_id=deployment_id,
                    artifact_id=record.image_reference,
                    source_git_ref=record.request.source_commit,
                    image_reference=record.image_reference,
                    deployed_at=utc_now_timestamp(),
                )
                save(
                    phase="bootstrapping",
                    execution={
                        "runtime_identity": identity.model_dump(mode="json"),
                        "promotion_record_id": promotion_id,
                        "writer_binding": writer_binding,
                        "release_trace": self.trace_id,
                        "original_reservation": lease.evidence_reservation().model_dump(
                            mode="json"
                        ),
                    },
                )
            reservation = lease.evidence_reservation()
            reservation_key = "reservation:" + reservation.record_id
            if reservation_key not in record.execution:
                save(
                    execution={
                        **record.execution,
                        reservation_key: reservation.model_dump(mode="json"),
                    }
                )
            pending = PromotionRecord(
                record_id=promotion_id,
                artifact_identity=ArtifactIdentityReference(artifact_id=record.image_reference),
                context=record.context,
                from_instance="testing",
                to_instance="prod",
                backup_record_id=self.backup_record_id,
                backup_gate=backup_check(),
                deploy=DeploymentEvidence(
                    status="pending",
                    target_name=record.namespace,
                    target_type="compose",
                    deploy_mode="compose",
                    started_at=identity.deployed_at,
                ),
            )
            self.store.write_promotion_record(pending)
            preparation.verify(record.model_copy(update={"authority_snapshot": baseline}))
            self.verify_upstreams(record, checkpoint, lease)
            require_peers_parked(self.root, self.store, record, checkpoint)
            host, token = source.read_dokploy_config(control_plane_root=self.root)

            if not record.execution.get("writer_hosts"):
                retained = api.fetch_dokploy_target_payload(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=record.retained_target_id.target_id,
                )
                if provider_binding_digest(
                    retained
                ) != record.retained_provider_sha256 or not retained.get("appName"):
                    raise ValueError("The retained writer host changed before dispatch.")
                save(
                    execution={
                        **record.execution,
                        "writer_hosts": [
                            {
                                "target_id": record.retained_target_id.target_id,
                                "server_id": str(retained.get("serverId") or ""),
                                "app_name": str(retained.get("appName") or ""),
                            }
                        ],
                    }
                )

            def pause_old_checkpoint(phase: str) -> None:
                from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER

                checkpoint(phase)
                if phase == ODOO_POST_DEPLOY_SCHEDULE_TRIGGER and not record.execution.get(
                    attempt_prefix + "old_pause_dispatched_at"
                ):
                    save(
                        execution={
                            **record.execution,
                            attempt_prefix + "old_pause_dispatched_at": utc_now_timestamp(),
                            "old_pause_dispatched_at": record.execution.get(
                                "old_pause_dispatched_at"
                            )
                            or utc_now_timestamp(),
                        }
                    )

            if repromote:
                writer_action(
                    self.root,
                    record,
                    old=True,
                    action="pause",
                    checkpoint=pause_old_checkpoint,
                    binding=writer_binding,
                )
                old_paused = True
                writer_action(
                    self.root,
                    record,
                    old=False,
                    action="resume",
                    checkpoint=checkpoint,
                    binding=record.publication.get("inverse_writer_binding")
                    or record.publication.get("writer_binding")
                    or None,
                )
                terminal_deployment = True
            elif record.execution.get("trigger_requested_at"):
                outcome = self.terminal_deployment(deployment_id)
                if outcome is None:
                    raise ValueError("The original deployment is absent, pending or ambiguous.")
                terminal_deployment = True
                old_paused = True
                if api.deployment_status(outcome) not in {
                    "success",
                    "succeeded",
                    "done",
                    "completed",
                    "healthy",
                    "finished",
                }:
                    raise ValueError("The original recipient deployment failed.")
            else:
                inspect_resources(self.root, record, prepare=False, checkpoint=checkpoint)
                writer_action(
                    self.root,
                    record,
                    old=True,
                    action="pause",
                    checkpoint=pause_old_checkpoint,
                    binding=writer_binding,
                )
                old_paused = True
                execute_dokploy_artifact_deploy(
                    host=host,
                    token=token,
                    ship_request=ShipRequest(
                        artifact_id=record.image_reference,
                        context=record.context,
                        instance="prod",
                        source_git_ref=record.request.source_commit,
                        target_name=record.namespace,
                        target_type="compose",
                        provider_id="dokploy",
                        target_category="compose",
                        provider_target_type="compose",
                        deploy_mode="compose",
                        wait=True,
                        timeout_seconds=record.retained_target.deploy_timeout_seconds or 1800,
                    ),
                    resolved_target=ResolvedTargetEvidence(
                        target_type="compose",
                        target_id=record.target_id,
                        target_name=record.namespace,
                    ),
                    deploy_timeout_seconds=record.retained_target.deploy_timeout_seconds or 1800,
                    runtime_identity=identity,
                    deployment_title=deployment_id,
                    before_provider_mutation=checkpoint,
                    effect_started=lambda: None,
                )
                terminal_deployment = True
            health = _verify_health_evidence_with_identity(
                HealthcheckEvidence(
                    status="pending",
                    urls=tuple(
                        {
                            *record.activation["request"]["health_urls"].values(),
                            record.activation["request"]["readiness_url"],
                        }
                    ),
                    timeout_seconds=record.retained_target.healthcheck_timeout_seconds or 120,
                ),
                expected_runtime_identity=identity,
            )
            if health.status != "pass" or health.runtime_identity_status != "match":
                raise ValueError(
                    "The replacement did not complete bootstrap with exact runtime identity."
                )
            writer_action(self.root, record, old=False, action="inspect", checkpoint=checkpoint)
            self.verify_runtime(record, checkpoint)
            checkpoint("publish_recipient_authority")
            publication = self.publication(
                record, identity, health, deployment_id, authority=baseline
            )
            publication = publication.model_copy(
                update={
                    "inventory": publication.inventory.model_copy(
                        update={
                            "promotion_record_id": promotion_id,
                            "promoted_from_instance": "testing",
                            "artifact_identity": ArtifactIdentityReference(
                                artifact_id=record.image_reference
                            ),
                        }
                    )
                }
            )
            final = pending.model_copy(
                update={
                    "deployment_record_id": deployment_id,
                    "deploy": publication.inventory.deploy,
                    "destination_health": health,
                }
            )
            deployment = DeploymentRecord(
                record_id=deployment_id,
                artifact_identity=final.artifact_identity,
                context=record.context,
                instance="prod",
                source_git_ref=record.request.source_commit,
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose", target_id=record.target_id, target_name=record.namespace
                ),
                runtime_identity=identity,
                deploy=final.deploy,
                destination_health=health,
            )
            commit_publication(
                self.store,
                record,
                publication,
                validate_release=check_local,
                reservation=lease.evidence_reservation(),
                deployment=deployment,
                promotion=final,
                authority=baseline if repromote else None,
                writer_binding=writer_binding,
            )
            return self.result("pass")
        except Exception as error:
            if not started and not record.execution:
                raise ProviderMutationRejectedError(
                    ValueError("Recipient release gates refused bootstrap.")
                ) from error
            terminal_deployment = (
                terminal_deployment
                or isinstance(error, api.DokployDeploymentFailed)
                and error.deployment is not None
            )
            try:
                committed = custody(self.store, record.product, record.context)
                if (
                    committed is not None
                    and committed.phase == "published"
                    and committed.plan_sha256 == record.plan_sha256
                ):
                    return self.result("pass")
                if not old_paused and not record.execution.get(
                    attempt_prefix + "old_pause_dispatched_at"
                ):
                    save(phase="preflight_refused")
                    raise ProviderMutationRejectedError(
                        ValueError("Cutover refused before any writer dispatch.")
                    ) from error
                if record.execution.get("trigger_requested_at") and not terminal_deployment:
                    terminal_deployment = (
                        self.terminal_deployment(
                            record.execution["runtime_identity"]["deployment_record_id"]
                        )
                        is not None
                    )
                old_paused = old_paused or bool(
                    record.execution.get(attempt_prefix + "old_pause_dispatched_at")
                )
                if old_paused and (
                    terminal_deployment or not record.execution.get("trigger_requested_at")
                ):
                    if record.execution.get("trigger_requested_at"):
                        writer_action(
                            self.root,
                            record,
                            old=False,
                            action="pause",
                            checkpoint=recovery_checkpoint,
                            binding=writer_binding,
                        )
                    current = custody(self.store, record.product, record.context)
                    if current is not None and current.phase == "published":
                        record = rollback_publication(
                            self.store,
                            current,
                            stop_replacement=lambda: None,
                            reservation=lease.evidence_reservation()
                            if isinstance(lease, ProviderEvidenceLease)
                            else None,
                        )
                    if (
                        self.store.read_provider_target_record(
                            context_name=record.context, instance_name="prod"
                        )
                        != record.retained_provider
                    ):
                        raise ValueError("The original production binding was not restored.")
                    writer_action(
                        self.root,
                        record,
                        old=True,
                        action="resume",
                        checkpoint=recovery_checkpoint,
                        binding=writer_binding,
                    )
                    save(
                        phase="rolled_back",
                        execution={
                            **record.execution,
                            attempt_prefix + "rollback_completed_at": utc_now_timestamp(),
                        },
                    )
                    if pending is not None:
                        self.store.write_promotion_record(
                            pending.model_copy(
                                update={
                                    "deploy": pending.deploy.model_copy(update={"status": "fail"})
                                }
                            )
                        )
                    return self.result("fail", rollback_status="pass")
            except ProviderMutationRejectedError:
                raise
            except Exception:
                pass
            raise ProviderMutationUnknownError(
                "Retain exact cutover custody; reconcile before restarting either writer."
            ) from error

    def rollback_resume_retryable(self, record: ProductionRecipientRecord, key: str) -> bool:
        attempt = canonical_sha256(key)
        if not record.execution.get(attempt + ":old_pause_dispatched_at"):
            return False
        binding = canonical_sha256({"preparation": record.plan_sha256, "operation": key})
        try:
            if (
                self.store.read_provider_target_record(
                    context_name=record.context, instance_name="prod"
                )
                != record.retained_provider
            ):
                return False
            if record.execution.get("trigger_requested_at"):
                writer_action(
                    self.root,
                    record,
                    old=False,
                    action="receipt_pause",
                    checkpoint=lambda _phase: None,
                    binding=binding,
                )
            writer_action(
                self.root,
                record,
                old=True,
                action="receipt_resume_retryable",
                checkpoint=lambda _phase: None,
                binding=binding,
            )
            return True
        except Exception:
            return False

    def rollback_witnessed(
        self, record: ProductionRecipientRecord, provider_operation_key: str
    ) -> bool:
        attempt = canonical_sha256(provider_operation_key)
        if not record.execution.get(attempt + ":old_pause_dispatched_at"):
            return False
        binding = canonical_sha256(
            {"preparation": record.plan_sha256, "operation": provider_operation_key}
        )
        try:
            if (
                self.store.read_provider_target_record(
                    context_name=record.context, instance_name="prod"
                )
                != record.retained_provider
            ):
                return False
            writer_action(
                self.root,
                record,
                old=True,
                action="receipt_resume",
                checkpoint=lambda _phase: None,
                binding=binding,
            )
            if record.execution.get("trigger_requested_at"):
                writer_action(
                    self.root,
                    record,
                    old=False,
                    action="receipt_pause",
                    checkpoint=lambda _phase: None,
                    binding=binding,
                )
            return True
        except Exception:
            return False

    def fail_pending_promotion(self, key: str, lease: ProviderOperationLease) -> None:
        if not isinstance(lease, ProviderEvidenceLease):
            raise ValueError("Promotion failure evidence requires a live lease.")
        try:
            pending = self.store.read_promotion_record(
                "promotion-recipient-" + canonical_sha256(key)[:24]
            )
        except FileNotFoundError:
            return
        with self.store.provider_evidence_guard(lease.evidence_reservation):
            self.store.write_promotion_record(
                pending.model_copy(
                    update={"deploy": pending.deploy.model_copy(update={"status": "fail"})}
                )
            )

    def result(
        self,
        status: Literal["pass", "fail"],
        *,
        provider_effect_performed: bool = True,
        **extra: str,
    ) -> ProviderMutationOutcome:
        return ProviderMutationOutcome(
            202,
            provider_operation_response_payload(
                trace_id=self.trace_id,
                records={},
                result={
                    "promotion_status": status,
                    "recipient_plan_sha256": self.record.plan_sha256,
                    **extra,
                },
            ),
            provider_effect_performed=provider_effect_performed,
        )

    def terminal_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        rows = [
            row
            for row in api.list_deployments_for_target(
                host=host,
                token=token,
                target_type="compose",
                target_id=self.record.target_id,
                require_complete=True,
            )
            if row.get("title") == deployment_id
        ]
        if len(rows) != 1 or api.deployment_status(rows[0]) not in {
            "success",
            "succeeded",
            "done",
            "completed",
            "healthy",
            "finished",
            "failed",
            "error",
            "canceled",
            "cancelled",
            "killed",
            "unhealthy",
            "timeout",
        }:
            return None
        return rows[0]

    def verify_upstreams(
        self,
        record: ProductionRecipientRecord,
        checkpoint: Callable[[str], None],
        lease: ProviderOperationLease,
    ) -> None:
        if not record.request.upstreams:
            return
        from control_plane.production_recipient_host import upstream_program
        from control_plane.production_recipient_upstreams import (
            upstream_connections,
            upstream_binding,
        )

        values = runtime_values(self.store, self.root, record)
        rendered = qualified_render(record, values)
        binding = upstream_binding(record)
        evidence = run_recipient_program(
            self.root,
            record,
            {
                "connections": upstream_connections(record, rendered, values),
                "image": rendered["services"][record.request.database_service]["image"],
                "binding": binding,
                "project": record.app_name,
                "allow_services": [
                    record.request.application_service,
                    record.request.database_service,
                ],
            },
            upstream_program(),
            checkpoint,
            scrub_guard=lambda: lease.checkpoint_effect("scrub_recipient_upstream_probe"),
        )
        if (
            evidence.get("recipient_upstreams_ok") != "true"
            or evidence.get("recipient_upstreams_binding") != binding
        ):
            raise ValueError("Actual upstream privileges are not read-only at bootstrap.")

    def verify_runtime(
        self, record: ProductionRecipientRecord, checkpoint: Callable[[str], None]
    ) -> None:
        rendered = qualified_render(record, runtime_values(self.store, self.root, record))
        import shlex

        services = {}
        for name, limits in record.request.service_limits.items():
            definition = rendered["services"][name]
            command = definition.get("command", [])
            mounts = {}
            for mount in definition.get("volumes", []):
                if isinstance(mount, str):
                    alias, destination, *_ = mount.split(":")
                else:
                    alias, destination = mount["source"], mount["target"]
                mounts[destination] = rendered["volumes"][alias]["name"]
            ports = {}
            for port in definition.get("ports", []):
                if isinstance(port, str):
                    parts = port.split(":")
                    published, target = parts[-2:]
                    target = target if "/" in target else target + "/tcp"
                else:
                    published, target = (
                        str(port["published"]),
                        str(port["target"]) + "/" + port.get("protocol", "tcp"),
                    )
                ports[target] = str(published)
            services[name] = {
                "cpus": limits.cpus,
                "memory": limits.memory_bytes,
                "image": definition["image"],
                "volumes": mounts,
                "ports": ports,
                "command": shlex.split(command) if isinstance(command, str) else command,
                "check_command": name == record.request.database_service,
            }
        evidence = run_recipient_program(
            self.root,
            record,
            {
                "project": record.app_name,
                "services": services,
                "image": record.image_reference,
                "application": record.request.application_service,
            },
            RUNTIME_PROGRAM,
            checkpoint,
        )
        if evidence.get(RUNTIME_OK_MARKER) != "true":
            raise ValueError("Actual recipient images and resource enforcement are unverified.")

    def publication(
        self,
        record: ProductionRecipientRecord,
        identity: RuntimeIdentity,
        health: HealthcheckEvidence,
        deployment_id: str,
        *,
        authority: dict[str, Any] | None = None,
    ) -> RecipientPublication:
        authority = authority if authority is not None else record.authority_snapshot
        stamp = utc_now_timestamp()
        target = record.retained_target.model_copy(
            update={
                "target_name": record.namespace,
                "project_name": record.namespace,
                "source_type": "raw",
                "updated_at": stamp,
                "env": {
                    **record.retained_target.env,
                    **{
                        key: record.volumes[role]
                        for role, key in record.request.volume_keys.items()
                    },
                    **{key: str(value) for key, value in record.request.ports.items()},
                },
            }
        )
        target_id = record.retained_target_id.model_copy(
            update={"target_id": record.target_id, "updated_at": stamp}
        )
        policies = []
        for row in authority["backup_policies"]:
            if row["status"] != "active":
                continue
            source_id = record.activation["request"]["backup_sources"][row["policy_id"]]
            policies.append(
                ProductionBackupPolicyRecord.model_validate(
                    {
                        **row,
                        "record_id": "",
                        "policy_digest": "",
                        "policy_revision": row["policy_revision"] + 1,
                        "supersedes_record_id": row["record_id"],
                        "fast_snapshot": {**row["fast_snapshot"], "source_target_id": source_id},
                        "independent_backup": {
                            **row["independent_backup"],
                            "source_target_id": source_id,
                        },
                    }
                )
            )
        return RecipientPublication(
            target=target,
            target_id=target_id,
            provider=ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            ),
            inventory=EnvironmentInventory(
                context=record.context,
                instance="prod",
                source_git_ref=record.request.source_commit,
                runtime_identity=identity,
                deploy=DeploymentEvidence(
                    status="pass",
                    target_name=record.namespace,
                    target_type="compose",
                    deploy_mode="compose",
                    deployment_id=deployment_id,
                ),
                destination_health=health,
                updated_at=stamp,
                deployment_record_id=deployment_id,
            ),
            health_endpoints=tuple(
                PrivateHealthEndpointRecord.model_validate(
                    {
                        **row,
                        "url": record.activation["request"]["health_urls"][row["endpoint_key"]],
                        "updated_at": stamp,
                    }
                )
                for row in authority["health_endpoints"]
            ),
            backup_policies=tuple(policies),
        )


RUNTIME_PROGRAM = (
    "RUNTIME_OK_MARKER="
    + repr(RUNTIME_OK_MARKER)
    + "\n"
    + r"""
import base64,json,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
def docker(*args):
    result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=60)
    if result.returncode or len(result.stdout)>1024*1024:
        raise ValueError('runtime inspection unavailable')
    return result.stdout
for service,limits in spec['services'].items():
    ids=docker('ps','-aq','--filter','label=com.docker.compose.project='+spec['project'],'--filter','label=com.docker.compose.service='+service).splitlines()
    if len(ids)!=1:
        raise ValueError('runtime service identity is ambiguous')
    rows=json.loads(docker('inspect',ids[0]))
    if len(rows)!=1 or not rows[0].get('State',{}).get('Running'):
        raise ValueError('runtime service is not running')
    row=rows[0]
    host=row.get('HostConfig',{})
    cpus=host.get('NanoCpus',0) or (host.get('CpuQuota',0)/host.get('CpuPeriod',1))*1000000000
    if not 0<cpus<=limits['cpus']*1000000000 or not 0<host.get('Memory',0)<=limits['memory']:
        raise ValueError('reviewed runtime limits are not enforced')
    image=json.loads(docker('image','inspect',row['Image']))
    expected=limits['image']
    digests=[value.rsplit('@',1)[-1] for value in image[0].get('RepoDigests',[])] if len(image)==1 else []
    if row.get('Config',{}).get('Image')!=expected or expected.rsplit('@',1)[-1] not in digests:
        raise ValueError('runtime artifact is not exact')
    if limits['check_command'] and row.get('Config',{}).get('Cmd')!=limits['command']:
        raise ValueError('runtime database command differs from reviewed bounds')
    mounts={mount['Destination']:mount.get('Name','') for mount in row.get('Mounts',[]) if mount.get('Type')=='volume'}
    if mounts!=limits['volumes'] or any(mount.get('Type')!='volume' for mount in row.get('Mounts',[])):
        raise ValueError('runtime storage is not the independent recipient')
    ports={key:bindings[0].get('HostPort') for key,bindings in host.get('PortBindings',{}).items() if len(bindings)==1}
    if ports!=limits['ports'] or len(ports)!=len(host.get('PortBindings',{})):
        raise ValueError('runtime listener bindings differ from review')
print(RUNTIME_OK_MARKER+'=true')
"""
)
