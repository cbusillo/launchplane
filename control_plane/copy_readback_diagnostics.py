from collections.abc import Mapping
from enum import StrEnum
import inspect
import re


NATIVE_PROGRAM_SOURCE_SITE_LIMIT = 8


class CopyFailureStage(StrEnum):
    HOST_PREPARATION = "host_preparation"
    SOURCE_TRANSFER = "source_transfer"
    WEBSITE_SETTINGS = "website_settings"
    INTEGRATION_READBACK = "integration_readback"
    SNAPSHOT = "snapshot"
    RUNTIME_START = "runtime_start"
    CLEANUP = "cleanup"
    RESTORE = "restore"
    ATTACHMENTS = "attachments"
    PRE_REGISTRY_SANITIZE = "pre_registry_sanitize"
    MODULE_UPDATE = "module_update"
    CORE_SCHEMA = "core_schema"
    USERS = "users"
    POST_SANITIZE = "post_sanitize"


COPY_FAILURE_STAGE_MARKER = "copy_failure_stage"
COPY_FAILURE_KIND_MARKER = "copy_failure_kind"
COPY_FAILURE_EXIT_MARKER = "copy_failure_exit_status"
COPY_FAILURE_EVIDENCE_MARKER = "copy_failure_evidence_id"
COPY_FAILURE_OUTPUT_BYTES = 1024 * 1024
COPY_FAILURE_STATE_DIRECTORY = ".local/state/launchplane-host/copy-failures"


def copy_failure_evidence_id(value: object) -> str:
    return (
        value
        if isinstance(value, str)
        and (value == "unavailable" or re.fullmatch(r"[a-f0-9]{32}", value) is not None)
        else ""
    )


def copy_failure_marker_is_safe(key: str, value: str) -> bool:
    if key == COPY_FAILURE_KIND_MARKER:
        return value in {"command_failed", "refused", "unavailable"}
    if key == COPY_FAILURE_STAGE_MARKER:
        return value in CopyFailureStage
    if key == COPY_FAILURE_EVIDENCE_MARKER:
        return bool(copy_failure_evidence_id(value))
    return (
        key == COPY_FAILURE_EXIT_MARKER
        and re.fullmatch(r"-?[0-9]{1,3}", value) is not None
        and -255 <= int(value) <= 255
        and int(value) != 0
    )


def safe_copy_failure_markers(values: object) -> dict[str, str]:
    if not isinstance(values, Mapping):
        return {}
    return {
        key: value
        for key, value in values.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and copy_failure_marker_is_safe(key, value)
    }


def copy_failure_receipt(values: object) -> str:
    return "".join(
        f":{key}={value}" for key, value in sorted(safe_copy_failure_markers(values).items())
    )


def copy_failure_summary(receipt: str) -> str:
    markers: dict[str, str] = {}
    for part in receipt.split(":"):
        key, _, value = part.partition("=")
        if copy_failure_marker_is_safe(key, value):
            markers.setdefault(key, value)
    return " ".join(f"{key}={value}" for key, value in markers.items())


def copy_failure_diagnostics_library() -> str:
    return (
        "import re\n"
        + inspect.getsource(copy_failure_evidence_id)
        + "\nclass CopyFailureStage:\n"
        + "\n".join(f"    {stage.name} = {stage.value!r}" for stage in CopyFailureStage)
        + f"\nCOPY_FAILURE_STAGES = {tuple(stage.value for stage in CopyFailureStage)!r}\n"
        + f"COPY_FAILURE_STAGE_MARKER = {COPY_FAILURE_STAGE_MARKER!r}\n"
        + f"COPY_FAILURE_KIND_MARKER = {COPY_FAILURE_KIND_MARKER!r}\n"
        + f"COPY_FAILURE_EXIT_MARKER = {COPY_FAILURE_EXIT_MARKER!r}\n"
        + f"COPY_FAILURE_EVIDENCE_MARKER = {COPY_FAILURE_EVIDENCE_MARKER!r}\n"
        + f"COPY_FAILURE_OUTPUT_BYTES = {COPY_FAILURE_OUTPUT_BYTES!r}\n"
        + f"COPY_FAILURE_STATE_DIRECTORY = {COPY_FAILURE_STATE_DIRECTORY!r}\n"
        + """
def copy_failure_mount_is_private(source: object) -> bool:
    from pathlib import Path
    if not isinstance(source, str) or not Path(source).is_absolute():
        return False
    private = Path.home().resolve(strict=True) / COPY_FAILURE_STATE_DIRECTORY
    path = Path(source).resolve(strict=False)
    return not (private.is_relative_to(path) or path.is_relative_to(private))


def copy_failure_storage_is_private(containers: object) -> bool:
    if not isinstance(containers, list):
        return False
    try:
        for container in containers:
            mounts = container.get('Mounts') if isinstance(container, dict) else None
            if not isinstance(mounts, list) or any(
                not isinstance(mount, dict) or mount.get('Type') not in {'tmpfs', 'image'} and not copy_failure_mount_is_private(mount.get('Source'))
                for mount in mounts):
                return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def retain_copy_command_failure(result, stage, spec):
    import base64
    from contextlib import suppress
    from datetime import datetime, timezone
    import hashlib
    import json
    import os
    from pathlib import Path
    import uuid
    evidence_path = None
    try:
        directory = Path.home().resolve(strict=True) / COPY_FAILURE_STATE_DIRECTORY
        root = directory.parent
        if directory.resolve() != directory.absolute():
            return 'unavailable'
        for path in (root, directory):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = path.stat()
            if metadata.st_uid != os.geteuid() or metadata.st_mode & (0o022 if path == root else 0o077):
                return 'unavailable'
        identity = uuid.uuid4().hex
        evidence = {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'input_sha256': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            'stage': stage,
            'exit_status': result.returncode,
        }
        for stream in ('stdout', 'stderr'):
            output = getattr(result, stream) or b''
            evidence[stream] = base64.b64encode(output[-COPY_FAILURE_OUTPUT_BYTES:]).decode()
            pretruncated = getattr(result, stream + '_capture_truncated', False)
            evidence[stream + '_bytes'] = None if pretruncated else len(output)
            evidence[stream + '_truncated'] = pretruncated or len(output) > COPY_FAILURE_OUTPUT_BYTES
        path = directory / (identity + '.json')
        descriptor = os.open(path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        evidence_path = path
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(evidence, handle)
        return identity
    except OSError:
        if evidence_path is not None:
            with suppress(OSError):
                evidence_path.unlink()
        return 'unavailable'


copy_failure_stage = CopyFailureStage.HOST_PREPARATION
copy_failure_containers = globals().get('copy_failure_containers')


def copy_failure_cause(error: Exception) -> Exception:
    chain = [error]
    while not chain[-1].__suppress_context__ and chain[-1].__context__ is not None:
        chain.append(chain[-1].__context__)
    error = next((cause for cause in reversed(chain) if isinstance(cause, CopyReadbackRefused)), chain[-1])
    command = next((cause for cause in reversed(chain[chain.index(error):])
        if hasattr(cause, 'copy_command_result') or hasattr(cause, 'copy_failure_markers')), error)
    result = getattr(command, 'copy_command_result', None)
    if result is not None:
        stage = command.copy_command_stage
        identity = retain_copy_command_failure(result, stage, command.copy_command_spec) if command.copy_command_private else 'unavailable'
        command.copy_failure_markers = {COPY_FAILURE_STAGE_MARKER: stage,
            COPY_FAILURE_KIND_MARKER: 'command_failed', COPY_FAILURE_EXIT_MARKER: str(result.returncode),
            COPY_FAILURE_EVIDENCE_MARKER: identity}
        del command.copy_command_result, command.copy_command_spec
    if hasattr(command, 'copy_failure_markers'):
        error.copy_failure_markers = command.copy_failure_markers
        setattr(error, COPY_FAILURE_EVIDENCE_MARKER, command.copy_failure_markers[COPY_FAILURE_EVIDENCE_MARKER])
    return error


def copy_failure_frame_markers(error: Exception) -> dict[str, str]:
    return getattr(copy_failure_cause(error), 'copy_failure_markers', {})


def emit_copy_failure(error: Exception) -> None:
    error = copy_failure_cause(error)
    markers = getattr(error, 'copy_failure_markers', None)
    if markers is None:
        markers = {COPY_FAILURE_STAGE_MARKER: copy_failure_stage,
            COPY_FAILURE_KIND_MARKER: 'refused' if isinstance(error, CopyReadbackRefused) else 'unavailable'}
    for key, value in markers.items():
        print(key + '=' + value, flush=True)


def copy_command_failure(error: Exception, result: object, spec: dict[str, object], containers: object = None) -> Exception:
    stage = copy_failure_stage
    for line in (result.stdout or b'').decode(errors='replace').splitlines():
        key, separator, value = line.partition('=')
        if separator and key == COPY_FAILURE_STAGE_MARKER and value in COPY_FAILURE_STAGES:
            stage = value
            break
    error.copy_command_result = result
    error.copy_command_spec = spec
    error.copy_command_stage = stage
    error.copy_command_private = copy_failure_storage_is_private(copy_failure_containers if containers is None else containers)
    return error
"""
    )


class ReadbackPredicate(StrEnum):
    SCHEDULE_EVIDENCE = "schedule_evidence_complete"
    SERVICE_RESPONSE = "service_observation_complete"
    SCHEDULE_TERMINAL = "schedule_terminal_observed"
    SCHEDULE_TRIGGER = "schedule_trigger_accepted"
    SCHEDULE_BASELINE = "schedule_baseline_readable"
    NATIVE_READ = "native_inspection_complete"
    FIXTURE_COMMAND = "fixture_command_complete"
    FIXTURE_RECEIPT = "fixture_receipt_valid"
    RUNNER_SELECTED = "runner_selected"
    WEB_SELECTED = "web_selected"
    RUNNER_RUNNING = "runner_running"
    RUNNER_IDLE = "runner_idle"
    EXECUTION_LOCK = "execution_lock_available"
    REVIEW_BINDING = "reviewed_resources_match"
    RESOURCES = "resources_qualified"
    ISOLATION = "connection_isolated"
    INTEGRATIONS = "integrations_isolated"
    WEB_RESTORED = "web_restored"
    WEB_STOPPED = "web_stopped_for_repair"
    GATE_REMOVED = "web_gate_removed_for_repair"
    BACKING_STORAGE = "backing_storage_valid"
    BACKING_DISTINCT = "backing_storage_distinct"
    STORAGE_CONSUMERS = "storage_consumers_exclusive"
    INSTANCE = "copy_instance_valid"
    NAMESPACE = "copy_namespace_valid"
    SERVICES = "copy_services_exact"
    CUSTODY = "copy_custody_matches"
    CANDIDATE_READINESS = "candidate_private_readiness_complete"
    DATABASE_PRESENT = "copy_database_present"
    PRIVATE_ENDPOINT = "copy_private_endpoint_exclusive"
    CAPABILITIES = "copy_host_capabilities_absent"
    PORTS = "copy_host_ports_absent"
    MOUNTS = "copy_mounts_exact"
    DATABASE_STORAGE = "database_storage_mounted"
    IMAGE = "copy_image_matches"
    DATABASE_ENDPOINT = "database_endpoint_local"
    SETTINGS = "copy_settings_match"
    CRON = "copy_cron_disabled"
    HOST_ALIASES = "copy_host_aliases_absent"
    CREDENTIALS = "copy_execution_credentials_absent"
    NETWORK = "copy_network_private"
    NETWORK_REACH = "copy_network_reach_exact"
    NETWORK_MEMBERS = "copy_network_members_exclusive"
    STORAGE = "copy_storage_exclusive"
    PRIVATE_STATE = "copy_private_state_unmounted"
    INPUT = "copy_inspection_input_valid"
    RUNNER_CAPABILITIES = "runner_capabilities_available"
    DATABASE_READ = "copy_database_readable"
    FILES_READ = "copy_native_files_readable"
    ATTACHMENTS = "copy_attachments_verified"
    COPY_WITNESS = "copy_generation_witness_matches"
    RUNTIME = "copy_runtime_running"
    IMAGE_DIGEST = "copy_image_digest_matches"
    RUNTIME_IDENTITY = "copy_runtime_identity_matches"
    WEB_GATE = "copy_web_gate_matches"
    CLEANUP_DATABASE = "cleanup_database_present"
    USAGE_DATABASE = "copy_usage_database_valid"
    USAGE_CUTOFF = "copy_usage_cutoff_valid"
    USAGE = "copy_usage_readable"


class CopyReadbackRefused(RuntimeError):
    def __init__(self, predicate: str) -> None:
        self.predicate = predicate
        super().__init__(predicate)


READBACK_FAILURE_MARKERS = frozenset({"readback_failed_predicate", "readback_failure_kind"})


def readback_predicate_library() -> str:
    return (
        "class ReadbackPredicate:\n"
        + "\n".join(f"    {item.name} = {item.value!r}" for item in ReadbackPredicate)
        + "\n"
    )


def readback_marker_is_safe(key: str, value: str) -> bool:
    if key == "readback_failed_predicate":
        return value in ReadbackPredicate
    return key == "readback_failure_kind" and value in {"refused", "unavailable"}


def readback_failure_evidence(evidence: Mapping[str, str]) -> dict[str, str]:
    predicate = evidence.get("readback_failed_predicate", "")
    kind = evidence.get("readback_failure_kind", "unavailable")
    if predicate not in ReadbackPredicate:
        predicate = ReadbackPredicate.SCHEDULE_EVIDENCE
        kind = "unavailable"
    if (
        predicate == ReadbackPredicate.ISOLATION
        and evidence.get("connection_isolation_ok") == "false"
        or predicate == ReadbackPredicate.INTEGRATIONS
        and evidence.get("integration_readback_ok") == "false"
    ):
        kind = "refused"
    return {
        **evidence,
        "readback_failed_predicate": predicate,
        "readback_failure_kind": kind if kind in {"refused", "unavailable"} else "unavailable",
    }
