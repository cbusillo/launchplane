"""Host-side source capture and pre-registry sanitization for a fresh testing copy."""

import base64
import json
from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from typing import Literal, TypeVar, TypedDict
from control_plane._odoo_website_settings_program import WEBSITE_SETTINGS_PROGRAM
from control_plane.contracts.odoo_testing_source import NATIVE_TESTING_COPY_CHUNK_BYTES
from control_plane.contracts.odoo_testing_source import NATIVE_TESTING_COPY_ARTIFACT_BYTES
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductRuntimeConfigRequirement,
    ProductSecretConfigRequirement,
    product_config_requirement_applies_to_lane,
)
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.odoo_connection_isolation import connection_isolation_library
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooTargetReplacementPlanBlockerCode,
)


Requirement = TypeVar(
    "Requirement", ProductRuntimeConfigRequirement, ProductSecretConfigRequirement
)


class FreshOdooTestingGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")
    domain_host: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bootstrap_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    certificate_type: Literal["none", "letsencrypt"] = "none"
    omitted_runtime_keys: tuple[str, ...] = ()
    prepared_at: str
    completed_at: str = ""
    # No backfill: legacy generations lack a complete lifetime witness.
    non_use_tracking_started_at: str = Field(default="", exclude_if=lambda value: not value)
    non_use_invalidated: bool = Field(default=False, exclude_if=lambda value: not value)
    reconcile_requested_at: str = ""
    source_mode: Literal["ssh", "container-export"] = "ssh"
    source_binding_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")
    drill_storage_budget: OdooTestingDrillStorageBudget | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    drill_database: str = Field(
        default="", pattern=r"^(?:lp_drill_[a-f0-9]{32})?$", exclude_if=lambda value: not value
    )
    discard_drill_database: str = Field(
        default="", pattern=r"^(?:lp_drill_[a-f0-9]{32})?$", exclude_if=lambda value: not value
    )
    discard_copy_binding: str = Field(
        default="", pattern=r"^(?:[a-f0-9]{64})?$", exclude_if=lambda value: not value
    )
    retained_drill_database: str = Field(
        default="", pattern=r"^(?:lp_drill_[a-f0-9]{32})?$", exclude_if=lambda value: not value
    )
    retained_copy_binding: str = Field(
        default="", pattern=r"^(?:[a-f0-9]{64})?$", exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _validate_source(self) -> "FreshOdooTestingGeneration":
        if (self.source_mode == "container-export") != bool(self.source_binding_sha256):
            raise ValueError("A native source needs its exact binding; SSH generations carry none.")
        if bool(self.discard_drill_database) != bool(self.discard_copy_binding) or (
            self.discard_drill_database and self.discard_drill_database == self.drill_database
        ):
            raise ValueError("Drill disposal requires a different witnessed prior drill database.")
        if bool(self.retained_drill_database) != bool(self.retained_copy_binding) or (
            self.retained_drill_database
            and self.retained_drill_database in {self.drill_database, self.discard_drill_database}
        ):
            raise ValueError("A retained predecessor must differ from the new and disposed copies.")
        return self


def fresh_testing_override_record(
    record: OdooInstanceOverrideRecord | None, generation: FreshOdooTestingGeneration
) -> OdooInstanceOverrideRecord | None:
    """Render the same generation URL at deployment and restore, without editing intent."""
    if record is None:
        return None
    if (record.context, record.instance) != (generation.context, generation.instance):
        raise ValueError("Fresh testing overrides belong to another lane.")
    if record.website_bootstrap is None:
        return record
    return record.model_copy(
        update={
            "website_bootstrap": record.website_bootstrap.model_copy(
                update={"canonical_url": "https://" + generation.domain_host}
            )
        }
    )


def fresh_testing_mode_refusal(
    generation: FreshOdooTestingGeneration | None, mode: str
) -> tuple[OdooTargetReplacementPlanBlockerCode, str] | None:
    if generation is None:
        return None
    if not generation.reconcile_requested_at:
        return (
            "fresh_testing_bootstrap_incomplete",
            "Finish the original bootstrap route and queue preparation before executing this generation.",
        )
    if not generation.completed_at and mode != "upstream_restore":
        return (
            "fresh_testing_restore_required",
            "Complete this generation's sanitized production-copy restore before deploying with existing data.",
        )
    if generation.completed_at and mode == "upstream_restore":
        return (
            "fresh_testing_refresh_unsupported",
            "This generation's initial copy is complete. Deploy using existing data; a later production refresh requires a separately qualified generation.",
        )
    return None


# This wrapper uses the artifact's SQL-only credential boundary before the first
# registry process. The runner gets no upstream or SSH settings. Additional copy
# connection rules run in that same transaction boundary and again after hooks.
# A missing boundary is an unsupported artifact, never permission to run it.
TESTING_COPY_COMPLETION_PARAMETER = "launchplane.testing_copy.restore_complete"
TESTING_COPY_RUNNER = (
    """import base64
import logging
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/volumes/scripts')
from run_odoo_data_workflows import LocalServerSettings, OdooDataWorkflowRunner
"""
    + connection_isolation_library()
    + r"""
logging.disable(logging.CRITICAL)
if os.environ.get("PLATFORM_INSTANCE") != "testing":
    raise SystemExit("Testing copy requires its exact instance")
for key in tuple(os.environ):
    if key.startswith("ODOO_UPSTREAM_") or key.startswith("DATA_WORKFLOW_SSH_"):
        os.environ.pop(key)
os.environ["ODOO_RESTORE_KEPT_INTEGRATIONS"] = ""
os.environ["ENV_OVERRIDE_DISABLE_CRON"] = "true"
os.environ["NO_SANITIZE"] = "false"


class CopyRunner(OdooDataWorkflowRunner):
    def overwrite_filestore(self, target_owner):
        # Host capture has already staged the fresh local filestore. No SSH key
        # or network source exists inside this container.
        return subprocess.Popen(["true"])

    def prepare_credentials_before_registry(self, *args, **kwargs):
        super().prepare_credentials_before_registry(*args, **kwargs)
        self.connect_to_db()
        with self.local.db_conn.cursor() as cursor:
            refused = connection_isolation(cursor, apply=True)
            if refused:
                raise RuntimeError("Testing copy connection isolation failed")
        self.local.db_conn.commit()
        observer = self._connect_with_retry(self.local.db_name)
        try:
            with observer.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                if connection_isolation(cursor, apply=False):
                    raise RuntimeError("Testing copy connection commit readback failed")
            observer.rollback()
        finally:
            observer.close()
        # The shared SQL-only integration readback checks all supported families,
        # including instance overrides reapplied by the native credential pass.
        checked = subprocess.run(["python3", "-", self.local.db_name, sys.argv[3]],
            input=base64.b64decode(sys.argv[4]), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False)
        if checked.returncode or b"integration_readback_ok=true" not in checked.stdout.splitlines():
            raise RuntimeError("Testing copy pre-registry integration readback failed")
        print("testing_copy_pre_registry_sanitized=true", flush=True)


if not callable(getattr(OdooDataWorkflowRunner, "prepare_credentials_before_registry", None)):
    raise SystemExit("Artifact lacks the pre-registry credential boundary")
settings = LocalServerSettings()
if settings.host != "database" or settings.db_name != sys.argv[2] or settings.platform_instance != "testing":
    raise SystemExit("Testing copy settings no longer name the isolated database")
runner = CopyRunner(settings, None, None)
runner.acquire_data_workflow_lock()
restored = False
try:
    dump = Path(sys.argv[1])
    copy_marker = """
    + repr(TESTING_COPY_COMPLETION_PARAMETER)
    + r"""
    if sys.argv[5] == "fresh":
        observer = runner._connect_with_retry("postgres")
        try:
            with observer.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (runner.local.db_name,))
                if cursor.fetchone()[0]:
                    raise RuntimeError("Fresh copy destination appeared before the restore lock")
        finally:
            observer.close()
        subprocess.run(["pg_restore", "--file=/dev/null", str(dump)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restored = True
        runner.overwrite_database(dump)
        dump.unlink()
        runner.connect_to_db()
        with runner.local.db_conn.cursor() as cursor:
            cursor.execute("INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (copy_marker, sys.argv[6]))
        runner.local.db_conn.commit()
    elif sys.argv[5] == "resume":
        runner.connect_to_db()
        with runner.local.db_conn.cursor() as cursor:
            cursor.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (copy_marker,))
            if cursor.fetchone() != (sys.argv[6],):
                raise RuntimeError("Existing copy lacks this generation's completed restore witness")
    else:
        raise RuntimeError("Unknown fresh testing restore phase")
    runner.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
    modules = [name.strip() for name in (os.environ.get("ODOO_UPDATE_MODULES") or "").split(",") if name.strip()]
    if not modules or "AUTO" in modules:
        raise RuntimeError("Fresh copy needs exact declared module intent")
    before = runner.module_update_witnesses(modules)
    runner._prepare_restored_database(runner._resolve_filestore_owner(), do_sanitize=True)
    states = runner._module_states_by_name()
    after = runner.module_update_witnesses(modules)
    if any(states.get(name) != "installed" or not after.get(name) or before.get(name) == after[name] for name in modules) or any(state in {"to install", "to upgrade", "to remove"} for state in states.values()):
        raise RuntimeError("Fresh copy lacks completed module update witnesses")
    print("odoo_module_update_completed=true", flush=True)
    print("odoo_module_update_modules_configured=true", flush=True)
    runner.assert_core_schema_healthy()
    runner.ensure_gpt_users()
    runner.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
    print("testing_copy_sanitized=true", flush=True)
except BaseException:
    if restored and sys.argv[5] == "fresh":
        runner._drop_database_after_failed_restore()
    raise
finally:
    runner.release_data_workflow_lock()
"""
)


def encoded_testing_copy_runner() -> str:
    return base64.b64encode(TESTING_COPY_RUNNER.encode()).decode()


def generation_binding(generation: FreshOdooTestingGeneration) -> str:
    from control_plane.contracts.canonical_json import canonical_json_sha256

    return canonical_json_sha256(generation_identity_payload(generation))


def generation_snapshot_matches(
    current: FreshOdooTestingGeneration | None, expected: FreshOdooTestingGeneration
) -> bool:
    """Monotonic non-use revocation is protection, not a new execution plan."""
    return current is not None and current.model_dump(
        exclude={"non_use_invalidated"}
    ) == expected.model_dump(exclude={"non_use_invalidated"})


def generation_identity_payload(generation: FreshOdooTestingGeneration) -> dict[str, object]:
    payload = generation.model_dump(
        mode="json",
        exclude={
            "completed_at",
            "reconcile_requested_at",
            "non_use_tracking_started_at",
            "non_use_invalidated",
        },
    )
    if generation.source_mode == "ssh":
        payload.pop("source_mode")
        payload.pop("source_binding_sha256")
    elif not generation.source_binding_sha256:
        raise ValueError("Native testing copy lacks its reviewed production source binding.")
    return payload


def encoded_restore_source(environment: dict[str, str]) -> str:
    """Only the existing supported source coordinates; no credential carrier."""
    from control_plane.dokploy.post_deploy import resolve_upstream_restore_workflow_environment

    source = resolve_upstream_restore_workflow_environment(desired_env_map=environment)
    return base64.b64encode(json.dumps(source, sort_keys=True).encode()).decode()


def preflight_fresh_testing_intent(*, modules: str, encoded_payload: str) -> None:
    """Use the strict restore policy before provider effects; expose no payload values."""
    from control_plane.contracts.odoo_target_replacement_failures import (
        OdooFreshTestingIntentRefusedError,
    )
    from control_plane.integration_readback import INTEGRATION_FAMILIES, IntegrationReadbackPolicy

    policy = IntegrationReadbackPolicy(
        families=INTEGRATION_FAMILIES,
        allowed_integrations=(),
        protected_shopify_store_handles=(),
    )
    try:
        validate_fresh_testing_intent(
            modules=modules, encoded_payload=encoded_payload, readback_spec=policy.encoded_spec()
        )
    except ValueError as error:
        raise OdooFreshTestingIntentRefusedError() from error


def validate_fresh_testing_intent(
    *, modules: str, encoded_payload: str, readback_spec: str
) -> None:
    """Refuse predictable execution failures before any production capture."""
    import re
    from control_plane.odoo_connection_isolation import CONNECTION_ISOLATION_PARAMETERS

    names = [name.strip() for name in modules.split(",") if name.strip()]
    if not names or any(
        name.upper() == "AUTO" or not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names
    ):
        raise ValueError("Fresh testing requires exact declared module intent.")
    if not encoded_payload:
        return
    payload = json.loads(base64.b64decode(encoded_payload, validate=True))
    policy = json.loads(base64.b64decode(readback_spec, validate=True))
    forbidden = {key for family in policy["families"] for key in family["config_parameters"]}
    for item in payload.get("config_parameters", []):
        key = item["key"]
        if key in forbidden or key.startswith(
            ("google_analytics.", "plausible.", "google_tag_manager.")
        ):
            raise ValueError("Fresh testing cannot apply integration connection settings.")
        if key in CONNECTION_ISOLATION_PARAMETERS and item["value"] != {
            "source": "literal",
            "value": CONNECTION_ISOLATION_PARAMETERS[key],
        }:
            raise ValueError("Fresh testing cannot replace its connection fences.")
    for item in payload.get("addon_settings", []):
        if (
            item.get("addon") == "shopify"
            and item.get("setting") == "action"
            and item.get("value") != {"source": "literal", "value": "clear"}
        ):
            raise ValueError("Fresh testing cannot enable a managed integration.")


class FreshRestoreScriptArguments(TypedDict):
    generation: FreshOdooTestingGeneration
    compose_project: str
    image_reference: str
    database_name: str
    source_environment: dict[str, str]
    readback_spec: str
    readback_program: str
    workflow_environment: dict[str, str]
    runtime_environment: dict[str, str]
    clear_stale_lock: bool
    data_workflow_lock_path: str


def build_fresh_testing_restore_script(
    *,
    generation: FreshOdooTestingGeneration,
    compose_project: str,
    image_reference: str,
    database_name: str,
    source_environment: dict[str, str],
    readback_spec: str,
    readback_program: str,
    workflow_environment: dict[str, str],
    runtime_environment: dict[str, str],
    clear_stale_lock: bool,
    data_workflow_lock_path: str,
    native_capture: dict[str, object] | None = None,
) -> str:
    spec = fresh_testing_restore_spec(
        generation=generation,
        compose_project=compose_project,
        image_reference=image_reference,
        database_name=database_name,
        source_environment=source_environment,
        readback_spec=readback_spec,
        readback_program=readback_program,
        workflow_environment=workflow_environment,
        runtime_environment=runtime_environment,
        clear_stale_lock=clear_stale_lock,
        data_workflow_lock_path=data_workflow_lock_path,
        native_capture=native_capture,
    )
    encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
        + encoded
        + " <<'LP_FRESH_COPY'\n"
        + FRESH_TESTING_RESTORE_HOST_PROGRAM
        + "\nLP_FRESH_COPY\n"
    )


def fresh_testing_restore_spec(
    *,
    generation: FreshOdooTestingGeneration,
    compose_project: str,
    image_reference: str,
    database_name: str,
    source_environment: dict[str, str],
    readback_spec: str,
    readback_program: str,
    workflow_environment: dict[str, str],
    runtime_environment: dict[str, str],
    clear_stale_lock: bool,
    data_workflow_lock_path: str,
    native_capture: dict[str, object] | None = None,
) -> dict[str, object]:
    from control_plane.dokploy.compose import (
        ODOO_DATABASE_DATA_DIRECTORY,
        ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
    )
    from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

    payload = workflow_environment.get(
        "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64",
        runtime_environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""),
    )
    validate_fresh_testing_intent(
        modules=workflow_environment.get("ODOO_UPDATE_MODULES", ""),
        encoded_payload=payload,
        readback_spec=readback_spec,
    )
    spec = {
        "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
        "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
        "runner_healthcheck_command": ODOO_SCRIPT_RUNNER_HEALTHCHECK_COMMAND,
        "expected_overrides_payload": payload,
        "stale_lock_program": FRESH_TESTING_STALE_LOCK_PROGRAM,
        "clear_stale_lock": clear_stale_lock,
        "data_workflow_lock_path": data_workflow_lock_path,
        "generation": generation.model_dump(mode="json"),
        "compose_project": compose_project,
        "image_reference": image_reference,
        "database_name": database_name,
        "source": json.loads(base64.b64decode(encoded_restore_source(source_environment)))
        if generation.source_mode == "ssh"
        else {},
        "native_capture": native_capture,
        "readback_spec": readback_spec,
        "readback_program": readback_program,
        # This witnesses the completed source capture, not a particular future
        # artifact or settings payload. Every resume re-runs their own boundary
        # and module/readback proof before the payload-bound web gate opens.
        "copy_binding_sha256": generation_binding(generation),
        "runner": TESTING_COPY_RUNNER,
        "native_files_program": NATIVE_TESTING_FILES_PROGRAM,
        "website_settings_program": WEBSITE_SETTINGS_PROGRAM,
        "workflow_environment": workflow_environment,
        "ssh_key_path": source_environment.get("DATA_WORKFLOW_SSH_KEY", ""),
        "ssh_directory": source_environment.get("DATA_WORKFLOW_SSH_DIR", ""),
    }
    return spec


FRESH_TESTING_STALE_LOCK_PROGRAM = r"""import os,sys
from pathlib import Path
path=Path(sys.argv[1])
if not path.is_absolute() or '..' in path.parts or not path.resolve().is_relative_to(Path(sys.argv[3]).resolve()) or path.is_symlink():
    raise RuntimeError('Unexpected testing workflow lock path')
for process in Path(sys.argv[4]).iterdir():
    if not process.name.isdigit() or int(process.name) == os.getpid():
        continue
    try:
        arguments=(process/'cmdline').read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        continue
    tokens=arguments.split(b'\0')
    if b'-c' in tokens:
        position=tokens.index(b'-c')
        if position+1 < len(tokens) and tokens[position+1].strip() == sys.argv[6].encode():
            continue
    if any(marker in arguments for marker in (b'odoo-bin', b'run_odoo_', b'pg_restore', sys.argv[5].encode())):
        raise RuntimeError('Testing data workflow still has an active process')
if path.exists():
    fields=dict(line.split('=',1) for line in path.read_text().splitlines() if '=' in line)
    pid=int(fields['pid'])
    if pid < 1:
        raise RuntimeError('Invalid testing workflow lock owner')
    try:
        os.kill(pid,0)
    except ProcessLookupError:
        if sys.argv[2] != 'true':
            raise RuntimeError('Testing workflow lock lacks terminal schedule proof')
        path.unlink()
    else:
        raise RuntimeError('Testing workflow lock still has a live process')
"""


_NATIVE_TESTING_FILES_PROGRAM_TEMPLATE = r"""
import base64,hashlib,json,os,re,sys,tarfile,tempfile
from pathlib import Path,PurePosixPath
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
capture=spec['capture']
identity=capture['capture_id']
if not re.fullmatch(r'[a-f0-9]{32}',identity):
    raise RuntimeError('Invalid capture identity')
mode=spec['mode']
chunk_size=__LP_NATIVE_CHUNK_BYTES__
def path_for(part,index):
    return Path('/tmp')/('lp-native-'+identity+'-'+part+'-'+str(index))
def chunks(part):
    record=capture[part]
    if not 0 < record['size'] <= __LP_NATIVE_ARTIFACT_BYTES__ or not re.fullmatch(r'[a-f0-9]{64}',record['sha256']):
        raise RuntimeError('Invalid artifact receipt')
    return range((record['size']+chunk_size-1)//chunk_size)
observed=[]
whole={}
for part in ('database','filestore'):
    complete=True;whole_digest=hashlib.sha256();whole_size=0
    for index in chunks(part):
        path=path_for(part,index)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError('Unexpected artifact staging path')
        if mode=='inspect':
            if path.exists():
                with path.open('rb') as handle:
                    data=handle.read(chunk_size+1)
                if len(data)>chunk_size:
                    raise RuntimeError('Artifact staging exceeds its chunk bound')
                observed.append({'part':part,'index':index,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                expected=min(chunk_size,capture[part]['size']-index*chunk_size)
                complete=complete and len(data)==expected
                whole_digest.update(data);whole_size+=len(data)
            else:
                complete=False
        elif mode=='cleanup':
            path.unlink(missing_ok=True)
    if mode=='inspect' and complete:
        whole[part]={'sha256':whole_digest.hexdigest(),'size':whole_size}
if mode=='inspect':
    print(json.dumps({'chunks':observed,'artifacts':whole}));sys.exit(0)
if mode=='cleanup':
    sys.exit(0)
if mode!='prepare':
    raise RuntimeError('Unsupported artifact operation')
artifacts={}
try:
    for part in ('database','filestore'):
        destination=Path('/tmp')/('lp-native-'+identity+'-'+part+'.artifact')
        if destination.is_symlink():
            raise RuntimeError('Unclaimed assembled artifact exists')
        artifacts[part]=destination
        if destination.exists():
            with destination.open('rb') as handle:
                existing=hashlib.file_digest(handle,'sha256').hexdigest()
            if not destination.is_file() or destination.stat().st_size!=capture[part]['size'] or existing!=capture[part]['sha256']:
                raise RuntimeError('An existing assembled artifact differs')
            continue
        digest=hashlib.sha256();size=0
        temporary=None
        try:
          with tempfile.NamedTemporaryFile(dir='/tmp',prefix='lp-native-'+identity+'-assemble-',delete=False) as output:
            temporary=Path(output.name)
            for index in chunks(part):
                path=path_for(part,index)
                if path.is_symlink():
                    raise RuntimeError('Artifact staging changed')
                with path.open('rb') as handle:
                    data=handle.read(chunk_size+1)
                expected=min(chunk_size,capture[part]['size']-index*chunk_size)
                if len(data)!=expected:
                    raise RuntimeError('Artifact staging is incomplete')
                output.write(data);digest.update(data);size+=len(data)
            output.flush();os.fsync(output.fileno())
          if size!=capture[part]['size'] or digest.hexdigest()!=capture[part]['sha256']:
              raise RuntimeError('Artifact integrity readback failed')
          os.link(temporary,destination)
        finally:
          if temporary is not None:
              temporary.unlink(missing_ok=True)
    database=spec['database_name']
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}',database):
        raise RuntimeError('Invalid testing database')
    root=Path('/volumes/data/filestore')/database
    if root.resolve()!=root.absolute():
        raise RuntimeError('Unsafe testing filestore')
    root.mkdir(parents=True,exist_ok=True)
    total=0
    with tarfile.open(artifacts['filestore'],'r:gz') as archive:
        for member in archive:
            relative=PurePosixPath(member.name)
            if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise RuntimeError('Unsafe source archive entry')
            name=str(relative)
            if member.isdir() or name.startswith('checklist/'):
                continue
            if not re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{40}',name):
                raise RuntimeError('Unsupported source attachment path')
            total+=member.size
            if total>512*1024*1024:
                raise RuntimeError('Expanded source exceeds its bound')
            path=root/name
            if path.resolve()!=path.absolute() or path.is_symlink():
                raise RuntimeError('Unsafe attachment destination')
            path.parent.mkdir(exist_ok=True)
            with archive.extractfile(member) as handle:
                data=handle.read(member.size+1)
            if len(data)!=member.size:
                raise RuntimeError('Source attachment is truncated')
            if path.exists():
                if not path.is_file() or path.read_bytes()!=data:
                    raise RuntimeError('An existing testing attachment differs')
            else:
                temporary=None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.lp-native-'+identity+'-',delete=False) as handle:
                        temporary=Path(handle.name)
                        handle.write(data);handle.flush();os.fsync(handle.fileno())
                    os.link(temporary,path)
                    descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
    for directory in (root,root.parent):
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    destination=Path(spec['dump_path'])
    if destination.parent!=Path('/tmp') or not re.fullmatch(r'lp-testing-[a-f0-9]{32}-[a-f0-9]{32}-database.dump',destination.name):
        raise RuntimeError('Unsafe testing dump destination')
    os.link(artifacts['database'],destination)
    os.chmod(destination,0o644)
finally:
    for path in artifacts.values():
        path.unlink(missing_ok=True)
"""


def render_native_testing_files_program() -> str:
    return _NATIVE_TESTING_FILES_PROGRAM_TEMPLATE.replace(
        "__LP_NATIVE_CHUNK_BYTES__", str(NATIVE_TESTING_COPY_CHUNK_BYTES)
    ).replace("__LP_NATIVE_ARTIFACT_BYTES__", str(NATIVE_TESTING_COPY_ARTIFACT_BYTES))


NATIVE_TESTING_FILES_PROGRAM = render_native_testing_files_program()


FRESH_TESTING_RESTORE_HOST_PROGRAM = r"""
import base64
import hashlib
import fcntl
import os
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid


def command(arguments, *, stdin=None, stdout=None):
    result = subprocess.run(arguments, input=stdin, stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE, check=False)
    if result.returncode:
        if any(word in result.stderr.lower() for word in (b'permission denied',b'password authentication failed',b'unauthorized')):
            raise PermissionError('The testing runtime refused this identity')
        raise RuntimeError("Fresh testing copy command did not pass")
    return result.stdout or b""


def docker(*arguments, stdin=None):
    return command(["docker", *arguments], stdin=stdin)


def one_inspection(identifier):
    values = json.loads(docker("inspect", identifier))
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError("Testing runtime inventory is incomplete")
    return values[0]


def discard_previous_drill(gen, database_container, runner, database, receipt_root=None):
    from datetime import datetime, timezone
    def current():
        if 'disposal_expires_at' in gen and datetime.now(timezone.utc).timestamp() >= gen['disposal_expires_at']:
            raise RuntimeError('Disposal qualification expired')
    current()
    previous = gen.get('discard_drill_database', '')
    witness = gen.get('discard_copy_binding', '')
    if not previous:
        if witness:
            raise RuntimeError('Unbound drill disposal witness')
        return
    protected = {database, gen.get('drill_database'), gen.get('retained_drill_database')}
    if not re.fullmatch(r'lp_drill_[a-f0-9]{32}', previous) or previous in protected or not re.fullmatch(r'[a-f0-9]{64}', witness):
        raise RuntimeError('Invalid prior drill database')
    # This host-owned state is never mounted in an application container.
    # Traverse beneath a trusted anchor with directory fds, never path-following
    # opens that a container or another user could redirect during a check.
    if receipt_root is None:
        anchor = Path.home().resolve(strict=True)
        components = ('.local', 'state', 'launchplane-testing-disposal', gen['namespace'])
    else:
        anchor = Path(receipt_root).resolve(strict=True)
        components = ()
    directory = os.open(anchor, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for component in components:
            try:
                os.mkdir(component, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=directory)
            owner = os.fstat(child)
            if owner.st_uid != os.geteuid() or owner.st_mode & 0o022:
                os.close(child)
                raise RuntimeError('Disposal state is not host-owned')
            os.fsync(directory)
            os.close(directory)
            directory = child
        receipt = previous + '.json'
        expected = {'database':previous,'witness':witness}
        recorded = False
        try:
            descriptor = os.open(receipt, os.O_RDONLY|os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(descriptor) as handle:
                if json.loads(handle.read(4097)) != expected:
                    raise RuntimeError('Disposal receipt identity changed')
            recorded = True
        present = docker('exec','-i',database_container,'sh','-c',
            'psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
            '_', previous, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
        if present not in {'0','1'}:
            raise RuntimeError('Ambiguous prior drill database')
        if present == '1':
            observed = docker('exec','-i',database_container,'sh','-c',
                'psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"',
                '_', previous, stdin=b"SELECT value FROM ir_config_parameter WHERE key='__LP_COPY_WITNESS__';\n").decode().strip()
            if observed != witness:
                raise RuntimeError('Prior database lacks its committed drill witness')
            if not recorded:
                descriptor = os.open(receipt, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(descriptor,'w') as handle:
                    json.dump(expected,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
                os.fsync(directory)
                recorded = True
            # No FORCE or connection termination. Unexpected connections retain it.
            current()
            docker('exec','-i',database_container,'sh','-c',
                'psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
                '_', previous, stdin=b'DROP DATABASE :"database_name";\n')
        elif not recorded:
            absent = docker('exec',runner,'python3','-c',
                "from pathlib import Path;import sys;p=Path('/volumes/data/filestore')/sys.argv[1];assert p.resolve()==p.absolute() and not p.is_symlink();print('absent' if not p.exists() else 'present')",previous).decode().strip()
            if absent == 'absent':
                return
            return 'retained'
        current()
        docker('exec',runner,'python3','-c',
            "from pathlib import Path;import re,shutil,sys;name=sys.argv[1];assert re.fullmatch(r'lp_drill_[a-f0-9]{32}',name);p=Path('/volumes/data/filestore')/name;assert p.resolve()==p.absolute() and not p.is_symlink();shutil.rmtree(p) if p.exists() else None", previous)
        # Keep the receipt even on success: original-key readback remains possible.
    finally:
        os.close(directory)


def restore_main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    gen = spec["generation"]
    namespace = gen["namespace"]
    project = spec["compose_project"]
    database = spec["database_name"]
    if gen["instance"] != "testing" or not re.fullmatch(r"lp-testing-[a-f0-9]{32}", namespace):
        raise RuntimeError("Testing generation is invalid")
    if not re.fullmatch(re.escape(namespace) + r"(?:-[a-z0-9]{6})?", project):
        raise RuntimeError("Testing compose identity changed")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}", database):
        raise RuntimeError("Testing database identity is invalid")
    modules = [name.strip() for name in str(spec.get("workflow_environment", {}).get("ODOO_UPDATE_MODULES", "")).split(",") if name.strip()]
    if not (spec.get('storage_maintenance') or spec.get('usage_only')) and (not modules or any(name.upper() == "AUTO" or not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in modules)):
        raise RuntimeError("Testing restore has no exact declared module update intent")
    volumes = {namespace + "-data", namespace + "-db", namespace + "-logs"}
    containers = {}
    all_containers = []
    for identifier in docker("ps", "-aq").decode().splitlines():
        container = one_inspection(identifier)
        all_containers.append(container)
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == project:
            service = labels.get("com.docker.compose.service")
            if service not in {"web", "database", "script-runner"} or service in containers:
                raise RuntimeError("Testing compose has an unexpected container")
            containers[service] = (identifier, container)
    if set(containers) != {"web", "database", "script-runner"}:
        raise RuntimeError("Testing compose inventory is incomplete")
    for service, (identifier, container) in containers.items():
        configuration = container["Config"]
        if container.get("HostConfig", {}).get("Privileged") or container.get("HostConfig", {}).get("NetworkMode") == "host" or container.get("HostConfig", {}).get("PidMode") not in (None, "", "private"):
            raise RuntimeError("Testing compose has host capabilities")
        if any(container.get("HostConfig", {}).get("PortBindings", {}).values()):
            raise RuntimeError("Testing compose publishes host ports")
        expected = {namespace + "-db"} if service == "database" else {namespace + "-data", namespace + "-logs"}
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or {item.get("Name") for item in mounts} != expected or any(item.get("Type") != "volume" for item in mounts):
            raise RuntimeError("Testing compose has unexpected writable or credential mounts")
        if service == "database" and mounts[0].get("Destination") != spec["database_data_directory"]:
            raise RuntimeError("Testing database storage is not mounted at its data directory")
        if service != "database":
            if configuration.get("Image") != spec["image_reference"]:
                raise RuntimeError("Testing artifact identity changed")
            environment = dict(item.split("=", 1) for item in configuration.get("Env", []) if "=" in item)
            allowed_databases = spec.get('usage_database_names',[database]) if spec.get('usage_only') else [database]
            if environment.get("PLATFORM_INSTANCE") != "testing" or environment.get("ODOO_DB_HOST") != "database" or environment.get("ODOO_DB_NAME") not in allowed_databases:
                raise RuntimeError("Testing database endpoint is not local")
            if not (spec.get('storage_maintenance') or spec.get('usage_only')) and environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", "") != spec["expected_overrides_payload"]:
                raise RuntimeError("Testing settings changed after execution preflight")
            if environment.get("ODOO_MAX_CRON_THREADS") != "0":
                raise RuntimeError("Fresh testing must have no cron workers")
            if container.get("HostConfig", {}).get("ExtraHosts"):
                raise RuntimeError("Fresh testing has host gateway aliases")
            if any(environment.get(key) for key in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DATA_WORKFLOW_SSH_KEY", "ODOO_KEY", "ODOO_SMTP_PASSWORD")):
                raise RuntimeError("Testing holds an execution credential")
        if service != "web":
            networks = container.get("NetworkSettings", {}).get("Networks")
            if not isinstance(networks, dict) or len(networks) != 1:
                raise RuntimeError("Testing data containers are not privately networked")
            network = one_inspection(next(iter(networks)))
            if network.get("Internal") is not True:
                raise RuntimeError("Testing data network has outbound access")
    data_networks = set(containers["database"][1]["NetworkSettings"]["Networks"])
    if set(containers["script-runner"][1]["NetworkSettings"]["Networks"]) != data_networks or set(containers["web"][1]["NetworkSettings"]["Networks"]) != data_networks | {"dokploy-network"}:
        raise RuntimeError("Fresh testing has unexpected network reach")
    for container in all_containers:
        if container in [item[1] for item in containers.values()]:
            continue
        if any(mount.get("Name") in volumes for mount in container.get("Mounts", [])):
            raise RuntimeError("Testing storage is shared by another container")
    # Resolve actual backing paths too: a bind alias of a named volume is
    # storage sharing even when Docker reports different mount names.
    paths = []
    identities = set()
    for name in volumes:
        volume = json.loads(docker("volume", "inspect", name))[0]
        if volume.get("Driver") != "local" or volume.get("Options") not in (None, {}) or volume.get("Labels", {}).get("io.launchplane.testing-resources") != gen["resources_binding_sha256"]:
            raise RuntimeError("Testing backing storage changed")
        backing = Path(volume["Mountpoint"]).resolve(strict=True)
        identity = (backing.stat().st_dev, backing.stat().st_ino)
        if identity in identities:
            raise RuntimeError("Testing backing storage overlaps")
        identities.add(identity)
        paths.append(backing)
    own = {item[1]["Id"] for item in containers.values()}
    for container in all_containers:
        if container.get("Id") in own:
            continue
        for mount in container.get("Mounts", []):
            if not mount.get("Source"):
                continue
            source_path = Path(mount["Source"]).resolve(strict=False)
            if any(source_path == path or source_path.is_relative_to(path)
                   or (mount.get("RW", True) and path.is_relative_to(source_path))
                   for path in paths):
                raise RuntimeError("Testing storage has a bind consumer")
    private_state = Path.home().resolve(strict=True) / '.local/state/launchplane-testing-disposal'
    for container in all_containers:
        for mount in container.get('Mounts', []):
            if mount.get('RW', True) and mount.get('Source'):
                source_path = Path(mount['Source']).resolve(strict=False)
                if private_state.is_relative_to(source_path) or source_path.is_relative_to(private_state):
                    raise RuntimeError('Disposal state is exposed to a container')
    runner = containers["script-runner"][0]
    web = containers["web"][0]
    if spec.get('storage_maintenance'):
        storage_maintenance(spec, containers)
        return
    if spec.get('inspection_only'):
        # Read-only qualification uses this exact same container/storage/network
        # guard as execution. It cannot stop a container, alter a gate or restore.
        docker("exec",runner,"python3","-c","import sys;sys.path.insert(0,'/volumes/scripts');from run_odoo_data_workflows import OdooDataWorkflowRunner as R;assert all(callable(getattr(R,name,None)) for name in ('prepare_credentials_before_registry','_prepare_restored_database','module_update_witnesses','_module_states_by_name'))")
        present=docker('exec','-i',containers['database'][0],'sh','-c','psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres','_',database,stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
        if present not in {'0','1'}:
            raise RuntimeError('Ambiguous testing database')
        if present=='1':
            marker=docker('exec','-i',containers['database'][0],'sh','-c','psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"','_',database,stdin=b"SELECT value FROM ir_config_parameter WHERE key='__LP_COPY_WITNESS__';\n").decode().strip()
            if marker!=spec['copy_binding_sha256']:
                raise RuntimeError('Existing testing database lacks its generation witness')
        prefix=sys.argv[2]
        if spec.get('task_account_cleanup') is True:
            if present != '1':
                raise RuntimeError('Testing cleanup database is unavailable')
            print(prefix+json.dumps({'type':'complete','cleanup_qualified':True}),flush=True)
            return
        if spec.get('usage_only'):
            if present != '1':
                raise RuntimeError('Testing login history is unavailable')
            if spec.get('usage_reclaim'):
                new_database=spec['usage_new_database']
                if not re.fullmatch(r'lp_drill_[a-f0-9]{32}',new_database):
                    raise RuntimeError('Reclaimed testing database is unbound')
                # Running includes the shell waiting behind the web-start gate.
                # The gate, removed before restore creates this database, is the
                # existing witness that this new copy may have served testers.
                serving=False
                new_present=docker('exec','-i',containers['database'][0],'sh','-c',
                    'psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
                    '_',new_database,stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
                if new_present not in {'0','1'}:
                    raise RuntimeError('Reclaimed testing database is unreadable')
                if new_present=='1':
                    gate=docker('exec',runner,'python3','-c',
                        "from pathlib import Path;import sys;print(Path(sys.argv[1]).is_file())",spec['web_gate_path']).decode().strip()
                    if gate not in {'True','False'}:
                        raise RuntimeError('Testing web gate is unreadable')
                    serving=serving or gate=='True'
                if serving:
                    print(prefix+json.dumps({'type':'complete','new_copy_serving':True}),flush=True)
                    return
            # Odoo records successful logins in res_users_log.create_date with
            # create_uid naming the account. Read all accounts without logging
            # identities or creating an Odoo session (which would add a login).
            since=spec['usage_since']
            from datetime import datetime,timezone
            moment=datetime.fromisoformat(since)
            if moment.tzinfo is None:
                raise RuntimeError('Testing usage cutoff requires UTC authority')
            cutoff=moment.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
            used=docker('exec','-i',containers['database'][0],'sh','-c',
                'psql -X -q -t -A -v ON_ERROR_STOP=1 -v usage_since="$2" -U "$POSTGRES_USER" -d "$1"',
                '_',database,cutoff,stdin=b"BEGIN READ ONLY; SET LOCAL statement_timeout = '10s'; SELECT EXISTS (SELECT 1 FROM res_users_log WHERE create_date >= :'usage_since'::timestamp); ROLLBACK;\n").decode().strip()
            if used not in {'t','f'}:
                raise RuntimeError('Testing login history is unreadable')
            print(prefix+json.dumps({'type':'complete','recent_signins':used=='t'}),flush=True)
            return
        capture=spec.get('native_capture')
        artifacts={}
        if capture is not None and present=='0':
            files=json.loads(docker('exec','-i','-u','root',runner,'python3','-',base64.b64encode(json.dumps({'capture':capture,'mode':'inspect'}).encode()).decode(),stdin=spec['native_files_program'].encode()))
            for item in files['chunks']:
                print(prefix+json.dumps({'type':'chunk',**item}),flush=True)
            artifacts=files['artifacts']
        print(prefix+json.dumps({'type':'complete','runner':containers['script-runner'][1]['Id'],'resume':present=='1','artifacts':artifacts}),flush=True)
        return
    present = docker("exec", "-i", containers["database"][0], "sh", "-c",
        'psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
        "_", database, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
    if present not in {"0", "1"}:
        raise RuntimeError("Fresh testing database inventory is ambiguous")
    resume = present == "1"
    staged_bytes = 0
    assembly_bytes = 0
    if not resume and gen.get('drill_storage_budget') and gen.get('source_mode') == 'container-export':
        capture = spec.get('native_capture')
        if capture is None:
            raise RuntimeError('Native copy lacks its original capture receipt')
        files = json.loads(docker('exec','-i','-u','root',runner,'python3','-',base64.b64encode(json.dumps({'capture':capture,'mode':'inspect'}).encode()).decode(),stdin=spec['native_files_program'].encode()))
        staged_bytes = sum(item['size'] for item in files['chunks'])
        assembly_bytes = sum(capture[part]['size'] for part in ('database', 'filestore'))
    if not resume and gen.get('drill_storage_budget') and not storage_capacity(spec, containers, gen['drill_storage_budget'], staged_bytes, assembly_bytes):
        raise RuntimeError('Testing storage capacity is insufficient')
    docker("exec", "-u", "root", runner, "rm", "-f", spec["web_gate_path"])
    docker("stop", web)
    docker("exec", "-i", runner, "python3", "-", spec["data_workflow_lock_path"],
        "true" if spec["clear_stale_lock"] else "false", "/volumes/data", "/proc", database, spec["runner_healthcheck_command"], stdin=spec["stale_lock_program"].encode())
    print("testing_copy_runtime_isolated=true", flush=True)
    docker("exec", runner, "python3", "-c", "import sys;sys.path.insert(0,'/volumes/scripts');from run_odoo_data_workflows import OdooDataWorkflowRunner as R;assert all(callable(getattr(R,name,None)) for name in ('prepare_credentials_before_registry','_prepare_restored_database','module_update_witnesses','_module_states_by_name'))")
    staging = None
    try:
        dump_path = "/tmp/" + namespace + "-" + uuid.uuid4().hex + "-database.dump"
        if not resume and gen.get('source_mode','ssh')=='container-export':
            capture=spec.get('native_capture')
            if capture is None or capture['generation_binding_sha256']!=spec['copy_binding_sha256'] or capture['source_binding_sha256']!=gen['source_binding_sha256']:
                raise RuntimeError('Native copy lacks its original capture receipt')
            docker('exec','-i','-u','root',runner,'python3','-',base64.b64encode(json.dumps({'capture':capture,'mode':'prepare','database_name':database,'dump_path':dump_path}).encode()).decode(),stdin=spec['native_files_program'].encode())
        elif not resume:
            source = spec["source"]
            for key in ("ODOO_UPSTREAM_HOST", "ODOO_UPSTREAM_USER", "ODOO_UPSTREAM_DB_USER", "ODOO_UPSTREAM_DB_NAME"):
                if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", source[key]):
                    raise RuntimeError("Unsupported restore source coordinate")
            path = source["ODOO_UPSTREAM_FILESTORE_PATH"]
            if not path.startswith("/") or ".." in Path(path).parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
                raise RuntimeError("Unsupported restore source filestore")
            destination = source["ODOO_UPSTREAM_USER"] + "@" + source["ODOO_UPSTREAM_HOST"]
            ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
            for key in ("ssh_key_path", "ssh_directory"):
                if spec[key] and (not spec[key].startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]+", spec[key]) or ".." in Path(spec[key]).parts):
                    raise RuntimeError("Unsupported existing host SSH location")
            ssh_directory = Path(spec["ssh_directory"] or "/home/ubuntu/.ssh")
            key_path = spec["ssh_key_path"]
            for mount_prefix in ("/root/.ssh/", "/home/ubuntu/.ssh/"):
                if key_path.startswith(mount_prefix):
                    key_path = str(ssh_directory / Path(key_path).name)
                    break
            if not key_path:
                key_path = next((str(ssh_directory / name) for name in ("id_ed25519", "id_ecdsa", "id_rsa", "id_dsa") if (ssh_directory / name).is_file()), "")
            if not key_path:
                raise RuntimeError("Existing supported restore identity is unavailable")
            ssh.extend(["-i", key_path, "-o", "IdentitiesOnly=yes", "-o", "UserKnownHostsFile=" + str(ssh_directory / "known_hosts")])
            # The host's existing identity is used only by these two fixed source reads.
            # No key, agent socket or SSH directory enters an application container.
            remote = shlex.join(["sudo", "-u", source["ODOO_UPSTREAM_DB_USER"], "pg_dump", "-Fc", "--", source["ODOO_UPSTREAM_DB_NAME"]])
            staging = Path(tempfile.mkdtemp(prefix=namespace + "-copy-"))
            dump = staging / "database.dump"
            with dump.open("xb") as output:
                command([*ssh, destination, remote], stdout=output)
            filestore = staging / "filestore"
            filestore.mkdir()
            command(["rsync", "-a", "--safe-links", "--no-devices", "--no-specials", "--protect-args",
                "-e", shlex.join(ssh), destination + ":" + path.rstrip("/") + "/", str(filestore) + "/"])
            docker("exec", "-u", "root", runner, "mkdir", "-p", "/volumes/data/filestore/" + database)
            docker("cp", str(filestore) + "/.", runner + ":/volumes/data/filestore/" + database)
            docker("cp", str(dump), runner + ":" + dump_path)
            docker("exec", "-u", "root", runner, "chmod", "644", dump_path)
        uid = docker("exec", runner, "id", "-u").decode().strip()
        gid = docker("exec", runner, "id", "-g").decode().strip()
        if not uid.isdigit() or not gid.isdigit():
            raise RuntimeError("Testing workflow identity is unavailable")
        docker("exec", "-u", "root", runner, "chown", "-R", uid + ":" + gid, "/volumes/data", "/volumes/logs")
        if not resume:
            docker("exec", "-u", "root", runner, "chown", uid + ":" + gid, dump_path)
        workflow = spec["workflow_environment"]
        environment = {**{key: value for key, value in workflow.items() if not key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_"))}, "ODOO_FILESTORE_PATH": "/volumes/data/filestore", "ODOO_FILESTORE_OWNER": uid + ":" + gid, "ENV_OVERRIDE_DISABLE_CRON": "true", "ODOO_RESTORE_KEPT_INTEGRATIONS": ""}
        arguments = ["exec", "-i"]
        for key, value in environment.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise RuntimeError("Invalid workflow environment key")
            arguments.extend(["-e", key + "=" + value])
        output = docker(*arguments, runner, "python3", "-", dump_path, database, spec["readback_spec"],
            base64.b64encode(spec["readback_program"].encode()).decode(), "resume" if resume else "fresh",
            spec["copy_binding_sha256"], stdin=spec["runner"].encode()).decode()
        markers = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if any(markers.get(key) != "true" for key in ("testing_copy_sanitized", "testing_copy_pre_registry_sanitized", "odoo_module_update_completed", "odoo_module_update_modules_configured")):
            raise RuntimeError("Testing copy lacks sanitization or module update proof")
        docker(*arguments, runner, "python3", "-", database, gen["instance"],
            stdin=spec["website_settings_program"].encode())
        readback = docker("exec", "-i", runner, "python3", "-", database, spec["readback_spec"], stdin=spec["readback_program"].encode()).decode()
        if "integration_readback_ok=true" not in readback.splitlines():
            raise RuntimeError("Testing copy integration readback did not pass")
        # Emit only the established value-free protocol markers, never runner logs.
        for line in output.splitlines():
            if re.fullmatch(r"(?:website_bootstrap_[a-z_]+|odoo_[a-z_]+)=(?:true|false|[0-9]+)", line):
                print(line)
        marker = spec["web_gate_path"]
        docker("exec", runner, "python3", "-c",
            "import hashlib,os;from pathlib import Path;Path(" + repr(marker) + ").write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+'\\n'+os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')).encode()).hexdigest())")
        docker("exec", "-u", "root", runner, "rm", "-f", dump_path)
        if spec.get('native_capture') is not None:
            docker('exec','-i','-u','root',runner,'python3','-',base64.b64encode(json.dumps({'capture':spec['native_capture'],'mode':'cleanup'}).encode()).decode(),stdin=spec['native_files_program'].encode())
        docker("start", web)
        # Disposal is reconciled by the service against fresh use/custody facts,
        # independently of the new copy's health and its long-running admission.
        for marker in ("testing_copy_pre_registry_sanitized", "testing_copy_sanitized", "integration_readback_ok", "odoo_module_update_image_match", "odoo_restore_completed"):
            print(marker + "=true")
    finally:
        try:
            docker("exec", "-u", "root", runner, "rm", "-f", dump_path)
        finally:
            if staging is not None:
                shutil.rmtree(staging)


def main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    namespace = spec["generation"]["namespace"]
    if not re.fullmatch(r"lp-testing-[a-f0-9]{32}", namespace):
        raise RuntimeError("Invalid testing host lock identity")
    if spec.get('inspection_only'):
        restore_main()
        return
    path = str(Path(tempfile.gettempdir()) / (namespace + "-copy.lock"))
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore_main()
    finally:
        os.close(descriptor)


__LP_STORAGE_MAINTENANCE_LIBRARY__

try:
    main()
except PermissionError:
    if len(sys.argv)>2:
        print(sys.argv[2]+json.dumps({'type':'failed'}),flush=True)
        sys.exit(1)
    print('odoo_restore_completed=false')
    print('testing_copy_sanitized=false')
    sys.exit(1)
except Exception:
    if len(sys.argv)>2:
        print(sys.argv[2]+json.dumps({'type':'failed'}),flush=True)
        sys.exit(1)
    print("odoo_restore_completed=false")
    print("testing_copy_sanitized=false")
    # Failed copies and their stopped web remain for exact-generation recovery.
    sys.exit(1)
"""


FRESH_TESTING_RESTORE_HOST_PROGRAM = FRESH_TESTING_RESTORE_HOST_PROGRAM.replace(
    "__LP_COPY_WITNESS__", TESTING_COPY_COMPLETION_PARAMETER
)


FRESH_TESTING_RESTORE_HOST_PROGRAM = FRESH_TESTING_RESTORE_HOST_PROGRAM.replace(
    "__LP_STORAGE_MAINTENANCE_LIBRARY__", STORAGE_MAINTENANCE_LIBRARY
)


def read_current_testing_generation(
    store: object,
    *,
    product: str,
    context: str,
    instance: str,
    bootstrap_preparation: bool = False,
    task_account_cleanup: bool = False,
) -> FreshOdooTestingGeneration | None:
    if instance != "testing":
        return None
    read = getattr(store, "read_fresh_odoo_testing_generation", None)
    if not callable(read):
        return None
    generation = read(product, context)
    if generation is None:
        return None
    if not isinstance(generation, FreshOdooTestingGeneration):
        raise ValueError("Fresh testing generation is unreadable.")
    if not bootstrap_preparation and not generation.reconcile_requested_at:
        raise ValueError("Fresh testing bootstrap has not committed its queue receipt.")
    target_id = getattr(store, "read_dokploy_target_id_record")(
        context_name=context, instance_name="testing"
    )
    if target_id.target_id != generation.target_id:
        raise ValueError("Fresh testing generation no longer owns the target.")
    profile = getattr(store, "read_product_profile_record")(product)
    if (
        not isinstance(profile, LaunchplaneProductProfileRecord)
        or profile.driver_id != "odoo"
        or not profile.is_active
    ):
        raise ValueError("Fresh testing generation no longer owns an active Odoo product.")
    require_distinct_testing_credentials(store, profile, context)
    allocation = getattr(store, "read_disposable_resource_preparation")(generation.operation_id)
    if (
        allocation is None
        or allocation.replacement_target_id != generation.target_id
        or allocation.resources_binding_sha256 != generation.resources_binding_sha256
        or allocation.resource_namespace != generation.namespace
    ):
        raise ValueError("Fresh testing generation lost its resource allocation.")
    target = getattr(store, "read_dokploy_target_record")(
        context_name=context, instance_name="testing"
    )
    if (
        target.policies.staff_testing_hold is not None and not task_account_cleanup
    ) or target.pending_public_host_effects:
        raise ValueError("Fresh testing generation has a use or route hold.")
    values: dict[str, object] = {}
    for scope in ("context", "instance"):
        for runtime in getattr(store, "list_runtime_environment_records")(context_name=context):
            if runtime.scope == scope and runtime.instance == (
                "testing" if scope == "instance" else ""
            ):
                values.update(runtime.env)
    values.update(target.env)
    from control_plane.odoo_testing_resources import OdooTestingResources

    resources = OdooTestingResources(
        product=product,
        context=context,
        operation_id=generation.operation_id,
        target_id=generation.target_id,
        namespace=generation.namespace,
    )
    if any(values.get(key) != value for key, value in resources.volumes.items()):
        raise ValueError("Fresh testing volume authority changed before deployment.")
    if (
        values.get("ODOO_PROJECT_NAME") != generation.namespace
        or values.get("ODOO_STACK_NAME") != generation.namespace
        or (generation.drill_database and values.get("ODOO_DB_NAME") != generation.drill_database)
    ):
        raise ValueError("Fresh testing project authority changed before deployment.")
    for other in getattr(store, "list_dokploy_target_id_records")():
        if other.target_id == generation.target_id and (other.context, other.instance) != (
            context,
            "testing",
        ):
            raise ValueError("Fresh testing target aliases another lane.")
    for other in getattr(store, "list_physical_provider_target_records")():
        if (
            other.provider_id == "dokploy"
            and other.target_id == generation.target_id
            and (other.context, other.instance) != (context, "testing")
        ):
            raise ValueError("Fresh testing provider target aliases another lane.")
    from control_plane.contracts.product_profile_record import (
        is_exclusive_product_context,
        product_context_owner_map,
    )

    if not is_exclusive_product_context(
        context=context,
        product=product,
        owners=product_context_owner_map(getattr(store, "list_product_profile_records")()),
    ):
        raise ValueError("Fresh testing context ownership changed.")
    return generation


FRESH_TESTING_LOCAL_CREDENTIAL_KEYS = frozenset(
    {"ODOO_ADMIN_PASSWORD", "ODOO_DB_PASSWORD", "ODOO_MASTER_PASSWORD"}
)
FRESH_TESTING_OMITTED_SETTINGS = frozenset(
    {
        "ODOO_WEB_COMMAND",
        "ODOO_KEY",
        "ODOO_SMTP_SERVER",
        "ODOO_SMTP_HOST",
        "ODOO_SMTP_PORT",
        "ODOO_SMTP_USER",
        "ODOO_SMTP_PASSWORD",
        "ODOO_SMTP_SSL",
    }
)


def narrow_fresh_testing_requirements(
    profile: LaunchplaneProductProfileRecord, *, context: str, omitted_keys: frozenset[str]
) -> LaunchplaneProductProfileRecord:
    """Keep existing models; scope shared requirements to their other stable lanes.

    This changes only the testing lane's effective required keys. Production's
    existing requirements, owner-input descriptions and secret bindings remain.
    """
    testing = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "testing")
    )

    def narrow(requirements: tuple[Requirement, ...], *, secret: bool) -> tuple[Requirement, ...]:
        retained = []
        generated = []
        for requirement in requirements:
            key = str(getattr(requirement, "binding_key" if secret else "key"))
            if key not in omitted_keys or not product_config_requirement_applies_to_lane(
                requirement_context=requirement.context,
                requirement_instance=requirement.instance,
                lane=testing,
            ):
                retained.append(requirement)
                continue
            for lane in profile.lanes:
                if lane == testing or not product_config_requirement_applies_to_lane(
                    requirement_context=requirement.context,
                    requirement_instance=requirement.instance,
                    lane=lane,
                ):
                    continue
                generated.append(
                    requirement.model_copy(
                        update={"context": lane.context, "instance": lane.instance}
                    )
                )

        def identity(requirement: Requirement) -> tuple[str, str, str, str]:
            return (
                str(getattr(requirement, "integration", "")),
                str(getattr(requirement, "binding_key" if secret else "key")),
                requirement.context,
                requirement.instance,
            )

        result = {identity(requirement): requirement for requirement in retained}
        for requirement in generated:
            result.setdefault(identity(requirement), requirement)
        return tuple(result.values())

    expected = profile.expected_config.model_copy(
        update={
            "runtime_environment_keys": narrow(
                profile.expected_config.runtime_environment_keys, secret=False
            ),
            "managed_secret_bindings": narrow(
                profile.expected_config.managed_secret_bindings, secret=True
            ),
        }
    )
    result = profile.model_copy(update={"expected_config": expected})
    return LaunchplaneProductProfileRecord.model_validate(
        result.model_dump(mode="json")
    ).validate_write_contract()


class TestingCredentialVersionsMissing(ValueError):
    """The service cannot compare both sets of existing managed versions."""


class TestingCredentialsOverlap(ValueError):
    """A testing credential equals one of the production source's values."""


def require_distinct_testing_credentials(
    store: object, profile: LaunchplaneProductProfileRecord, context: str
) -> None:
    """Opaque source preflight inside the service; no value or hash is transported.

    Use existing managed versions, not secret names/version IDs as value proof.
    This reads control-plane source authority and never writes a secret, binding,
    production target or credential. The supported copy runner separately strips
    credentials from the restored database before registry code executes.
    """
    import hmac
    from typing import cast
    from control_plane import secrets

    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(production) != 1:
        raise ValueError("A unique production source is required for credential comparison.")
    source = production[0]
    reader = cast(secrets.SecretReadStore, store)
    testing_values = secrets.resolve_secret_values_for_integration_from_store(
        record_store=reader,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context,
        instance_name="testing",
        scopes=frozenset({"context_instance"}),
    )
    source_values = secrets.resolve_secret_values_for_integration_from_store(
        record_store=reader,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=source.context,
        instance_name="prod",
        scopes=frozenset({"context", "context_instance"}),
    )
    if any(not testing_values.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS) or any(
        not source_values.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
    ):
        raise TestingCredentialVersionsMissing(
            "Credential comparison needs the existing testing and production source versions."
        )
    if any(
        hmac.compare_digest(testing_values[key].encode(), value.encode())
        for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
        for value in source_values.values()
        if value
    ):
        raise TestingCredentialsOverlap(
            "Existing testing credentials overlap the supported production source."
        )


def fresh_testing_authority_sha256(
    store: object,
    generation: FreshOdooTestingGeneration,
    *,
    include_override_results: bool = False,
) -> str:
    """Bind rendered values to recipient and supported-source version metadata."""
    from typing import cast
    from control_plane import secrets
    from control_plane.disposable_lanes import DisposableLaneStore, disposable_lane_configuration
    from control_plane.contracts.canonical_json import canonical_json_sha256

    profile = getattr(store, "read_product_profile_record")(generation.product)
    if generation.source_mode == "container-export":
        from control_plane.odoo_testing_native_source import (
            native_testing_source_authority,
            recorded_native_testing_capture,
        )
        from control_plane.storage.postgres import PostgresRecordStore

        if recorded_native_testing_capture(store, generation) is None and (
            native_testing_source_authority(
                cast(PostgresRecordStore, store),
                profile=profile,
                testing_context=generation.context,
            ).binding_sha256
            != generation.source_binding_sha256
        ):
            raise ValueError("The reviewed native source authority changed.")
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(production) != 1:
        raise ValueError("Fresh testing source authority is unavailable.")
    metadata = list(
        disposable_lane_configuration(
            cast(DisposableLaneStore, store), generation.context, "testing"
        )
    )
    selected = secrets.resolve_effective_secret_bindings_from_store(
        record_store=cast(secrets.SecretReadStore, store),
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=production[0].context,
        instance_name="prod",
        scopes=frozenset({"context", "context_instance"}),
    )
    for binding, record in selected.values():
        metadata.extend((binding, record))
    payloads = [item.model_dump(mode="json") for item in metadata]
    secret_ids = {
        getattr(item, "secret_id") for item in metadata if hasattr(item, "current_version_id")
    }
    for secret_id in secret_ids:
        record = getattr(store, "read_secret_record")(secret_id)
        version = getattr(store, "read_secret_version")(record.current_version_id)
        if version.secret_id != record.secret_id:
            raise ValueError("Fresh testing secret version provenance is inconsistent.")
        payloads.append(version.model_dump(mode="json"))
    override = None
    try:
        override = getattr(store, "read_odoo_instance_override_record")(
            context_name=generation.context, instance_name="testing"
        )
    except FileNotFoundError:
        pass
    return canonical_json_sha256(
        {
            "profile": profile.model_dump(mode="json"),
            "generation": generation_identity_payload(generation),
            "target": getattr(store, "read_dokploy_target_record")(
                context_name=generation.context, instance_name="testing"
            ).model_dump(mode="json"),
            "target_id": generation.target_id,
            "configuration": sorted(payloads, key=lambda item: json.dumps(item, sort_keys=True)),
            # Apply results are observations written by this very execution. Bind
            # every intent field, but do not make publishing a result look like
            # a new restore intent. Legacy journals retain their exact digest;
            # an absent preimage is never permission to rebind a held operation.
            "overrides": override.model_dump(
                mode="json",
                exclude={}
                if include_override_results
                else {
                    "last_apply": {"attempted", "status", "applied_at", "detail"},
                    "updated_at": True,
                    "source_label": True,
                },
            )
            if override is not None
            else None,
        }
    )
