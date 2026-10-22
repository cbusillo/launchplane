from collections.abc import Mapping
from enum import StrEnum


class ReadbackPredicate(StrEnum):
    SCHEDULE_EVIDENCE = "schedule_evidence_complete"
    SERVICE_RESPONSE = "service_observation_complete"
    SCHEDULE_TERMINAL = "schedule_terminal_observed"
    SCHEDULE_TRIGGER = "schedule_trigger_accepted"
    SCHEDULE_BASELINE = "schedule_baseline_readable"
    NATIVE_READ = "native_inspection_complete"
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
    COPY_WITNESS = "copy_generation_witness_matches"
    RUNTIME = "copy_runtime_running"
    IMAGE_DIGEST = "copy_image_digest_matches"
    RUNTIME_IDENTITY = "copy_runtime_identity_matches"
    WEB_GATE = "copy_web_gate_matches"
    CLEANUP_DATABASE = "cleanup_database_present"
    USAGE_DATABASE = "copy_usage_database_valid"
    USAGE_CUTOFF = "copy_usage_cutoff_valid"
    USAGE = "copy_usage_readable"


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
        for marker, failed in (
            ("connection_isolation_resources_ok", ReadbackPredicate.RESOURCES),
            ("connection_isolation_ok", ReadbackPredicate.ISOLATION),
            ("integration_readback_ok", ReadbackPredicate.INTEGRATIONS),
        ):
            if evidence.get(marker) == "false":
                predicate = failed
                kind = "unavailable" if failed is ReadbackPredicate.RESOURCES else "refused"
                break
    if (
        predicate == ReadbackPredicate.ISOLATION
        and evidence.get("connection_isolation_ok") == "false"
        or predicate == ReadbackPredicate.INTEGRATIONS
        and evidence.get("integration_readback_ok") == "false"
    ):
        kind = "refused"
    resource_refusals = evidence.get("integration_readback_refused", "").split(",")
    if len(resource_refusals) == 1 and resource_refusals[0].startswith("testing_resources/"):
        reason = resource_refusals[0].removeprefix("testing_resources/")
        predicate = {
            "workflow_active": ReadbackPredicate.EXECUTION_LOCK,
            "runner_exec_active": ReadbackPredicate.RUNNER_IDLE,
            "review_binding": ReadbackPredicate.REVIEW_BINDING,
        }.get(reason, ReadbackPredicate.RESOURCES)
        kind = (
            "unavailable"
            if reason
            in {
                "inventory_unverified",
                "inventory_shape",
                "inventory_limit",
                "qualification_unverified",
            }
            else "refused"
        )
    return {
        **evidence,
        "readback_failed_predicate": predicate,
        "readback_failure_kind": kind if kind in {"refused", "unavailable"} else "unavailable",
    }
