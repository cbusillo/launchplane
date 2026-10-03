"""Fixed reasons for record failures; inputs never contain provider or exception text."""

from control_plane.contracts.promotion_record import RecordFailure

RECORD_FAILURE_DESCRIPTIONS: dict[str, str] = {
    "bootstrap_failed": "The stable bootstrap did not complete.",
    "bootstrap_post_deploy_failed": "The post-deploy update after stable bootstrap failed.",
    "bootstrap_verification_failed": "The stable bootstrap readiness checks did not pass.",
    "restore_failed": "The production backup restore did not complete.",
    "restore_post_deploy_failed": "The post-deploy update after production backup restore failed.",
    "restore_verification_failed": "The production backup restore verification did not pass.",
    "restore_runtime_identity_failed": "The restored runtime identity did not match.",
    "preview_plan_blocked": "The preview plan was blocked.",
    "preview_config_refused": "The preview runtime settings are incomplete or invalid.",
    "preview_provenance_refused": "The preview plan provenance was refused.",
    "preview_apply_failed": "The preview provider operation failed.",
    "preview_build_failed": "The preview of this build failed; Launchplane tries again when the PR has a new build (a push, or a re-run of its Build workflow).",
    "preview_destination_refused": "Launchplane's reconcile may not change this preview destination.",
    "preview_credentials_unavailable": "Launchplane could not resolve the preview's GitHub App credentials.",
    "preview_lease_lost": "This worker no longer holds the preview reconcile lease; nothing more was changed.",
    "preview_slug_unavailable": "Launchplane could not resolve the preview slug.",
    "preview_reconcile_failed": "Launchplane could not reconcile the preview.",
    "reconcile_required": "The provider outcome is unknown; reconciliation is required before retrying.",
    "rollback_not_needed": "Production was not changed, so no rollback was needed.",
    "rollback_unavailable": "No automatic rollback target was available.",
    "rollback_failed": "The rollback did not complete.",
    "rollback_deploy_failed": "The rollback deployment failed.",
    "rollback_health_failed": "Production was rolled back, but its health check did not pass.",
    "rollback_passed": "Production was rolled back to the previous deployment.",
}


def record_failure(code: str) -> RecordFailure:
    return RecordFailure(code=code, description=RECORD_FAILURE_DESCRIPTIONS[code])


def record_failure_summary(code: str) -> str:
    failure = record_failure(code)
    return f"{failure.code}: {failure.description}"
