"""What each production backup gate failure code means, in Launchplane's words.

A backup operation's read shows its code and this description, never the
provider's or the script's text, which can name hosts, storages and guests.
"""

BACKUP_FAILED_CODE = "backup_failed"

_STAGE_NAMES: dict[str, str] = {
    "preflight": "the preflight check",
    "snapshot": "the guest snapshot",
    "independent_backup": "the independent backup",
    "snapshot_retention": "snapshot retention",
}

BACKUP_FAILURE_DESCRIPTIONS: dict[str, str] = {
    BACKUP_FAILED_CODE: "The backup failed.",
    "backup_preflight_unavailable": (
        "The backup stopped before its provider steps: its policy, targets or credentials "
        "could not be prepared."
    ),
    "backup_authority_changed": "The lane's backup policy or targets changed while it ran.",
    "backup_authority_unavailable": "Launchplane could not read the lane's backup policy.",
    "backup_boundary_invalid": "The backup's provider boundary failed validation.",
    "backup_endpoint_invalid": "The backup target's endpoint failed validation.",
    "backup_host_binding_mismatch": (
        "The backup host does not match the host the lane's backup target names."
    ),
    "backup_lease_lost": "The backup worker lost its lease before it finished.",
    "backup_effect_outcome_unknown": (
        "The backup worker was interrupted after a provider step started. Its outcome is "
        "unknown, so Launchplane stopped the release and will not retry this capture automatically."
    ),
    "backup_progress_unavailable": (
        "Launchplane could not save the backup's progress. The capture stopped; any provider "
        "effect already started requires reconciliation before another attempt."
    ),
    "backup_operation_store_unavailable": "Launchplane could not record the backup's progress.",
    "backup_source_busy": "Another backup of the same source was running.",
    "backup_source_lock_lost": "The backup lost its lock on the source while it ran.",
    "backup_ssh_material_missing": "The backup's SSH credentials are not stored.",
    "backup_ssh_memory_files_unavailable": (
        "The backup worker could not hold its SSH credentials in memory."
    ),
    "backup_ssh_memory_write_failed": (
        "The backup worker could not hold its SSH credentials in memory."
    ),
    "backup_storage_not_active_pbs": (
        "The backup destination is not an active Proxmox Backup Server storage."
    ),
    "backup_timeout": "The backup did not finish within its time limit.",
    "independent_backup_identity_missing": "The independent backup returned no archive id.",
    "independent_backup_not_found": (
        "The independent backup's archive was not found in the destination storage."
    ),
    "snapshot_name_too_long": "The snapshot name exceeds the provider's limit.",
    "snapshot_not_found": "The guest snapshot was not found after it was taken.",
    "snapshot_prefix_invalid": "The lane's snapshot name prefix failed validation.",
    "operation_authorization_administrator_revoked": (
        "The administrator who approved the backup no longer holds that authority."
    ),
    "operation_authorization_policy_unavailable": (
        "The authorization policy could not be read when the backup ran."
    ),
    "operation_authorization_provenance_missing": (
        "The backup had no recorded authorization and could not run."
    ),
    "operation_authorization_reconcile_refused": (
        "Launchplane's reconcile grant did not cover this backup when it ran."
    ),
    "operation_authorization_revoked": (
        "The backup's authorization was removed or narrowed before it ran."
    ),
}
for _stage, _name in _STAGE_NAMES.items():
    BACKUP_FAILURE_DESCRIPTIONS[f"{_stage}_command_failed"] = (
        f"A provider command failed during {_name}."
    )
    BACKUP_FAILURE_DESCRIPTIONS[f"{_stage}_unavailable"] = (
        f"The provider could not be reached during {_name}."
    )

_UNKNOWN_BACKUP_FAILURE = "The backup failed with a code this Launchplane does not describe."


def backup_failure_description(code: str) -> str:
    """The fixed description of a backup failure ``code``; "" for no code."""
    if not code:
        return ""
    return BACKUP_FAILURE_DESCRIPTIONS.get(code, _UNKNOWN_BACKUP_FAILURE)


def known_backup_failure_code(code: str) -> str:
    """``code`` when Launchplane describes it, otherwise the generic failure code."""
    return code if code in BACKUP_FAILURE_DESCRIPTIONS else BACKUP_FAILED_CODE
