import json
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.odoo_instance_override_record import (
    ODOO_CONFIG_PARAMETER_KEY_PATTERN,
    OdooInstanceOverrideRecord,
)
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.integration_readback import CONFIG_PARAMETER_READBACK_LIBRARY
from control_plane.odoo_product_driver_http import resolve_odoo_product_route
from control_plane.odoo_secret_references import (
    read_secret_reference_authority,
    selected_config_parameter_bindings,
)
from control_plane.odoo_source_prerequisite_programs import HOST_INSPECT_LIBRARY, FailureStage
from control_plane.runtime_environments import SiteSecretDeliveryStore

ROUTE = "/v1/products/{product}/environments/{environment}/odoo-parameter-presence"


class ParameterPresenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameter: str = Field(pattern="^" + ODOO_CONFIG_PARAMETER_KEY_PATTERN + "$")
    context: str = Field(min_length=1, max_length=128)
    deployment_record_id: str = Field(min_length=1, max_length=256)
    source_git_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    artifact_id: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ParameterPresence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameter: str
    exists: bool = Field(strict=True)
    nonempty: bool = Field(strict=True)
    count: Annotated[int, Field(strict=True, ge=0, le=1)]

    @model_validator(mode="after")
    def consistent_presence(self) -> "ParameterPresence":
        if self.exists != bool(self.count) or (self.nonempty and not self.exists):
            raise ValueError("Inconsistent presence evidence")
        return self


class ParameterPresenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: Literal["verified", "unknown"] = "unknown"
    evidence: reads.OdooRuntimeEvidence | None = None
    presence: ParameterPresence | None = None
    failure_stage: FailureStage | None = None
    applied: Literal[False] = False
    values_redacted: Literal[True] = True


class PresenceStore(reads.OdooRuntimeReadStore, SiteSecretDeliveryStore, Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...


CONTAINER_PROGRAM = (
    CONFIG_PARAMETER_READBACK_LIBRARY
    + r"""
import configparser,json,os,sys
import psycopg2
spec=json.loads(sys.stdin.read(4097))
result={'runtime':'unknown'}
stage='runtime_inspection'
try:
    if os.environ.get('PLATFORM_INSTANCE')!='testing' or json.loads(os.environ[__IDENTITY_KEY__])!=spec['identity']:
        raise ValueError()
    stage='configuration'
    config=configparser.ConfigParser(interpolation=None)
    path=__CONFIG_PATH__
    with open(path,'rb') as stream:
        before=stream.read(262145)
    if len(before)>262144:
        raise ValueError()
    config.read_string(before.decode())
    options=config['options']
    database=os.environ.get('ODOO_DB_NAME')
    if not database or options.get('db_name')!=database or options.get('db_host')!='database':
        raise ValueError()
    stage='applied_parameters'
    connection=psycopg2.connect(host='database',port=options.get('db_port') or '5432',
        user=options['db_user'],password=options['db_password'],dbname=database,connect_timeout=3,
        options='-c default_transaction_read_only=on -c statement_timeout=3000')
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            key=spec['parameter']
            cursor.execute('SELECT count(*) FROM ir_config_parameter WHERE key = %s',(key,))
            count=cursor.fetchone()[0]
            nonempty=bool(present_config_parameters(cursor,(key,)))
        result={'runtime':'verified','presence':{'parameter':key,'exists':bool(count),'nonempty':nonempty,'count':count}}
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
    stage='drift'
    with open(path,'rb') as stream:
        if stream.read(262145)!=before:
            raise ValueError()
except BaseException:
    result={'runtime':'unknown','failure_stage':stage}
print(json.dumps(result))
"""
)

HOST_PROGRAM = (
    HOST_INSPECT_LIBRARY
    + r"""
result={'runtime':'unknown'}
stage='host_inspection'
try:
    before=inspect()
    stage='host_execution'
    child=subprocess.run(['docker','exec','-i',before['Id'],'timeout','--kill-after=2s','15s','python3','-I','-c',spec['container_program']],
        input=json.dumps({'identity':spec['identity'],'parameter':spec['parameter']}),text=True,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,check=False)
    stage='drift'
    after=inspect()
    stage='drift' if before!=after else 'host_execution'
    if before!=after or child.returncode!=0 or len(child.stdout)>1024:
        raise ValueError()
    stage='result_validation'
    observed=json.loads(child.stdout)
    if set(observed)=={'runtime','failure_stage'} and observed['runtime']=='unknown' and observed['failure_stage'] in ('runtime_inspection','configuration','applied_parameters','drift'):
        stage=observed['failure_stage']
        raise ValueError()
    presence=observed['presence']
    if (set(observed)!={'runtime','presence'} or observed['runtime']!='verified'
        or set(presence)!={'parameter','exists','nonempty','count'}
        or presence['parameter']!=spec['parameter'] or type(presence['count']) is not int
        or presence['count'] not in (0,1) or type(presence['exists']) is not bool
        or type(presence['nonempty']) is not bool or presence['exists']!=bool(presence['count'])
        or (presence['nonempty'] and not presence['exists'])):
        raise ValueError()
    result=observed
except Exception:
    result={'runtime':'unknown','failure_stage':stage}
print(prefix+json.dumps({'type':'complete','result':result}),flush=True)
"""
)


def parameter_authority(store: PresenceStore, product: str, query: ParameterPresenceQuery) -> str:
    profile = resolve_odoo_product_route(record_store=store, product=product, instance="testing")
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    if lane.context != query.context:
        raise ValueError("Selected context unavailable")
    record = store.read_odoo_instance_override_record(
        context_name=lane.context, instance_name=lane.instance
    )
    if (record.context, record.instance) != (lane.context, lane.instance):
        raise ValueError("Selected reference unavailable")
    authority = read_secret_reference_authority(store, record)
    selected = selected_config_parameter_bindings(record, authority, (query.parameter,))
    parameter = next(item for item in record.config_parameters if item.key == query.parameter)
    if parameter.value.secret_binding_id != selected[query.parameter].binding_id:
        raise ValueError("Selected reference unavailable")
    return json.dumps(
        [
            profile.model_dump(mode="json"),
            record.model_dump(mode="json"),
            authority.metadata.model_dump(mode="json"),
            authority.runtime.model_dump(mode="json"),
            store.read_dokploy_target_record(
                context_name=lane.context, instance_name=lane.instance
            ).model_dump(mode="json"),
            store.read_dokploy_target_id_record(
                context_name=lane.context, instance_name=lane.instance
            ).model_dump(mode="json"),
        ],
        sort_keys=True,
    )


def read_parameter_presence(
    *,
    store: PresenceStore,
    product: str,
    query: ParameterPresenceQuery,
    control_plane_root: Path,
    database_url: str | None,
) -> ParameterPresenceResult:
    stage: FailureStage = "authority"
    # noinspection PyBroadException
    try:
        authority = parameter_authority(store, product, query)
        stage = "runtime_selection"
        profile = store.read_product_profile_record(product)
        selection = reads.select_stable_runtime(store, profile, "testing")
        identity = selection.identity
        if (
            identity.product != product
            or identity.context != query.context
            or identity.deployment_record_id != query.deployment_record_id
            or identity.source_git_ref != query.source_git_ref
            or identity.artifact_id != query.artifact_id
            or identity.image_reference.rpartition("@")[2] != query.image_digest
        ):
            return ParameterPresenceResult(failure_stage=stage)
        stage = "runtime_connection"
        connection = reads.connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        stage = "runtime_inspection"
        observed = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            selection.identity,
        )
        stage = "transport"
        frames: list[dict[str, object]] = []
        _read_remote(
            host=connection.host,
            token=connection.token,
            server_id=connection.server_id,
            program=HOST_PROGRAM.replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)),
            payload={
                "project": connection.app_name,
                "container": connection.evidence.container_id,
                "identity": json.loads(observed[RUNTIME_IDENTITY_ENV_KEY]),
                "parameter": query.parameter,
                "container_program": CONTAINER_PROGRAM.replace(
                    "__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)
                ).replace("__CONFIG_PATH__", repr(ODOO_WEB_CONFIG_PATH)),
            },
            receive=frames.append,
            timeout_seconds=40,
        )
        stage = "result_validation"
        if (
            len(frames) != 1
            or set(frames[0]) != {"type", "result"}
            or frames[0]["type"] != "complete"
        ):
            return ParameterPresenceResult(failure_stage=stage)
        result = ParameterPresenceResult.model_validate(frames[0]["result"])
        if result.runtime == "unknown" and result.evidence is None and result.presence is None:
            return result
        if (
            result.runtime != "verified"
            or result.evidence is not None
            or result.presence is None
            or result.presence.parameter != query.parameter
        ):
            return ParameterPresenceResult(failure_stage=stage)
        stage = "drift"
        reads.assert_selection_current(store, selection)
        if parameter_authority(store, product, query) != authority:
            return ParameterPresenceResult(failure_stage=stage)
        return result.model_copy(update={"evidence": connection.evidence})
    except Exception:
        return ParameterPresenceResult(failure_stage=stage)
