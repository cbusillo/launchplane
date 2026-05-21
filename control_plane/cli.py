import base64
from collections.abc import Mapping
import hashlib
import json
import os
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, TypeVar, cast, overload
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import click
from control_plane import dokploy as control_plane_dokploy
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane import release_tuples as control_plane_release_tuples
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyResult
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import PreviewPullRequestSummary
from control_plane.contracts.preview_manifest import LaunchplaneResolvedPreviewManifest
from control_plane.contracts.preview_request_metadata import (
    LaunchplaneCompanionPullRequestReference,
    LaunchplanePreviewRequestMetadata,
    LaunchplanePreviewRequestParseResult,
)
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PromotionRecord,
    PromotionRequest,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.ship_request import ShipRequest
from control_plane.cli_dokploy_targets import (
    dokploy_target_route,
    register_dokploy_target_commands,
    summarize_dokploy_target_record,
    target_id_map,
)
from control_plane.cli_every_code import register_every_code_commands
from control_plane.cli_runner_lanes import register_runner_lane_commands
from control_plane.cli_launchplane_previews import (
    LaunchplanePreviewCliCallbacks,
    register_launchplane_preview_commands,
)
from control_plane.cli_odoo import OdooCliCallbacks, register_odoo_commands
from control_plane.cli_odoo import (
    _normalize_odoo_prod_rollback_source_channel as _normalize_odoo_prod_rollback_source_channel,
)
from control_plane.cli_preview_workflow import register_preview_workflow_commands
from control_plane.cli_policy_profiles import (
    PolicyProfileCliCallbacks,
    register_policy_profile_commands,
    summarize_product_profile_record,
)
from control_plane.cli_promotion_ship import (
    PromotionShipCliCallbacks,
    register_promotion_ship_commands,
)
from control_plane.cli_product_config import register_product_config_commands
from control_plane.cli_runtime_environments import (
    register_runtime_environment_commands,
    summarize_runtime_environment_record,
)
from control_plane.cli_records import RecordsCliCallbacks, register_record_commands
from control_plane.cli_service import ServiceCliCallbacks, register_service_commands
from control_plane.cli_storage_secrets import register_storage_secret_commands
from control_plane.cli_work_graph import register_work_graph_core_commands
from control_plane.launchplane_rendering import (
    build_launchplane_action_script as _build_launchplane_action_script,
    build_launchplane_backup_gate_write_recipe_script as _build_launchplane_backup_gate_write_recipe_script,
    build_launchplane_environment_ship_recipe_script as _build_launchplane_environment_ship_recipe_script,
    build_launchplane_promotion_execute_recipe_script as _build_launchplane_promotion_execute_recipe_script,
    build_launchplane_promotion_resolve_recipe_script as _build_launchplane_promotion_resolve_recipe_script,
    int_from_json_value as _int_from_json_value,
    json_object as _json_object,
    json_object_items as _json_object_items,
    launchplane_action_slug as _launchplane_action_slug,
    launchplane_environment_bundle_relative_path as _launchplane_environment_bundle_relative_path,
    launchplane_inventory_bucket as _launchplane_inventory_bucket,
    launchplane_preview_bundle_relative_path as _launchplane_preview_bundle_relative_path,
    launchplane_preview_enablement_record_id as _launchplane_preview_enablement_record_id,
    launchplane_promotion_bundle_relative_path as _launchplane_promotion_bundle_relative_path,
    relative_href as _relative_href,
    render_launchplane_environment_status_page_html,
    render_launchplane_preview_index_page_html,
    render_launchplane_preview_policy_page_html as _render_launchplane_preview_policy_page_html,
    render_launchplane_preview_status_page_html,
    render_launchplane_promotion_status_page_html,
)
from control_plane.launchplane_mutations import (
    control_plane_root as shared_control_plane_root,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.factory import build_record_store, resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.service_auth import load_authz_policy
from control_plane.workflows.launchplane import (
    build_preview_inventory_payload,
    build_preview_status_payload,
    launchplane_anchor_repo_context,
    launchplane_preview_label_enabled,
    LAUNCHPLANE_PREVIEW_ENABLE_LABEL,
    resolve_pull_request_event_manifest,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackRequest,
    OdooProdRollbackResult,
    execute_odoo_prod_rollback,
)
from control_plane.workflows import promotion_ship_execution
from control_plane.workflows import promotion_ship_resolution
from control_plane.workflows.ship import utc_now_timestamp

ARTIFACT_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"
ENVIRONMENT_STATUS_HISTORY_LIMIT = 3
_render_launchplane_environment_status_page_html = render_launchplane_environment_status_page_html
_render_launchplane_preview_index_page_html = render_launchplane_preview_index_page_html
_render_launchplane_preview_status_page_html = render_launchplane_preview_status_page_html
_render_launchplane_promotion_status_page_html = render_launchplane_promotion_status_page_html
_LEGACY_MONOREPO_MARKER = "odoo-ai"
_RUNTIME_CONTRACT_ENV_KEYS = (
    ARTIFACT_IMAGE_REFERENCE_ENV_KEY,
    "ODOO_BASE_RUNTIME_IMAGE",
    "ODOO_BASE_DEVTOOLS_IMAGE",
    "ODOO_ADDON_REPOSITORIES",
    "OPENUPGRADE_ADDON_REPOSITORY",
)
_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
_MASTER_ENCRYPTION_KEY_ENV_KEYS = ("LAUNCHPLANE_MASTER_ENCRYPTION_KEY",)
_LAUNCHPLANE_SERVICE_POLICY_ENV_KEYS = (
    "LAUNCHPLANE_POLICY_TOML",
    "LAUNCHPLANE_POLICY_B64",
    "LAUNCHPLANE_POLICY_FILE",
)
_SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS = {"PASSWORD", "TOKEN", "SECRET", "KEY"}
_SUCCESSFUL_DOKPLOY_STATUSES = {"success", "succeeded", "done", "completed", "healthy", "finished"}


@overload
def _store(state_dir: Path, *, database_url: None = None) -> FilesystemRecordStore: ...


@overload
def _store(
    state_dir: Path, *, database_url: str
) -> FilesystemRecordStore | PostgresRecordStore: ...


def _store(
    state_dir: Path, *, database_url: str | None = None
) -> FilesystemRecordStore | PostgresRecordStore:
    if database_url is not None and database_url.strip():
        return build_record_store(state_dir=state_dir, database_url=database_url)
    return FilesystemRecordStore(state_dir=state_dir)


def _odoo_store_factory(
    state_dir: Path, *, database_url: str | None = None
) -> FilesystemRecordStore | PostgresRecordStore:
    return _store(state_dir, database_url=database_url)


def _execute_odoo_prod_rollback_from_cli(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooProdRollbackRequest,
) -> OdooProdRollbackResult:
    return execute_odoo_prod_rollback(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=request,
    )


def _control_plane_root() -> Path:
    return shared_control_plane_root()


def _resolve_bootstrap_policy_file(*, control_plane_root: Path, policy_file: Path | None) -> Path:
    if policy_file is None:
        raise click.ClickException(
            "Launchplane bootstrap policy rendering requires --policy-file. "
            "The repo no longer tracks a live authz policy file."
        )
    resolved = policy_file
    if not resolved.is_absolute():
        resolved = control_plane_root / resolved
    if resolved.name.endswith(".example"):
        raise click.ClickException(
            f"Refusing to use example authz policy file as live bootstrap source: {resolved}"
        )
    if not resolved.is_file():
        raise click.ClickException(f"Launchplane bootstrap policy file does not exist: {resolved}")
    return resolved


def _build_bootstrap_policy_payload(
    *,
    control_plane_root: Path,
    policy_file: Path | None,
) -> dict[str, object]:
    resolved_policy_file = _resolve_bootstrap_policy_file(
        control_plane_root=control_plane_root,
        policy_file=policy_file,
    )
    policy_text = resolved_policy_file.read_text(encoding="utf-8")
    policy = load_authz_policy(resolved_policy_file)
    policy_bytes = policy_text.encode("utf-8")
    return {
        "policy_file": str(resolved_policy_file),
        "policy_b64": base64.b64encode(policy_bytes).decode("ascii"),
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "github_actions_rule_count": len(policy.github_actions),
    }


_ChoiceValue = TypeVar("_ChoiceValue", bound=str)
OdooOverrideApplyStatus = Literal["skipped", "pending", "pass", "fail"]
DokployTargetType = Literal["compose", "application"]


def _normalize_cli_choice(
    value: str,
    *,
    choices: Mapping[str, _ChoiceValue],
    error_message: str,
) -> _ChoiceValue:
    normalized_value = value.strip().lower()
    try:
        return choices[normalized_value]
    except KeyError as exc:
        raise click.ClickException(error_message) from exc


def _normalize_odoo_apply_status(
    status: str,
) -> OdooOverrideApplyStatus:
    return cast(
        OdooOverrideApplyStatus,
        _normalize_cli_choice(
            status,
            choices={
                "skipped": "skipped",
                "pending": "pending",
                "pass": "pass",
                "fail": "fail",
            },
            error_message="Odoo override apply status must be skipped, pending, pass, or fail.",
        ),
    )


def _normalize_dokploy_target_type(target_type: str) -> DokployTargetType:
    return cast(
        DokployTargetType,
        _normalize_cli_choice(
            target_type,
            choices={"compose": "compose", "application": "application"},
            error_message="Dokploy target type must be compose or application.",
        ),
    )


def _mutate_dokploy_payload_for_target_creation(
    host: str,
    token: str,
    path: str,
    payload: dict[str, control_plane_dokploy.JsonValue],
) -> dict[str, control_plane_dokploy.JsonValue]:
    response = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path=path,
        method="POST",
        payload=payload,
    )
    response_object = control_plane_dokploy.as_json_object(response)
    if response_object is None:
        raise click.ClickException(f"Dokploy API POST {path} returned an invalid response.")
    return response_object


def _sync_launchplane_bootstrap_policy(
    *,
    target_type: str,
    target_id: str,
    control_plane_root: Path,
    policy_file: Path | None,
    apply_changes: bool,
) -> dict[str, object]:
    policy_payload = _build_bootstrap_policy_payload(
        control_plane_root=control_plane_root,
        policy_file=policy_file,
    )
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    raw_env_text = str(target_payload.get("env") or "")
    env_map = control_plane_dokploy.parse_dokploy_env_text(raw_env_text)
    current_policy_b64 = env_map.get("LAUNCHPLANE_POLICY_B64", "")
    changed = current_policy_b64 != str(policy_payload["policy_b64"])
    if apply_changes and changed:
        env_map["LAUNCHPLANE_POLICY_B64"] = str(policy_payload["policy_b64"])
        env_map.pop("LAUNCHPLANE_POLICY_TOML", None)
        env_map.pop("LAUNCHPLANE_POLICY_FILE", None)
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            target_payload=target_payload,
            env_text=control_plane_dokploy.serialize_dokploy_env_text(env_map),
        )
    current_policy_sha256 = ""
    if current_policy_b64:
        try:
            current_policy_sha256 = hashlib.sha256(
                base64.b64decode(current_policy_b64, validate=True)
            ).hexdigest()
        except Exception:
            current_policy_sha256 = ""
    return {
        "status": "ok",
        "target_type": target_type,
        "target_id": target_id,
        "apply_changes": apply_changes,
        "changed": changed,
        "current_policy_sha256": current_policy_sha256,
        "desired_policy_sha256": policy_payload["policy_sha256"],
        "desired_policy_file": policy_payload["policy_file"],
        "github_actions_rule_count": policy_payload["github_actions_rule_count"],
    }


def _require_launchplane_preview_status_payload(
    *,
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> dict[str, object]:
    payload = build_preview_status_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if payload is None:
        raise click.ClickException(
            f"No Launchplane preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    return payload


def _launchplane_preview_profile_rows(
    record_store: FilesystemRecordStore,
) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for profile in record_store.list_product_profile_records():
        if not profile.preview.enabled or not profile.preview.context.strip():
            continue
        rows.append(
            (profile.repository.strip().rsplit("/", maxsplit=1)[-1], profile.preview.context)
        )
    return tuple(sorted(rows))


def _launchplane_context_anchor_repo_from_store(
    *, record_store: FilesystemRecordStore, context_name: str
) -> str:
    normalized_context = context_name.strip()
    if not normalized_context:
        return ""
    for repo_name, repo_context in _launchplane_preview_profile_rows(record_store):
        if repo_context == normalized_context:
            return repo_name
    return ""


def _build_launchplane_environment_action_payload(
    *,
    context_name: str,
    instance_name: str,
    environment_payload: dict[str, object] | None,
) -> dict[str, object]:
    live_payload = (
        environment_payload.get("live") if isinstance(environment_payload, dict) else None
    )
    artifact_id = (
        str(live_payload.get("artifact_id", "")).strip() if isinstance(live_payload, dict) else ""
    )
    source_git_ref = (
        str(live_payload.get("source_git_ref", "")).strip()
        if isinstance(live_payload, dict)
        else ""
    )
    deploy_status = (
        str(live_payload.get("deploy_status", "")).strip().lower()
        if isinstance(live_payload, dict)
        else ""
    )
    if not artifact_id or not source_git_ref:
        return {
            "instance": instance_name,
            "status": "missing_evidence",
            "tone": "neutral",
            "headline": f"{instance_name.capitalize()} has no actionable ship evidence yet.",
            "summary": "Launchplane needs a live artifact id and source ref before it can emit a typed ship recipe for this lane.",
            "recipe": "",
        }
    tone = "good" if deploy_status == "pass" else "warn"
    return {
        "instance": instance_name,
        "status": "actionable",
        "tone": tone,
        "headline": f"Re-ship current {instance_name} artifact",
        "summary": (
            f"Resolve and execute Launchplane's typed ship flow for the current {instance_name} artifact without re-deriving inputs by hand."
        ),
        "artifact_id": artifact_id,
        "source_git_ref": source_git_ref,
        "recipe": _build_launchplane_environment_ship_recipe_script(
            context_name=context_name,
            instance_name=instance_name,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
        ),
    }


def _launchplane_preview_enablement_actionable_payload(
    *,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
    anchor_pr_url: str,
    anchor_head_sha: str,
    state: str,
    label_enabled: bool,
    summary: str,
    baseline_release_tuple_id: str,
    resolved_manifest_fingerprint: str,
    source_map: list[dict[str, object]] | None = None,
    companion_summaries: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    preview_request_payload = {
        "schema_version": 1,
        "context": context_name,
        "anchor_repo": anchor_repo,
        "anchor_pr_number": anchor_pr_number,
        "anchor_pr_url": anchor_pr_url,
        "state": "pending",
        "created_at": "<utc-timestamp>",
        "updated_at": "<utc-timestamp>",
        "eligible_at": "<utc-timestamp>",
    }
    generation_request_payload = {
        "schema_version": 1,
        "context": context_name,
        "anchor_repo": anchor_repo,
        "anchor_pr_number": anchor_pr_number,
        "anchor_pr_url": anchor_pr_url,
        "anchor_head_sha": anchor_head_sha,
        "state": "resolving",
        "requested_reason": "operator_requested_enablement",
        "requested_at": "<utc-timestamp>",
        "resolved_manifest_fingerprint": resolved_manifest_fingerprint,
        "baseline_release_tuple_id": baseline_release_tuple_id,
        "source_map": source_map
        if source_map is not None
        else [
            {
                "repo": anchor_repo,
                "git_sha": anchor_head_sha,
                "selection": "anchor",
            }
        ],
        "companion_summaries": companion_summaries or [],
        "deploy_status": "pending",
        "verify_status": "pending",
        "overall_health_status": "pending",
    }
    action_slug = _launchplane_action_slug(f"{context_name}-{anchor_repo}-pr-{anchor_pr_number}")
    recipe = _build_launchplane_action_script(
        command_name="request-generation",
        file_payloads=(
            (
                "PREVIEW_FILE",
                f"/tmp/launchplane-{action_slug}-preview.json",
                preview_request_payload,
            ),
            (
                "GENERATION_FILE",
                f"/tmp/launchplane-{action_slug}-generation.json",
                generation_request_payload,
            ),
        ),
        command_args=(
            "--preview-input-file",
            '"$PREVIEW_FILE"',
            "--generation-input-file",
            '"$GENERATION_FILE"',
        ),
    )
    if state == "requested":
        headline = "Materialize requested Launchplane preview"
    elif label_enabled:
        headline = "Request Launchplane preview from saved label state"
    else:
        headline = "Request Launchplane preview"
    return {
        "status": "actionable",
        "tone": "warn",
        "headline": headline,
        "summary": summary,
        "recipe": recipe,
        "recipe_id": f"enablement-{action_slug}-request-generation",
    }


def _launchplane_preview_companion_snapshot_source_map_payload(
    *,
    anchor_repo: str,
    anchor_head_sha: str,
    request_metadata_companion_summaries: tuple[PreviewPullRequestSummary, ...],
) -> list[dict[str, object]]:
    companion_rows: list[dict[str, object]] = [
        {
            "repo": summary.repo,
            "git_sha": summary.head_sha,
            "selection": "companion",
        }
        for summary in request_metadata_companion_summaries
    ]
    return [
        {
            "repo": anchor_repo,
            "git_sha": anchor_head_sha,
            "selection": "anchor",
        },
        *companion_rows,
    ]


def _launchplane_preview_companion_summaries_payload(
    request_metadata_companion_summaries: tuple[PreviewPullRequestSummary, ...],
) -> list[dict[str, object]]:
    return [summary.model_dump(mode="json") for summary in request_metadata_companion_summaries]


def _launchplane_preview_unresolved_companion_payload(*, detail: str) -> dict[str, object]:
    return {
        "status": "blocked",
        "tone": "bad",
        "headline": "Companion PR snapshots are required before Launchplane can request this preview.",
        "summary": detail,
        "recipe": "",
    }


def _build_launchplane_preview_enablement_action_payload(
    *,
    control_plane_root: Path | None,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
    anchor_pr_url: str,
    anchor_head_sha: str,
    state: str,
    label_enabled: bool,
    request_metadata_status: str,
    request_metadata_baseline_channel: str,
    request_metadata_companions: tuple[LaunchplaneCompanionPullRequestReference, ...],
    request_metadata_companion_summaries: tuple[PreviewPullRequestSummary, ...],
    preview_row: dict[str, object] | None,
) -> dict[str, object]:
    if preview_row is not None:
        return {
            "status": "existing_preview",
            "tone": "neutral",
            "headline": "Preview detail already exists.",
            "summary": "Use the preview detail page or live route instead of requesting a second initial preview action from the tenant page.",
            "recipe": "",
        }
    if state not in {"candidate", "requested"}:
        return {
            "status": "none",
            "tone": "neutral",
            "headline": "No preview action available.",
            "summary": "This preview row does not need an initial Launchplane request recipe right now.",
            "recipe": "",
        }
    if request_metadata_status == "invalid":
        return {
            "status": "blocked",
            "tone": "bad",
            "headline": "Preview metadata must be fixed before Launchplane can request this preview.",
            "summary": "The saved PR snapshot says the Launchplane preview metadata is invalid, so Launchplane avoids printing a request recipe that would drift from the PR contract.",
            "recipe": "",
        }
    request_metadata = LaunchplanePreviewRequestParseResult(status="missing")
    if request_metadata_status == "valid":
        request_metadata = LaunchplanePreviewRequestParseResult(
            status="valid",
            metadata=LaunchplanePreviewRequestMetadata(
                baseline_channel=request_metadata_baseline_channel,
                companions=request_metadata_companions,
            ),
        )
    if control_plane_root is None:
        if request_metadata_companions and not request_metadata_companion_summaries:
            return _launchplane_preview_unresolved_companion_payload(
                detail="The saved PR metadata asks Launchplane to include companion pull requests, but Launchplane has no exact companion head SHA snapshot for this enablement record."
            )
        return _launchplane_preview_enablement_actionable_payload(
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            anchor_pr_url=anchor_pr_url,
            anchor_head_sha=anchor_head_sha,
            state=state,
            label_enabled=label_enabled,
            summary=(
                "Launchplane cannot resolve the default baseline contract from this workspace snapshot, so this request recipe keeps explicit placeholders for the baseline tuple id and manifest fingerprint before execution."
            ),
            baseline_release_tuple_id="<resolved-baseline-tuple-id>",
            resolved_manifest_fingerprint="<resolved-manifest-fingerprint>",
            source_map=(
                _launchplane_preview_companion_snapshot_source_map_payload(
                    anchor_repo=anchor_repo,
                    anchor_head_sha=anchor_head_sha,
                    request_metadata_companion_summaries=request_metadata_companion_summaries,
                )
                if request_metadata_companion_summaries
                else None
            ),
            companion_summaries=_launchplane_preview_companion_summaries_payload(
                request_metadata_companion_summaries
            ),
        )
    if not anchor_pr_url or not anchor_head_sha:
        return {
            "status": "missing_evidence",
            "tone": "neutral",
            "headline": "Launchplane needs the latest PR URL and head SHA before it can request this preview.",
            "summary": "The current tenant snapshot is missing the exact anchor PR evidence needed for a typed request-generation recipe.",
            "recipe": "",
        }
    if request_metadata_companions and not request_metadata_companion_summaries:
        return _launchplane_preview_unresolved_companion_payload(
            detail="The saved PR metadata asks Launchplane to include companion pull requests, but Launchplane has no exact companion head SHA snapshot for this enablement record."
        )

    synthetic_event = GitHubPullRequestEvent(
        action="opened",
        repo=anchor_repo,
        pr_number=anchor_pr_number,
        pr_url=anchor_pr_url,
        occurred_at="<utc-timestamp>",
        pr_body="",
        state="open",
        merged=False,
        head_sha=anchor_head_sha,
        label_names=(LAUNCHPLANE_PREVIEW_ENABLE_LABEL,) if label_enabled else (),
        action_label=LAUNCHPLANE_PREVIEW_ENABLE_LABEL if label_enabled else "",
    )
    try:
        resolved_manifest = resolve_pull_request_event_manifest(
            control_plane_root=control_plane_root,
            event=synthetic_event,
            resolved_context=context_name,
            preview=None,
            request_metadata=request_metadata,
            companion_summaries_snapshot=request_metadata_companion_summaries,
        )
    except click.ClickException as exc:
        if request_metadata_companions and not request_metadata_companion_summaries:
            return _launchplane_preview_unresolved_companion_payload(
                detail=(
                    "The saved PR metadata asks Launchplane to include companion pull requests, but Launchplane could not prove their exact head SHAs from stored evidence. "
                    f"Resolve the companion snapshots before running the request recipe: {exc}"
                ),
            )
        return _launchplane_preview_enablement_actionable_payload(
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            anchor_pr_url=anchor_pr_url,
            anchor_head_sha=anchor_head_sha,
            state=state,
            label_enabled=label_enabled,
            summary=(
                "Launchplane could not resolve the default preview manifest automatically for this PR yet. "
                f"Use the typed request recipe below after replacing the baseline placeholders: {exc}"
            ),
            baseline_release_tuple_id="<resolved-baseline-tuple-id>",
            resolved_manifest_fingerprint="<resolved-manifest-fingerprint>",
            source_map=(
                _launchplane_preview_companion_snapshot_source_map_payload(
                    anchor_repo=anchor_repo,
                    anchor_head_sha=anchor_head_sha,
                    request_metadata_companion_summaries=request_metadata_companion_summaries,
                )
                if request_metadata_companion_summaries
                else None
            ),
            companion_summaries=_launchplane_preview_companion_summaries_payload(
                request_metadata_companion_summaries
            ),
        )
    if resolved_manifest is None:
        if request_metadata_companions and not request_metadata_companion_summaries:
            return _launchplane_preview_unresolved_companion_payload(
                detail="The saved PR metadata asks Launchplane to include companion pull requests, but Launchplane has no exact companion head SHA snapshot for this enablement record."
            )
        return _launchplane_preview_enablement_actionable_payload(
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            anchor_pr_url=anchor_pr_url,
            anchor_head_sha=anchor_head_sha,
            state=state,
            label_enabled=label_enabled,
            summary=(
                "Launchplane could not resolve the default preview manifest automatically for this PR yet. "
                "Use the typed request recipe below after replacing the baseline tuple id and manifest fingerprint placeholders."
            ),
            baseline_release_tuple_id="<resolved-baseline-tuple-id>",
            resolved_manifest_fingerprint="<resolved-manifest-fingerprint>",
            source_map=(
                _launchplane_preview_companion_snapshot_source_map_payload(
                    anchor_repo=anchor_repo,
                    anchor_head_sha=anchor_head_sha,
                    request_metadata_companion_summaries=request_metadata_companion_summaries,
                )
                if request_metadata_companion_summaries
                else None
            ),
            companion_summaries=_launchplane_preview_companion_summaries_payload(
                request_metadata_companion_summaries
            ),
        )

    return _launchplane_preview_enablement_actionable_payload(
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=anchor_pr_url,
        anchor_head_sha=anchor_head_sha,
        state=state,
        label_enabled=label_enabled,
        summary=(
            "This tenant PR is preview-eligible but still inactive. Run Launchplane's typed request-generation flow to create the initial preview route from the default testing baseline."
            if state != "requested" and not label_enabled
            else "GitHub already marked this PR for preview. Run Launchplane's typed request-generation flow to turn that saved label state into a live preview route."
            if label_enabled and state != "requested"
            else "A preview request exists, but Launchplane has not produced a serving route yet. Run the typed request-generation flow to materialize the preview from the saved PR snapshot."
        ),
        baseline_release_tuple_id=resolved_manifest.baseline_release_tuple_id,
        resolved_manifest_fingerprint=resolved_manifest.resolved_manifest_fingerprint,
        source_map=[item.model_dump(mode="json") for item in resolved_manifest.source_map],
        companion_summaries=[
            item.model_dump(mode="json") for item in resolved_manifest.companion_summaries
        ],
    )


def _launchplane_promotion_check_status(
    value: str,
    *,
    allow_skipped: bool = False,
) -> str:
    normalized_value = value.strip().lower()
    if normalized_value == "pass":
        return "pass"
    if allow_skipped and normalized_value == "skipped":
        return "pass"
    if normalized_value in {"pending", ""}:
        return "pending"
    return "fail"


def _launchplane_promotion_candidate_evidence_check(
    *,
    testing_artifact_id: str,
    prod_artifact_id: str,
) -> dict[str, str]:
    candidate_status = "pending"
    candidate_detail = "Launchplane is waiting for testing/prod inventory evidence before it can name a promotion candidate."
    if testing_artifact_id and prod_artifact_id and testing_artifact_id == prod_artifact_id:
        candidate_status = "pass"
        candidate_detail = f"Testing and prod are already aligned on {testing_artifact_id}."
    elif testing_artifact_id:
        candidate_status = "pass"
        candidate_detail = f"Testing is carrying {testing_artifact_id or 'an artifact'} while prod is carrying {prod_artifact_id or 'nothing recorded yet'}."
    elif prod_artifact_id:
        candidate_status = "fail"
        candidate_detail = (
            "Prod has an artifact, but Launchplane has no current testing artifact to promote from."
        )
    return {
        "label": "Promotion candidate",
        "status": candidate_status,
        "detail": candidate_detail,
    }


def _launchplane_promotion_backup_gate_evidence_check(
    latest_backup_gate: BackupGateRecord | None,
) -> dict[str, str]:
    if latest_backup_gate is None:
        return {
            "label": "Prod backup gate",
            "status": "pending",
            "detail": "Launchplane has no prod backup-gate evidence yet. Promotion stays blocked until one is recorded.",
        }
    if latest_backup_gate.required and latest_backup_gate.status == "pass":
        return {
            "label": "Prod backup gate",
            "status": "pass",
            "detail": f"Latest prod backup gate {latest_backup_gate.record_id} passed and can authorize promotion.",
        }
    if latest_backup_gate.status == "fail":
        return {
            "label": "Prod backup gate",
            "status": "fail",
            "detail": f"Latest prod backup gate {latest_backup_gate.record_id} failed. Promotion is blocked until a passing gate is recorded.",
        }
    return {
        "label": "Prod backup gate",
        "status": "pending",
        "detail": f"Latest prod backup gate {latest_backup_gate.record_id} is {latest_backup_gate.status} and does not yet authorize promotion.",
    }


def _build_launchplane_promotion_action_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    testing_environment: dict[str, object] | None,
    prod_environment: dict[str, object] | None,
) -> dict[str, object]:
    testing_live = (
        testing_environment.get("live") if isinstance(testing_environment, dict) else None
    )
    prod_live = prod_environment.get("live") if isinstance(prod_environment, dict) else None
    testing_artifact_id = (
        str(testing_live.get("artifact_id", "")).strip() if isinstance(testing_live, dict) else ""
    )
    prod_artifact_id = (
        str(prod_live.get("artifact_id", "")).strip() if isinstance(prod_live, dict) else ""
    )
    testing_source_git_ref = (
        str(testing_live.get("source_git_ref", "")).strip()
        if isinstance(testing_live, dict)
        else ""
    )
    testing_deploy_status = (
        str(testing_live.get("deploy_status", "")).strip().lower()
        if isinstance(testing_live, dict)
        else ""
    )
    testing_health_status = (
        str(testing_live.get("destination_health_status", "")).strip().lower()
        if isinstance(testing_live, dict)
        else ""
    )
    latest_promotion = (
        prod_environment.get("latest_promotion") if isinstance(prod_environment, dict) else None
    )
    recent_backup_gates = record_store.list_backup_gate_records(
        context_name=context_name,
        instance_name="prod",
        limit=3,
    )
    latest_backup_gate = recent_backup_gates[0] if recent_backup_gates else None

    evidence_checks: list[dict[str, str]] = []
    evidence_checks.append(
        _launchplane_promotion_candidate_evidence_check(
            testing_artifact_id=testing_artifact_id,
            prod_artifact_id=prod_artifact_id,
        )
    )

    deploy_check_status = _launchplane_promotion_check_status(testing_deploy_status)
    evidence_checks.append(
        {
            "label": "Testing deploy",
            "status": deploy_check_status,
            "detail": (
                f"Latest testing deployment status is {testing_deploy_status or 'unavailable'}."
                if testing_live is not None
                else "Launchplane has not recorded a live testing deployment yet."
            ),
        }
    )

    health_check_status = _launchplane_promotion_check_status(
        testing_health_status,
        allow_skipped=True,
    )
    evidence_checks.append(
        {
            "label": "Testing health",
            "status": health_check_status,
            "detail": (
                f"Latest testing health status is {testing_health_status or 'unavailable'}."
                if testing_live is not None
                else "Launchplane has not recorded testing health evidence yet."
            ),
        }
    )

    backup_record_id = ""
    backup_gate_source = "prod-gate"
    backup_gate_evidence: dict[str, str] = {"snapshot": "s3://path/to/prod-backup"}
    if latest_backup_gate is not None:
        backup_record_id = latest_backup_gate.record_id
        backup_gate_source = latest_backup_gate.source
        backup_gate_evidence = dict(latest_backup_gate.evidence)
    backup_gate_check = _launchplane_promotion_backup_gate_evidence_check(latest_backup_gate)
    backup_check_status = backup_gate_check["status"]
    evidence_checks.append(backup_gate_check)

    promotion_status = "unknown"
    headline = "Launchplane cannot plan the next promotion yet."
    summary = "Testing and prod need clearer environment evidence before Launchplane can describe the next action."
    next_action = "Wait for Launchplane to record current tenant environment evidence."
    tone = "neutral"
    backup_gate_recipe = ""
    resolve_recipe = ""
    execute_recipe = ""
    if testing_artifact_id and prod_artifact_id and testing_artifact_id == prod_artifact_id:
        promotion_status = "in_sync"
        headline = "Prod is already serving the current testing artifact."
        summary = (
            "No promotion is pending because the tenant's long-lived lanes are already aligned."
        )
        next_action = (
            "Keep shipping new artifacts into testing until a new promotion candidate appears."
        )
        tone = "good"
    elif testing_artifact_id:
        all_checks_pass = (
            deploy_check_status == "pass"
            and health_check_status == "pass"
            and backup_check_status == "pass"
        )
        if all_checks_pass:
            promotion_status = "promotable"
            headline = "Testing is ready to promote into prod."
            summary = "Launchplane has the artifact, testing evidence, and backup authorization needed to plan the next promotion request."
            next_action = (
                "Resolve the typed promotion request, review it, then execute it against prod."
            )
            tone = "good"
            resolve_recipe = _build_launchplane_promotion_resolve_recipe_script(
                context_name=context_name,
                artifact_id=testing_artifact_id,
                backup_record_id=backup_record_id,
            )
            execute_recipe = _build_launchplane_promotion_execute_recipe_script(
                state_dir="/path/to/state"
            )
        else:
            promotion_status = "blocked"
            headline = "A newer testing artifact exists, but Launchplane cannot promote it yet."
            summary = "The tenant has a promotion candidate, but one or more required evidence checks are still missing or failing."
            next_action = (
                "Clear the failing evidence checks before using Launchplane's promotion flow."
            )
            tone = "warn"
            if backup_check_status != "pass":
                backup_gate_recipe = _build_launchplane_backup_gate_write_recipe_script(
                    context_name=context_name,
                    source=backup_gate_source,
                    evidence=backup_gate_evidence,
                )
    elif prod_artifact_id:
        promotion_status = "prod_only"
        headline = "Prod has live evidence, but Launchplane has no current testing candidate."
        summary = "Launchplane cannot promote until testing is carrying the next exact artifact for this tenant."
        next_action = "Ship a new artifact into testing first, then return here to plan promotion."
        tone = "neutral"

    retained_evidence = "Launchplane will append a promotion record, keep backup-gate evidence, and refresh prod inventory when a waited promotion completes successfully."
    return {
        "status": promotion_status,
        "tone": tone,
        "headline": headline,
        "summary": summary,
        "next_action": next_action,
        "candidate_artifact_id": testing_artifact_id,
        "current_prod_artifact_id": prod_artifact_id,
        "source_git_ref": testing_source_git_ref,
        "latest_backup_gate": _summarize_backup_gate_record(latest_backup_gate)
        if latest_backup_gate is not None
        else None,
        "latest_promotion": latest_promotion if isinstance(latest_promotion, dict) else None,
        "evidence_checks": evidence_checks,
        "retained_evidence": retained_evidence,
        "backup_gate_recipe": backup_gate_recipe,
        "resolve_recipe": resolve_recipe,
        "execute_recipe": execute_recipe,
    }


def _build_launchplane_promotion_detail_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    testing_environment: dict[str, object] | None,
    prod_environment: dict[str, object] | None,
    promotion_action: dict[str, object],
) -> dict[str, object] | None:
    recent_backup_gates = record_store.list_backup_gate_records(
        context_name=context_name,
        instance_name="prod",
        limit=ENVIRONMENT_STATUS_HISTORY_LIMIT,
    )
    recent_promotions_payload = (
        prod_environment.get("recent_promotions") if isinstance(prod_environment, dict) else None
    )
    recent_promotions = (
        list(recent_promotions_payload)
        if isinstance(recent_promotions_payload, (list, tuple))
        else []
    )
    testing_live = (
        testing_environment.get("live") if isinstance(testing_environment, dict) else None
    )
    prod_live = prod_environment.get("live") if isinstance(prod_environment, dict) else None
    latest_backup_gate = (
        promotion_action.get("latest_backup_gate")
        if isinstance(promotion_action.get("latest_backup_gate"), dict)
        else None
    )
    latest_promotion = (
        promotion_action.get("latest_promotion")
        if isinstance(promotion_action.get("latest_promotion"), dict)
        else None
    )
    evidence_checks = _json_object_items(promotion_action.get("evidence_checks"))
    if (
        testing_live is None
        and prod_live is None
        and not recent_promotions
        and not recent_backup_gates
        and not any(
            str(promotion_action.get(key, "")).strip()
            for key in ("candidate_artifact_id", "current_prod_artifact_id")
        )
    ):
        return None
    return {
        "context": context_name,
        "path_label": f"{context_name}/testing-to-prod",
        "from_instance": "testing",
        "to_instance": "prod",
        "status": str(promotion_action.get("status", "unknown")).strip() or "unknown",
        "tone": str(promotion_action.get("tone", "neutral")).strip() or "neutral",
        "headline": str(
            promotion_action.get("headline", "Launchplane cannot describe the promotion path yet.")
        ),
        "summary": str(promotion_action.get("summary", "No promotion summary recorded.")),
        "next_action": str(promotion_action.get("next_action", "No next action recorded.")),
        "candidate_artifact_id": str(promotion_action.get("candidate_artifact_id", "")).strip(),
        "current_prod_artifact_id": str(
            promotion_action.get("current_prod_artifact_id", "")
        ).strip(),
        "source_git_ref": str(promotion_action.get("source_git_ref", "")).strip(),
        "retained_evidence": str(promotion_action.get("retained_evidence", "")).strip(),
        "evidence_checks": evidence_checks,
        "latest_backup_gate": latest_backup_gate,
        "latest_promotion": latest_promotion,
        "recent_backup_gates": tuple(
            _summarize_backup_gate_record(record) for record in recent_backup_gates
        ),
        "recent_promotions": tuple(item for item in recent_promotions if isinstance(item, dict)),
        "testing_live": testing_live if isinstance(testing_live, dict) else None,
        "prod_live": prod_live if isinstance(prod_live, dict) else None,
        "backup_gate_recipe": str(promotion_action.get("backup_gate_recipe", "")).strip(),
        "resolve_recipe": str(promotion_action.get("resolve_recipe", "")).strip(),
        "execute_recipe": str(promotion_action.get("execute_recipe", "")).strip(),
    }


def _build_launchplane_preview_enablement_record(
    *,
    context_name: str,
    event: GitHubPullRequestEvent,
    request_metadata: LaunchplanePreviewRequestParseResult,
    resolved_manifest: LaunchplaneResolvedPreviewManifest | None = None,
) -> PreviewEnablementRecord | None:
    resolved_context = context_name.strip()
    if not resolved_context:
        return None
    updated_at = event.occurred_at.strip() or utc_now_timestamp()
    return PreviewEnablementRecord(
        record_id=_launchplane_preview_enablement_record_id(
            context_name=resolved_context,
            anchor_repo=event.repo,
            anchor_pr_number=event.pr_number,
        ),
        context=resolved_context,
        anchor_repo=event.repo,
        anchor_pr_number=event.pr_number,
        anchor_pr_url=event.pr_url,
        anchor_head_sha=event.head_sha,
        action=event.action,
        pr_state=event.state,
        updated_at=updated_at,
        label_enabled=launchplane_preview_label_enabled(label_names=event.label_names),
        action_label=event.action_label,
        request_metadata_status=request_metadata.status,
        request_metadata_error=request_metadata.error,
        request_metadata_baseline_channel=(
            request_metadata.metadata.baseline_channel
            if request_metadata.metadata is not None
            else ""
        ),
        request_metadata_companions=(
            request_metadata.metadata.companions if request_metadata.metadata is not None else ()
        ),
        request_metadata_companion_summaries=_enablement_companion_summaries_snapshot(
            request_metadata=request_metadata,
            resolved_manifest=resolved_manifest,
        ),
    )


def _enablement_companion_summaries_snapshot(
    *,
    request_metadata: LaunchplanePreviewRequestParseResult,
    resolved_manifest: LaunchplaneResolvedPreviewManifest | None,
) -> tuple[PreviewPullRequestSummary, ...]:
    if request_metadata.metadata is None or resolved_manifest is None:
        return ()
    requested_keys = tuple(
        (companion.repo.strip(), companion.pr_number)
        for companion in request_metadata.metadata.companions
    )
    if not requested_keys:
        return ()
    summary_by_key = {
        (summary.repo.strip(), summary.pr_number): summary
        for summary in resolved_manifest.companion_summaries
    }
    summaries: list[PreviewPullRequestSummary] = []
    for requested_key in requested_keys:
        summary = summary_by_key.get(requested_key)
        if summary is None:
            return ()
        summaries.append(summary)
    return tuple(summaries)


def _launchplane_preview_enablement_item_tone(state: str) -> str:
    if state == "running":
        return "good"
    if state in {"requested", "paused"}:
        return "warn"
    return "neutral"


def _launchplane_preview_enablement_item_source(
    *,
    label_enabled: bool,
    preview_row: dict[str, object] | None,
    latest_requested_reason: str,
) -> str:
    if label_enabled:
        return "github_label"
    if latest_requested_reason.startswith("operator_requested"):
        return "launchplane"
    if preview_row is not None and not label_enabled:
        return "history"
    return "none"


def _launchplane_preview_enablement_item_state(
    *, preview_row: dict[str, object] | None, label_enabled: bool, pr_state: str
) -> str:
    preview_state = (
        str(preview_row.get("state", "")).strip().lower() if preview_row is not None else ""
    )
    serving_generation_id = (
        str(preview_row.get("serving_generation_id", "")).strip() if preview_row is not None else ""
    )
    if preview_state == "destroyed":
        return "retained"
    if preview_state == "paused":
        return "paused"
    if preview_row is not None and serving_generation_id:
        return "running"
    if preview_row is not None or label_enabled:
        return "requested"
    if pr_state == "open":
        return "candidate"
    return ""


def _launchplane_preview_enablement_item_request_summary(
    *,
    state: str,
    source: str,
    request_metadata_status: str,
    request_metadata_error: str,
    preview_row: dict[str, object] | None,
) -> str:
    if state == "candidate":
        return "Eligible tenant PR. No preview request is active yet."
    if state == "retained":
        return "Launchplane is keeping this PR's preview history as retained evidence."
    if source == "github_label":
        if request_metadata_status == "invalid":
            return (
                "GitHub label launchplane-preview requested a preview, but Launchplane preview metadata is invalid: "
                f"{request_metadata_error}"
            )
        if preview_row is None:
            return "GitHub label launchplane-preview requested a preview, but Launchplane has not created the preview record yet."
        return "GitHub label launchplane-preview is the current preview request source."
    if source == "launchplane":
        return "Launchplane explicitly requested this preview without relying on the GitHub label."
    if state == "paused":
        return (
            "Launchplane is intentionally holding this preview in place until operators resume it."
        )
    return "Launchplane still has preview evidence from an earlier request even though no current GitHub label is present."


def _launchplane_preview_enablement_item_status_summary(
    *, state: str, preview_row: dict[str, object] | None
) -> str:
    if preview_row is not None:
        status_summary = str(preview_row.get("status_summary", "")).strip()
        if status_summary:
            return status_summary
    if state == "candidate":
        return "Ready for opt-in preview enablement."
    if state == "requested":
        return "Preview request recorded; Launchplane has not materialized a serving route yet."
    return "No additional preview lifecycle evidence recorded yet."


def _build_launchplane_preview_enablement_items(
    *,
    control_plane_root: Path | None,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    previews: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    enablement_records = list(
        record_store.list_preview_enablement_records(
            context_name=context_name,
            anchor_repo=anchor_repo,
        )
    )
    enablement_by_key: dict[tuple[str, int], PreviewEnablementRecord] = {}
    for record in enablement_records:
        key = (record.anchor_repo, record.anchor_pr_number)
        enablement_by_key.setdefault(key, record)

    preview_by_key: dict[tuple[str, int], dict[str, object]] = {}
    for row in previews:
        row_anchor_repo = str(row.get("anchor_repo", "")).strip()
        row_pr_number = _int_from_json_value(row.get("anchor_pr_number", 0))
        if not row_anchor_repo or row_pr_number <= 0:
            continue
        preview_by_key.setdefault((row_anchor_repo, row_pr_number), row)

    keys = set(enablement_by_key) | set(preview_by_key)
    items: list[dict[str, object]] = []
    for item_anchor_repo, item_pr_number in keys:
        preview_row = preview_by_key.get((item_anchor_repo, item_pr_number))
        enablement_record = enablement_by_key.get((item_anchor_repo, item_pr_number))
        pr_state = enablement_record.pr_state if enablement_record is not None else "open"
        state = _launchplane_preview_enablement_item_state(
            preview_row=preview_row,
            label_enabled=enablement_record.label_enabled
            if enablement_record is not None
            else False,
            pr_state=pr_state,
        )
        if not state:
            continue

        preview_id = (
            str(preview_row.get("preview_id", "")).strip() if preview_row is not None else ""
        )
        latest_requested_reason = ""
        if preview_id:
            latest_generations = record_store.list_preview_generation_records(
                preview_id=preview_id, limit=1
            )
            if latest_generations:
                latest_requested_reason = latest_generations[0].requested_reason

        label_enabled = enablement_record.label_enabled if enablement_record is not None else False
        source = _launchplane_preview_enablement_item_source(
            label_enabled=label_enabled,
            preview_row=preview_row,
            latest_requested_reason=latest_requested_reason,
        )
        request_metadata_status = (
            enablement_record.request_metadata_status
            if enablement_record is not None
            else "missing"
        )
        request_metadata_error = (
            enablement_record.request_metadata_error if enablement_record is not None else ""
        )
        anchor_head_sha = enablement_record.anchor_head_sha if enablement_record is not None else ""
        request_metadata_baseline_channel = (
            enablement_record.request_metadata_baseline_channel
            if enablement_record is not None
            else ""
        )
        request_metadata_companions = (
            enablement_record.request_metadata_companions if enablement_record is not None else ()
        )
        request_metadata_companion_summaries = (
            enablement_record.request_metadata_companion_summaries
            if enablement_record is not None
            else ()
        )

        timestamps = [
            str(preview_row.get("updated_at", "")).strip() if preview_row is not None else "",
            enablement_record.updated_at if enablement_record is not None else "",
        ]
        updated_at = max((value for value in timestamps if value), default="")
        anchor_pr_url = (
            str(preview_row.get("anchor_pr_url", "")).strip()
            if preview_row is not None
            else enablement_record.anchor_pr_url
            if enablement_record is not None
            else ""
        )
        preview_label = (
            str(preview_row.get("preview_label", "")).strip()
            if preview_row is not None
            else f"{context_name}/{item_anchor_repo}/pr-{item_pr_number}"
        )
        action_payload = _build_launchplane_preview_enablement_action_payload(
            control_plane_root=control_plane_root,
            context_name=context_name,
            anchor_repo=item_anchor_repo,
            anchor_pr_number=item_pr_number,
            anchor_pr_url=anchor_pr_url,
            anchor_head_sha=anchor_head_sha,
            state=state,
            label_enabled=label_enabled,
            request_metadata_status=request_metadata_status,
            request_metadata_baseline_channel=request_metadata_baseline_channel,
            request_metadata_companions=request_metadata_companions,
            request_metadata_companion_summaries=request_metadata_companion_summaries,
            preview_row=preview_row,
        )
        items.append(
            {
                "anchor_repo": item_anchor_repo,
                "anchor_pr_number": item_pr_number,
                "anchor_pr_url": anchor_pr_url,
                "anchor_head_sha": anchor_head_sha,
                "preview_id": preview_id,
                "preview_label": preview_label,
                "canonical_url": str(preview_row.get("canonical_url", "")).strip()
                if preview_row is not None
                else "",
                "state": state,
                "tone": _launchplane_preview_enablement_item_tone(state),
                "request_source": source,
                "request_summary": _launchplane_preview_enablement_item_request_summary(
                    state=state,
                    source=source,
                    request_metadata_status=request_metadata_status,
                    request_metadata_error=request_metadata_error,
                    preview_row=preview_row,
                ),
                "status_summary": _launchplane_preview_enablement_item_status_summary(
                    state=state, preview_row=preview_row
                ),
                "updated_at": updated_at,
                "label_enabled": label_enabled,
                "request_metadata_status": request_metadata_status,
                "request_metadata_baseline_channel": request_metadata_baseline_channel,
                "request_metadata_companion_summaries": [
                    item.model_dump(mode="json") for item in request_metadata_companion_summaries
                ],
                "preview_state": str(preview_row.get("state", "")).strip()
                if preview_row is not None
                else "",
                "action": action_payload,
            }
        )

    priority = {"candidate": 0, "requested": 1, "paused": 2, "running": 3, "retained": 4}
    items.sort(
        key=lambda item: _int_from_json_value(item.get("anchor_pr_number", 0)),
        reverse=True,
    )
    items.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    items.sort(key=lambda item: priority.get(str(item.get("state", "")).strip(), 9))

    counts = {"candidate": 0, "requested": 0, "running": 0, "paused": 0, "retained": 0}
    for item in items:
        item_state_value = str(item.get("state", "")).strip()
        if item_state_value in counts:
            counts[item_state_value] += 1
    return items, counts


def _build_launchplane_tenant_payload(
    *,
    control_plane_root: Path | None = None,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str = "",
) -> dict[str, object] | None:
    resolved_context = context_name.strip()
    resolved_anchor_repo = anchor_repo.strip()
    if resolved_context and not resolved_anchor_repo:
        resolved_anchor_repo = _launchplane_context_anchor_repo_from_store(
            record_store=record_store,
            context_name=resolved_context,
        )
    if resolved_anchor_repo and not resolved_context:
        resolved_context = launchplane_anchor_repo_context(
            record_store=record_store,
            repo=resolved_anchor_repo,
        )

    if not resolved_context and not resolved_anchor_repo:
        return None

    preview_inventory = build_preview_inventory_payload(
        record_store=record_store,
        context_name=resolved_context,
    )
    previews = _json_object_items(preview_inventory.get("previews"))
    if resolved_anchor_repo:
        previews = [
            item
            for item in previews
            if str(item.get("anchor_repo", "")).strip() == resolved_anchor_repo
        ]

    environments: dict[str, dict[str, object] | None] = {}
    if resolved_context:
        for instance_name in ("testing", "prod"):
            try:
                environments[instance_name] = _build_environment_status_payload(
                    record_store=record_store,
                    context_name=resolved_context,
                    instance_name=instance_name,
                )
            except FileNotFoundError:
                environments[instance_name] = None
    else:
        environments = {"testing": None, "prod": None}

    preview_enablement, preview_enablement_counts = _build_launchplane_preview_enablement_items(
        control_plane_root=control_plane_root,
        record_store=record_store,
        context_name=resolved_context,
        anchor_repo=resolved_anchor_repo,
        previews=previews,
    )

    if not previews and not preview_enablement and not any(environments.values()):
        return None

    preview_counts = {
        "all": len(previews),
        "attention": sum(
            1 for row in previews if _launchplane_inventory_bucket(row) == "attention"
        ),
        "in_flight": sum(
            1 for row in previews if _launchplane_inventory_bucket(row) == "in_flight"
        ),
        "live": sum(1 for row in previews if _launchplane_inventory_bucket(row) == "live"),
        "retained": sum(1 for row in previews if _launchplane_inventory_bucket(row) == "retained"),
        "reviewable": sum(
            1
            for row in previews
            if _launchplane_inventory_bucket(row) == "live"
            and str(row.get("canonical_url", "")).strip()
            and str(row.get("serving_generation_id", "")).strip()
        ),
    }
    preview_candidates = [
        item for item in preview_enablement if str(item.get("state", "")).strip() == "candidate"
    ]

    testing_environment = environments.get("testing")
    prod_environment = environments.get("prod")
    testing_live = (
        testing_environment.get("live") if isinstance(testing_environment, dict) else None
    )
    prod_live = prod_environment.get("live") if isinstance(prod_environment, dict) else None

    testing_artifact_id = (
        str(testing_live.get("artifact_id", "")).strip() if isinstance(testing_live, dict) else ""
    )
    prod_artifact_id = (
        str(prod_live.get("artifact_id", "")).strip() if isinstance(prod_live, dict) else ""
    )
    promotion_summary = {
        "status": "unknown",
        "summary": "Launchplane has not recorded enough tenant environment evidence to describe promotion state yet.",
    }
    if testing_artifact_id and prod_artifact_id and testing_artifact_id == prod_artifact_id:
        promotion_summary = {
            "status": "in_sync",
            "summary": "Prod is already serving the current testing artifact.",
            "artifact_id": testing_artifact_id,
        }
    elif testing_artifact_id:
        promotion_summary = {
            "status": "candidate",
            "summary": "Testing is carrying a newer artifact than prod and is the current promotion candidate.",
            "artifact_id": testing_artifact_id,
            "source_git_ref": str(testing_live.get("source_git_ref", "")).strip()
            if isinstance(testing_live, dict)
            else "",
        }
    elif prod_artifact_id:
        promotion_summary = {
            "status": "prod_only",
            "summary": "Prod has live deployment evidence, but Launchplane does not yet have current testing evidence for comparison.",
            "artifact_id": prod_artifact_id,
        }
    promotion_action = _build_launchplane_promotion_action_payload(
        record_store=record_store,
        context_name=resolved_context,
        testing_environment=testing_environment if isinstance(testing_environment, dict) else None,
        prod_environment=prod_environment if isinstance(prod_environment, dict) else None,
    )
    promotion_detail = _build_launchplane_promotion_detail_payload(
        record_store=record_store,
        context_name=resolved_context,
        testing_environment=testing_environment if isinstance(testing_environment, dict) else None,
        prod_environment=prod_environment if isinstance(prod_environment, dict) else None,
        promotion_action=promotion_action,
    )
    environment_actions = {
        instance_name: _build_launchplane_environment_action_payload(
            context_name=resolved_context,
            instance_name=instance_name,
            environment_payload=environments.get(instance_name)
            if isinstance(environments, dict)
            else None,
        )
        for instance_name in ("testing", "prod")
    }

    return {
        "context": resolved_context,
        "anchor_repo": resolved_anchor_repo,
        "tenant_label": "/".join(part for part in (resolved_context, resolved_anchor_repo) if part),
        "preview_counts": preview_counts,
        "preview_enablement_counts": preview_enablement_counts,
        "preview_enablement": preview_enablement,
        "preview_candidates": preview_candidates,
        "previews": previews,
        "environments": environments,
        "environment_actions": environment_actions,
        "promotion_summary": promotion_summary,
        "promotion_action": promotion_action,
        "promotion_detail": promotion_detail,
    }


def _write_launchplane_site_bundle(
    *,
    state_dir: Path,
    output_dir: Path,
    context_name: str,
) -> None:
    record_store = FilesystemRecordStore(state_dir=state_dir)
    inventory_payload = build_preview_inventory_payload(
        record_store=record_store,
        context_name=context_name,
    )
    tenant_payload = _build_launchplane_tenant_payload(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        context_name=context_name,
    )
    preview_items = _json_object_items(inventory_payload.get("previews"))
    tenant_environments = (
        tenant_payload.get("environments") if isinstance(tenant_payload, dict) else None
    )
    tenant_environment_actions = (
        tenant_payload.get("environment_actions") if isinstance(tenant_payload, dict) else None
    )
    tenant_promotion_detail = (
        tenant_payload.get("promotion_detail") if isinstance(tenant_payload, dict) else None
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    index_file = output_dir / "index.html"
    policy_file = output_dir / "policy.html"

    promotion_output_map: dict[str, Path] = {}
    if isinstance(tenant_promotion_detail, dict):
        item_context = str(tenant_promotion_detail.get("context", "")).strip() or context_name
        if item_context:
            promotion_output_map[item_context] = (
                output_dir
                / _launchplane_promotion_bundle_relative_path(
                    context_name=item_context,
                )
            )

    environment_output_map: dict[tuple[str, str], Path] = {}
    if isinstance(tenant_environments, dict):
        for instance_name in ("testing", "prod"):
            environment_payload = tenant_environments.get(instance_name)
            if not isinstance(environment_payload, dict):
                continue
            item_context = str(environment_payload.get("context", "")).strip() or context_name
            relative_path = _launchplane_environment_bundle_relative_path(
                context_name=item_context,
                instance_name=instance_name,
            )
            environment_output_map[(item_context, instance_name)] = output_dir / relative_path

    preview_output_map: dict[tuple[str, str, int], Path] = {}
    for item in preview_items:
        item_context = str(item.get("context", ""))
        anchor_repo = str(item.get("anchor_repo", ""))
        anchor_pr_number = _int_from_json_value(item.get("anchor_pr_number", 0))
        relative_path = _launchplane_preview_bundle_relative_path(
            context_name=item_context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        )
        preview_output_map[(item_context, anchor_repo, anchor_pr_number)] = (
            output_dir / relative_path
        )

    def detail_href_builder(item_context: str, anchor_repo: str, anchor_pr_number: int) -> str:
        preview_file = preview_output_map.get((item_context, anchor_repo, anchor_pr_number))
        if preview_file is None:
            return ""
        return _relative_href(from_file=index_file, to_file=preview_file)

    def environment_detail_href_builder(item_context: str, instance_name: str) -> str:
        environment_file = environment_output_map.get((item_context, instance_name))
        if environment_file is None:
            return ""
        return _relative_href(from_file=index_file, to_file=environment_file)

    def promotion_detail_href_builder(item_context: str) -> str:
        promotion_file = promotion_output_map.get(item_context)
        if promotion_file is None:
            return ""
        return _relative_href(from_file=index_file, to_file=promotion_file)

    index_nav_links = {"overview": "index.html", "policy": "policy.html"}
    first_detail_file = next(iter(environment_output_map.values()), None)
    if first_detail_file is None:
        first_detail_file = next(iter(promotion_output_map.values()), None)
    if first_detail_file is None:
        first_detail_file = next(iter(preview_output_map.values()), None)
    if first_detail_file is not None:
        index_nav_links["detail"] = _relative_href(from_file=index_file, to_file=first_detail_file)

    index_html = _render_launchplane_preview_index_page_html(
        inventory_payload,
        tenant_payload=tenant_payload,
        detail_href_builder=detail_href_builder,
        environment_detail_href_builder=environment_detail_href_builder,
        promotion_detail_href_builder=promotion_detail_href_builder,
        nav_links=index_nav_links,
    )
    index_file.write_text(index_html, encoding="utf-8")

    policy_nav_links = {
        "overview": _relative_href(from_file=policy_file, to_file=index_file),
        "policy": "policy.html",
    }
    if first_detail_file is not None:
        policy_nav_links["detail"] = _relative_href(
            from_file=policy_file, to_file=first_detail_file
        )
    policy_html = _render_launchplane_preview_policy_page_html(
        inventory_payload,
        nav_links=policy_nav_links,
    )
    policy_file.write_text(policy_html, encoding="utf-8")

    if isinstance(tenant_promotion_detail, dict):
        for item_context, detail_file in promotion_output_map.items():
            detail_file.parent.mkdir(parents=True, exist_ok=True)
            detail_nav_links = {
                "overview": _relative_href(from_file=detail_file, to_file=index_file),
                "policy": _relative_href(from_file=detail_file, to_file=policy_file),
                "detail": detail_file.name,
            }
            detail_html = _render_launchplane_promotion_status_page_html(
                tenant_promotion_detail,
                nav_links=detail_nav_links,
            )
            detail_file.write_text(detail_html, encoding="utf-8")

    if isinstance(tenant_environments, dict):
        for item_context, instance_name in environment_output_map:
            environment_payload = tenant_environments.get(instance_name)
            if not isinstance(environment_payload, dict):
                continue
            action_payload = None
            if isinstance(tenant_environment_actions, dict):
                candidate_action_payload = tenant_environment_actions.get(instance_name)
                if isinstance(candidate_action_payload, dict):
                    action_payload = candidate_action_payload
            detail_file = environment_output_map[(item_context, instance_name)]
            detail_file.parent.mkdir(parents=True, exist_ok=True)
            detail_nav_links = {
                "overview": _relative_href(from_file=detail_file, to_file=index_file),
                "policy": _relative_href(from_file=detail_file, to_file=policy_file),
                "detail": detail_file.name,
            }
            detail_html = _render_launchplane_environment_status_page_html(
                environment_payload,
                action_payload=action_payload,
                nav_links=detail_nav_links,
            )
            detail_file.write_text(detail_html, encoding="utf-8")

    for item_context, anchor_repo, anchor_pr_number in preview_output_map:
        status_payload = _require_launchplane_preview_status_payload(
            state_dir=state_dir,
            context_name=item_context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        )
        detail_file = preview_output_map[(item_context, anchor_repo, anchor_pr_number)]
        detail_file.parent.mkdir(parents=True, exist_ok=True)
        detail_nav_links = {
            "overview": _relative_href(from_file=detail_file, to_file=index_file),
            "policy": _relative_href(from_file=detail_file, to_file=policy_file),
            "detail": detail_file.name,
        }
        detail_html = _render_launchplane_preview_status_page_html(
            status_payload,
            nav_links=detail_nav_links,
        )
        detail_file.write_text(detail_html, encoding="utf-8")


def _load_json_file(input_file: Path) -> dict[str, object]:
    payload = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise click.ClickException(f"{input_file} must contain a JSON object.")
    return payload


def _post_launchplane_service_json(
    *,
    service_url: str,
    path: str,
    payload: dict[str, object],
    bearer_token: str = "",
    session_cookie: str = "",
    idempotency_key: str = "",
) -> dict[str, object]:
    if bool(bearer_token.strip()) == bool(session_cookie.strip()):
        raise click.ClickException("Provide exactly one of bearer token or session cookie.")
    normalized_service_url = service_url.strip().rstrip("/")
    if not normalized_service_url:
        raise click.ClickException("Launchplane service URL is required.")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if bearer_token.strip():
        headers["Authorization"] = f"Bearer {bearer_token.strip()}"
    else:
        headers["Cookie"] = session_cookie.strip()
    if idempotency_key.strip():
        headers["Idempotency-Key"] = idempotency_key.strip()
    request = Request(
        f"{normalized_service_url}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(response_text)
        except JSONDecodeError:
            error_payload = {"error": {"message": response_text.strip()}}
        error_detail = error_payload.get("error") if isinstance(error_payload, dict) else None
        error_message = error_detail.get("message", "") if isinstance(error_detail, dict) else ""
        message = str(error_message or f"Launchplane service returned HTTP {error.code}.")
        raise click.ClickException(message) from error
    except (URLError, TimeoutError) as error:
        raise click.ClickException(f"Launchplane service request failed: {error}") from error
    except JSONDecodeError as error:
        raise click.ClickException("Launchplane service response was not valid JSON.") from error
    if not isinstance(response_payload, dict):
        raise click.ClickException("Launchplane service response was not a JSON object.")
    return cast(dict[str, object], response_payload)


def _load_github_webhook_json_bytes(
    raw_payload_bytes: bytes,
    *,
    description: str = "GitHub webhook payload",
) -> dict[str, object]:
    try:
        webhook_payload = json.loads(raw_payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise click.ClickException(f"{description} must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(webhook_payload, dict):
        raise click.ClickException(f"{description} must decode to a JSON object.")
    return webhook_payload


def _wait_for_ship_healthcheck(*, url: str, timeout_seconds: int) -> None:
    promotion_ship_execution.wait_for_ship_healthcheck(
        url=url,
        timeout_seconds=timeout_seconds,
        sleep=time.sleep,
        monotonic=time.monotonic,
    )


def _verify_ship_healthchecks(*, request: ShipRequest) -> None:
    promotion_ship_execution.verify_ship_healthchecks(
        request=request,
        wait_for_healthcheck=_wait_for_ship_healthcheck,
    )


def _verify_healthcheck_urls(*, health_urls: tuple[str, ...], timeout_seconds: int) -> None:
    if not health_urls:
        raise click.ClickException("At least one Launchplane service health URL is required.")
    if timeout_seconds <= 0:
        raise click.ClickException(
            "Launchplane service health timeout must be greater than zero seconds."
        )
    healthcheck_errors: list[str] = []
    for healthcheck_url in health_urls:
        try:
            _wait_for_ship_healthcheck(url=healthcheck_url, timeout_seconds=timeout_seconds)
            return
        except click.ClickException as error:
            healthcheck_errors.append(str(error))
    raise click.ClickException(
        "Launchplane service health verification failed for all configured URLs:\n"
        + "\n".join(healthcheck_errors)
    )


def _launchplane_service_env_key_present(*, env_map: dict[str, str], env_key: str) -> bool:
    return bool(env_map.get(env_key, "").strip())


def _launchplane_service_env_alias_value(
    *, env_map: dict[str, str], env_keys: tuple[str, ...]
) -> str:
    for env_key in env_keys:
        configured_value = str(env_map.get(env_key, "")).strip()
        if configured_value:
            return configured_value
    return ""


def _launchplane_service_env_alias_present(
    *, env_map: dict[str, str], env_keys: tuple[str, ...]
) -> bool:
    return bool(_launchplane_service_env_alias_value(env_map=env_map, env_keys=env_keys))


def _launchplane_service_any_env_keys_present(
    *, env_map: dict[str, str], env_keys: tuple[str, ...]
) -> bool:
    return any(
        _launchplane_service_env_key_present(env_map=env_map, env_key=env_key)
        for env_key in env_keys
    )


def _launchplane_present_env_keys(
    *, env_map: dict[str, str], env_keys: tuple[str, ...]
) -> list[str]:
    return [
        env_key
        for env_key in env_keys
        if _launchplane_service_env_key_present(env_map=env_map, env_key=env_key)
    ]


def _launchplane_path_payload(path: Path, *, kind: str) -> dict[str, object]:
    return {"path": str(path), "exists": path.exists(), "kind": kind}


def _inspect_local_launchplane_config_boundary(*, control_plane_root: Path) -> dict[str, object]:
    env_map = dict(os.environ)
    database_url = _launchplane_service_env_alias_value(
        env_map=env_map, env_keys=_DATABASE_URL_ENV_KEYS
    )
    managed_store_status = _launchplane_managed_store_status(database_url=database_url)
    managed_secret_bindings = managed_store_status.get("dokploy_secret_bindings", {})
    assert isinstance(managed_secret_bindings, dict)

    repo_env_file = control_plane_root / ".env"
    repo_runtime_env_file = (
        control_plane_root / control_plane_runtime_environments.DEFAULT_RUNTIME_ENVIRONMENTS_FILE
    )
    repo_dokploy_source_file = (
        control_plane_root / control_plane_dokploy.DEFAULT_CONTROL_PLANE_DOKPLOY_SOURCE_FILE
    )
    repo_target_ids_file = (
        control_plane_root / control_plane_dokploy.DEFAULT_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE
    )

    runtime_environment_record_count = _int_from_json_value(
        managed_store_status.get("runtime_environment_record_count")
    )
    dokploy_target_record_count = _int_from_json_value(
        managed_store_status.get("dokploy_target_record_count")
    )
    dokploy_target_id_record_count = _int_from_json_value(
        managed_store_status.get("dokploy_target_id_record_count")
    )
    release_tuple_record_count = _int_from_json_value(
        managed_store_status.get("release_tuple_record_count")
    )
    authz_policy_record_count = _int_from_json_value(
        managed_store_status.get("authz_policy_record_count")
    )
    dokploy_host_managed = bool(managed_secret_bindings.get("DOKPLOY_HOST"))
    dokploy_token_managed = bool(managed_secret_bindings.get("DOKPLOY_TOKEN"))
    dokploy_managed_complete = dokploy_host_managed and dokploy_token_managed
    if dokploy_managed_complete:
        dokploy_credentials_status = "db_only"
    else:
        dokploy_credentials_status = "missing"

    return {
        "control_plane_root": str(control_plane_root),
        "bootstrap": {
            "database_url_present": bool(database_url),
            "master_encryption_key_present": _launchplane_service_any_env_keys_present(
                env_map=env_map,
                env_keys=_MASTER_ENCRYPTION_KEY_ENV_KEYS,
            ),
            "policy_present": _launchplane_service_any_env_keys_present(
                env_map=env_map,
                env_keys=_LAUNCHPLANE_SERVICE_POLICY_ENV_KEYS,
            ),
            "docker_image_reference_present": _launchplane_service_env_key_present(
                env_map=env_map,
                env_key=ARTIFACT_IMAGE_REFERENCE_ENV_KEY,
            ),
            "service_process_env": {
                "LAUNCHPLANE_SERVICE_HOST": _launchplane_service_env_key_present(
                    env_map=env_map,
                    env_key="LAUNCHPLANE_SERVICE_HOST",
                ),
                "LAUNCHPLANE_SERVICE_PORT": _launchplane_service_env_key_present(
                    env_map=env_map,
                    env_key="LAUNCHPLANE_SERVICE_PORT",
                ),
                "LAUNCHPLANE_SERVICE_AUDIENCE": _launchplane_service_env_key_present(
                    env_map=env_map,
                    env_key="LAUNCHPLANE_SERVICE_AUDIENCE",
                ),
                "LAUNCHPLANE_STATE_DIR": _launchplane_service_env_key_present(
                    env_map=env_map,
                    env_key="LAUNCHPLANE_STATE_DIR",
                ),
                "LAUNCHPLANE_APP_ROOT": _launchplane_service_env_key_present(
                    env_map=env_map,
                    env_key="LAUNCHPLANE_APP_ROOT",
                ),
            },
        },
        "db": {
            "configured": bool(database_url),
            "inspectable": bool(managed_store_status.get("inspectable")),
            "error": str(managed_store_status.get("error") or ""),
            "managed_dokploy_secret_bindings": {
                "DOKPLOY_HOST": dokploy_host_managed,
                "DOKPLOY_TOKEN": dokploy_token_managed,
            },
            "runtime_environment_record_count": runtime_environment_record_count,
            "dokploy_target_record_count": dokploy_target_record_count,
            "dokploy_target_id_record_count": dokploy_target_id_record_count,
            "release_tuple_record_count": release_tuple_record_count,
            "authz_policy_record_count": authz_policy_record_count,
        },
        "legacy_paths": {
            "repo_env_file": _launchplane_path_payload(repo_env_file, kind="repo_file"),
            "repo_runtime_environments_file": _launchplane_path_payload(
                repo_runtime_env_file,
                kind="repo_file",
            ),
            "repo_dokploy_source_file": _launchplane_path_payload(
                repo_dokploy_source_file,
                kind="repo_file",
            ),
            "repo_dokploy_target_ids_file": _launchplane_path_payload(
                repo_target_ids_file,
                kind="repo_file",
            ),
        },
        "authority": {
            "dokploy_credentials": dokploy_credentials_status,
            "runtime_environments": "db_only"
            if runtime_environment_record_count > 0
            else "missing",
            "dokploy_target_ids": "db_only" if dokploy_target_id_record_count > 0 else "missing",
            "stable_targets": "db_only" if dokploy_target_record_count > 0 else "missing",
            "release_tuples_catalog": "db_only" if release_tuple_record_count > 0 else "missing",
            "authz_policy": "db_only" if authz_policy_record_count > 0 else "bootstrap_only",
        },
        "transition_inputs": {
            "selector_env_keys_present": [],
            "payload_env_keys_present": [],
        },
    }


def _launchplane_managed_store_status(*, database_url: str) -> dict[str, object]:
    if not database_url.strip():
        return {
            "inspectable": False,
            "error": "",
            "dokploy_secret_bindings": {},
            "dokploy_target_record_count": 0,
            "runtime_environment_record_count": 0,
            "dokploy_target_id_record_count": 0,
            "release_tuple_record_count": 0,
            "authz_policy_record_count": 0,
        }
    record_store: PostgresRecordStore | None = None
    try:
        record_store = PostgresRecordStore(database_url=database_url)
        record_store.ensure_schema()
        dokploy_bindings = {
            binding.binding_key: binding
            for binding in record_store.list_secret_bindings(integration="dokploy", limit=None)
            if binding.status == control_plane_secrets.SECRET_STATUS_CONFIGURED
        }
        runtime_environment_records = record_store.list_runtime_environment_records()
        dokploy_target_id_records = record_store.list_dokploy_target_id_records()
        return {
            "inspectable": True,
            "error": "",
            "dokploy_secret_bindings": {
                "DOKPLOY_HOST": "DOKPLOY_HOST" in dokploy_bindings,
                "DOKPLOY_TOKEN": "DOKPLOY_TOKEN" in dokploy_bindings,
            },
            "dokploy_target_record_count": len(record_store.list_dokploy_target_records()),
            "runtime_environment_record_count": len(runtime_environment_records),
            "dokploy_target_id_record_count": len(dokploy_target_id_records),
            "release_tuple_record_count": len(record_store.list_release_tuple_records()),
            "authz_policy_record_count": len(
                record_store.list_authz_policy_records(status="active")
            ),
        }
    except Exception as error:
        return {
            "inspectable": False,
            "error": str(error),
            "dokploy_secret_bindings": {},
            "dokploy_target_record_count": 0,
            "runtime_environment_record_count": 0,
            "dokploy_target_id_record_count": 0,
            "release_tuple_record_count": 0,
            "authz_policy_record_count": 0,
        }
    finally:
        if record_store is not None:
            try:
                record_store.close()
            except Exception:
                pass


def _launchplane_database_url_parts(database_url: str) -> tuple[str, str]:
    if not database_url:
        return "", ""
    parsed_database_url = urlsplit(database_url)
    return parsed_database_url.scheme.strip().lower(), (parsed_database_url.hostname or "").strip()


def _launchplane_service_git_access_mode(custom_git_url: str) -> str:
    if custom_git_url.startswith("git@"):
        return "ssh"
    if custom_git_url.startswith("https://") or custom_git_url.startswith("http://"):
        return "https"
    return ""


def _build_launchplane_service_runtime_contract(
    *,
    env_map: dict[str, str],
    database_url: str,
    database_scheme: str,
    database_host: str,
    managed_store_status: Mapping[str, object],
) -> dict[str, object]:
    managed_secret_bindings = managed_store_status.get("dokploy_secret_bindings", {})
    assert isinstance(managed_secret_bindings, dict)
    authz_policy_record_count = _int_from_json_value(
        managed_store_status.get("authz_policy_record_count")
    )
    dokploy_target_id_record_count = _int_from_json_value(
        managed_store_status.get("dokploy_target_id_record_count")
    )
    runtime_environment_record_count = _int_from_json_value(
        managed_store_status.get("runtime_environment_record_count")
    )
    return {
        "database_url_present": bool(database_url),
        "database_scheme": database_scheme,
        "database_host": database_host,
        "managed_store_inspectable": bool(managed_store_status.get("inspectable")),
        "managed_store_error": str(managed_store_status.get("error") or ""),
        "master_encryption_key_present": _launchplane_service_env_alias_present(
            env_map=env_map,
            env_keys=_MASTER_ENCRYPTION_KEY_ENV_KEYS,
        ),
        "dokploy_host_present": _launchplane_service_env_key_present(
            env_map=env_map,
            env_key="DOKPLOY_HOST",
        ),
        "dokploy_token_present": _launchplane_service_env_key_present(
            env_map=env_map,
            env_key="DOKPLOY_TOKEN",
        ),
        "dokploy_managed_host_present": bool(managed_secret_bindings.get("DOKPLOY_HOST")),
        "dokploy_managed_token_present": bool(managed_secret_bindings.get("DOKPLOY_TOKEN")),
        "policy_configured": _launchplane_service_any_env_keys_present(
            env_map=env_map,
            env_keys=_LAUNCHPLANE_SERVICE_POLICY_ENV_KEYS,
        ),
        "authz_policy_record_count": authz_policy_record_count,
        "authz_policy_db_configured": authz_policy_record_count > 0,
        "target_ids_env_configured": False,
        "target_ids_record_count": dokploy_target_id_record_count,
        "target_ids_configured": dokploy_target_id_record_count > 0,
        "runtime_environments_env_configured": False,
        "runtime_environment_record_count": runtime_environment_record_count,
        "runtime_environments_configured": runtime_environment_record_count > 0,
        "docker_image_reference_present": _launchplane_service_env_key_present(
            env_map=env_map,
            env_key=ARTIFACT_IMAGE_REFERENCE_ENV_KEY,
        ),
    }


def _launchplane_service_preflight_blockers(
    *,
    target_type: str,
    source_type: str,
    custom_git_url: str,
    custom_git_branch: str,
    custom_git_ssh_key_id: str,
    compose_path: str,
    git_access_mode: str,
    runtime_contract: Mapping[str, object],
) -> list[str]:
    blockers: list[str] = []
    if target_type == "compose" and not compose_path:
        blockers.append("Dokploy compose target is missing composePath.")
    if source_type == "git" and not custom_git_url:
        blockers.append("Dokploy target uses sourceType=git but is missing customGitUrl.")
    if source_type == "git" and not custom_git_branch:
        blockers.append("Dokploy target uses sourceType=git but is missing customGitBranch.")
    if git_access_mode == "ssh" and not custom_git_ssh_key_id:
        blockers.append(
            "Dokploy target uses an SSH git remote but has no customGitSSHKeyId configured."
        )
    if not runtime_contract["database_url_present"]:
        blockers.append("Launchplane service target is missing LAUNCHPLANE_DATABASE_URL.")
    elif not str(runtime_contract["database_scheme"]).startswith("postgresql"):
        blockers.append("Launchplane service target database URL is not a PostgreSQL URL.")
    elif not runtime_contract["database_host"]:
        blockers.append("Launchplane service target database URL is missing a database host.")

    if not runtime_contract["master_encryption_key_present"]:
        blockers.append("Launchplane service target is missing LAUNCHPLANE_MASTER_ENCRYPTION_KEY.")
    if runtime_contract["managed_store_inspectable"]:
        if not runtime_contract["dokploy_managed_host_present"]:
            blockers.append(
                "Launchplane service runtime store is missing managed Dokploy binding DOKPLOY_HOST."
            )
        if not runtime_contract["dokploy_managed_token_present"]:
            blockers.append(
                "Launchplane service runtime store is missing managed Dokploy binding DOKPLOY_TOKEN."
            )

    if not runtime_contract["policy_configured"]:
        blockers.append(
            "Launchplane service target is missing LAUNCHPLANE_POLICY_* or LAUNCHPLANE_POLICY_FILE. "
            "Startup fails closed without an explicit policy input."
        )
    return blockers


def _launchplane_service_preflight_warnings(
    *,
    compose_status: str,
    runtime_contract: Mapping[str, object],
) -> list[str]:
    warnings: list[str] = []
    if not runtime_contract["managed_store_inspectable"]:
        warnings.append(
            "Launchplane service managed-store inspection was unavailable. Confirm the shared store has configured Dokploy bindings for DOKPLOY_HOST and DOKPLOY_TOKEN."
        )
    if runtime_contract["dokploy_host_present"] or runtime_contract["dokploy_token_present"]:
        warnings.append(
            "Launchplane service target still exposes legacy Dokploy credentials in target env. Remove DOKPLOY_HOST and DOKPLOY_TOKEN after confirming the shared store bindings."
        )

    if not runtime_contract["authz_policy_db_configured"]:
        warnings.append(
            "Launchplane service target has no confirmed DB-backed authz policy records. "
            "The service will seed from bootstrap policy on startup until DB-backed policy exists."
        )
    if not runtime_contract["target_ids_configured"]:
        warnings.append(
            "Launchplane service target has no confirmed DB-backed Dokploy target-id records. "
            "Live route resolution will fail closed until those records exist."
        )
    if not runtime_contract["runtime_environments_configured"]:
        warnings.append(
            "Launchplane service target has no confirmed DB-backed runtime-environment records. "
            "Environment resolution will fail closed until those records exist."
        )
    if not runtime_contract["docker_image_reference_present"]:
        warnings.append(
            "Launchplane service target does not currently define DOCKER_IMAGE_REFERENCE. "
            "The first deploy cannot capture a previous image reference for rollback."
        )
    normalized_compose_status = compose_status.lower()
    if normalized_compose_status and normalized_compose_status not in _SUCCESSFUL_DOKPLOY_STATUSES:
        warnings.append(
            f"Dokploy target currently reports composeStatus={compose_status!r}; investigate the live target before replacing it."
        )
    return warnings


def _build_launchplane_service_target_preflight(
    *,
    target_type: str,
    target_id: str,
    target_payload: Mapping[str, object],
) -> dict[str, object]:
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    source_type = str(target_payload.get("sourceType") or "").strip()
    custom_git_url = str(target_payload.get("customGitUrl") or "").strip()
    custom_git_branch = str(target_payload.get("customGitBranch") or "").strip()
    custom_git_ssh_key_id = str(target_payload.get("customGitSSHKeyId") or "").strip()
    compose_path = str(target_payload.get("composePath") or "").strip()
    compose_status = str(target_payload.get("composeStatus") or "").strip()
    database_url = _launchplane_service_env_alias_value(
        env_map=env_map, env_keys=_DATABASE_URL_ENV_KEYS
    )
    database_scheme, database_host = _launchplane_database_url_parts(database_url)
    managed_store_status = _launchplane_managed_store_status(database_url=database_url)
    git_access_mode = _launchplane_service_git_access_mode(custom_git_url)
    runtime_contract = _build_launchplane_service_runtime_contract(
        env_map=env_map,
        database_url=database_url,
        database_scheme=database_scheme,
        database_host=database_host,
        managed_store_status=managed_store_status,
    )
    blockers = _launchplane_service_preflight_blockers(
        target_type=target_type,
        source_type=source_type,
        custom_git_url=custom_git_url,
        custom_git_branch=custom_git_branch,
        custom_git_ssh_key_id=custom_git_ssh_key_id,
        compose_path=compose_path,
        git_access_mode=git_access_mode,
        runtime_contract=runtime_contract,
    )
    warnings = _launchplane_service_preflight_warnings(
        compose_status=compose_status,
        runtime_contract=runtime_contract,
    )

    return {
        "target_id": target_id,
        "target_type": target_type,
        "target_name": str(target_payload.get("name") or "").strip(),
        "app_name": str(target_payload.get("appName") or "").strip(),
        "compose_status": compose_status,
        "source_type": source_type,
        "custom_git_url": custom_git_url,
        "custom_git_branch": custom_git_branch,
        "custom_git_ssh_key_configured": bool(custom_git_ssh_key_id),
        "git_access_mode": git_access_mode,
        "compose_path": compose_path,
        "runtime_contract": runtime_contract,
        "blockers": blockers,
        "warnings": warnings,
    }


def _inspect_launchplane_service_dokploy_target(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
) -> tuple[control_plane_dokploy.JsonObject, dict[str, object]]:
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    preflight_payload = _build_launchplane_service_target_preflight(
        target_type=target_type,
        target_id=target_id,
        target_payload=target_payload,
    )
    return target_payload, preflight_payload


def _launchplane_service_target_preflight_error_message(
    *, preflight_payload: dict[str, object]
) -> str:
    blockers = preflight_payload.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        return "Launchplane service Dokploy target preflight failed."
    rendered_blockers = "\n".join(f"- {str(blocker)}" for blocker in blockers)
    return f"Launchplane service Dokploy target preflight failed:\n{rendered_blockers}"


def _apply_dokploy_image_reference(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    image_reference: str,
    target_payload: control_plane_dokploy.JsonObject | None = None,
) -> dict[str, object]:
    normalized_image_reference = image_reference.strip()
    if not normalized_image_reference:
        raise click.ClickException(
            "Launchplane service deploy requires a non-empty image reference."
        )
    resolved_target_payload = target_payload or control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    raw_env_text = str(resolved_target_payload.get("env") or "")
    env_map = control_plane_dokploy.parse_dokploy_env_text(raw_env_text)
    previous_value_present = ARTIFACT_IMAGE_REFERENCE_ENV_KEY in env_map
    previous_image_reference = env_map.get(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, "")
    updated_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
        raw_env_text,
        updates={ARTIFACT_IMAGE_REFERENCE_ENV_KEY: normalized_image_reference},
    )
    if updated_env_text != raw_env_text:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            target_payload=resolved_target_payload,
            env_text=updated_env_text,
        )
    return {
        "previous_image_reference": previous_image_reference,
        "previous_image_reference_present": previous_value_present,
        "image_reference_changed": updated_env_text != raw_env_text,
    }


def _restore_dokploy_image_reference(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    image_reference: str,
    value_present: bool,
) -> dict[str, object]:
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    raw_env_text = str(target_payload.get("env") or "")
    if value_present:
        restored_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
            raw_env_text,
            updates={ARTIFACT_IMAGE_REFERENCE_ENV_KEY: image_reference},
        )
    else:
        restored_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
            raw_env_text,
            removals=(ARTIFACT_IMAGE_REFERENCE_ENV_KEY,),
        )
    if restored_env_text != raw_env_text:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            target_payload=target_payload,
            env_text=restored_env_text,
        )
    return {"image_reference_changed": restored_env_text != raw_env_text}


def _trigger_and_wait_for_dokploy_target_deploy(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    deploy_timeout_seconds: int,
    no_cache: bool,
) -> dict[str, str]:
    return control_plane_live_target_runtime.trigger_and_wait_for_dokploy_target_deploy(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        deploy_timeout_seconds=deploy_timeout_seconds,
        no_cache=no_cache,
    )


def _resolve_dokploy_target(
    *,
    request: ShipRequest,
) -> tuple[ResolvedTargetEvidence, int]:
    control_plane_root = _control_plane_root()
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"No DB-backed Dokploy target definition found for {request.context}/{request.instance}."
        )
    if target_definition.target_type != request.target_type:
        raise click.ClickException(
            "Ship request target_type does not match the DB-backed tracked Dokploy target. "
            f"Request={request.target_type} configured={target_definition.target_type}."
        )
    resolved_target = ResolvedTargetEvidence(
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        target_name=target_definition.target_name.strip() or request.target_name,
    )
    deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
        timeout_override_seconds=request.timeout_seconds,
        target_definition=target_definition,
    )
    return resolved_target, deploy_timeout_seconds


def _resolve_deploy_mode(*, configured_ship_mode: str, target_type: str) -> str:
    return promotion_ship_resolution.resolve_deploy_mode(
        configured_ship_mode=configured_ship_mode,
        target_type=target_type,
    )


def _load_runtime_environment_values(*, context_name: str, instance_name: str) -> dict[str, str]:
    try:
        return control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=_control_plane_root(),
            context_name=context_name,
            instance_name=instance_name,
        )
    except click.ClickException as error:
        if str(error).startswith("Missing control-plane runtime environments file."):
            return {}
        raise


def _require_dokploy_target_definition(
    *,
    source_of_truth: control_plane_dokploy.DokploySourceOfTruth,
    context_name: str,
    instance_name: str,
    operation_name: str,
) -> control_plane_dokploy.DokployTargetDefinition:
    return promotion_ship_resolution.require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name=operation_name,
    )


def _resolve_native_ship_request(
    *,
    context_name: str,
    instance_name: str,
    artifact_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> ShipRequest:
    return promotion_ship_resolution.resolve_native_ship_request(
        control_plane_root=_control_plane_root(),
        context_name=context_name,
        instance_name=instance_name,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        wait=wait,
        timeout_override_seconds=timeout_override_seconds,
        verify_health=verify_health,
        health_timeout_override_seconds=health_timeout_override_seconds,
        dry_run=dry_run,
        no_cache=no_cache,
        allow_dirty=allow_dirty,
        environment_values=_load_runtime_environment_values(
            context_name=context_name,
            instance_name=instance_name,
        ),
    )


def _resolve_ship_request_for_promotion(
    *,
    request: PromotionRequest,
) -> ShipRequest:
    return promotion_ship_resolution.resolve_ship_request_for_promotion(
        control_plane_root=_control_plane_root(),
        request=request,
        environment_values=_load_runtime_environment_values(
            context_name=request.context,
            instance_name=request.to_instance,
        ),
    )


def _resolve_native_promotion_request(
    *,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    artifact_id: str,
    backup_record_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> PromotionRequest:
    destination_environment_values = _load_runtime_environment_values(
        context_name=context_name,
        instance_name=to_instance_name,
    )
    return promotion_ship_resolution.resolve_native_promotion_request(
        control_plane_root=_control_plane_root(),
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        artifact_id=artifact_id,
        backup_record_id=backup_record_id,
        source_git_ref=source_git_ref,
        wait=wait,
        timeout_override_seconds=timeout_override_seconds,
        verify_health=verify_health,
        health_timeout_override_seconds=health_timeout_override_seconds,
        dry_run=dry_run,
        no_cache=no_cache,
        allow_dirty=allow_dirty,
        source_environment_values=destination_environment_values,
        destination_environment_values=destination_environment_values,
    )


def _execute_dokploy_deploy(
    *,
    request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
) -> None:
    control_plane_root = _control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    promotion_ship_execution.execute_dokploy_deploy(
        host=host,
        token=token,
        request=request,
        resolved_target=resolved_target,
        deploy_timeout_seconds=deploy_timeout_seconds,
    )


def _run_compose_post_deploy_update(
    *,
    env_file: Path | None,
    request: ShipRequest,
) -> None:
    control_plane_root = _control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"Compose post-deploy update target {request.context}/{request.instance} is missing from the control-plane Dokploy source-of-truth."
        )
    if target_definition.target_type != "compose":
        raise click.ClickException(
            "Compose post-deploy update requires a compose target in the control-plane Dokploy source-of-truth. "
            f"Configured={target_definition.target_type}."
        )
    odoo_override_record = _read_odoo_instance_override_record_for_post_deploy(
        context_name=request.context,
        instance_name=request.instance,
    )
    workflow_environment_overrides: dict[str, str] = {}
    required_workflow_environment_keys: tuple[str, ...] = ()
    protected_shopify_store_keys = (
        control_plane_dokploy.protected_shopify_store_keys_for_target_definition(target_definition)
    )
    if odoo_override_record is not None and "deploy" in odoo_override_record.apply_on:
        try:
            post_deploy_environment = (
                control_plane_odoo_instance_overrides.build_post_deploy_environment(
                    odoo_override_record,
                    protected_shopify_store_keys=protected_shopify_store_keys,
                )
            )
            workflow_environment_overrides = post_deploy_environment.inline_environment
            required_workflow_environment_keys = (
                post_deploy_environment.required_container_environment_keys
            )
        except click.ClickException as error:
            _write_odoo_instance_override_apply_result(
                record=odoo_override_record,
                status="fail",
                detail=str(error),
            )
            raise
    try:
        control_plane_dokploy.run_compose_post_deploy_update(
            host=host,
            token=token,
            target_definition=target_definition,
            env_file=env_file,
            workflow_environment_overrides=workflow_environment_overrides,
            required_workflow_environment_keys=required_workflow_environment_keys,
            protected_shopify_store_keys=protected_shopify_store_keys,
        )
    except click.ClickException as error:
        if odoo_override_record is not None:
            _write_odoo_instance_override_apply_result(
                record=odoo_override_record,
                status="fail",
                detail=str(error),
            )
        raise
    if odoo_override_record is not None:
        _write_odoo_instance_override_apply_result(
            record=odoo_override_record,
            status="pass"
            if workflow_environment_overrides or required_workflow_environment_keys
            else "skipped",
            detail=(
                "Applied Odoo instance overrides through the compose post-deploy workflow."
                if workflow_environment_overrides or required_workflow_environment_keys
                else "No deploy-phase Odoo instance overrides were rendered for the compose post-deploy workflow."
            ),
        )


def _read_odoo_instance_override_record_for_post_deploy(
    *,
    context_name: str,
    instance_name: str,
) -> OdooInstanceOverrideRecord | None:
    database_url = resolve_database_url(None)
    if database_url is None:
        return None
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        return postgres_store.read_odoo_instance_override_record(
            context_name=context_name.strip().lower(),
            instance_name=instance_name.strip().lower(),
        )
    except FileNotFoundError:
        return None
    finally:
        postgres_store.close()


def _write_odoo_instance_override_apply_result(
    *,
    record: OdooInstanceOverrideRecord,
    status: Literal["skipped", "pending", "pass", "fail"],
    detail: str,
) -> None:
    database_url = resolve_database_url(None)
    if database_url is None:
        return
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        updated_record = record.model_copy(
            update={
                "last_apply": OdooOverrideApplyResult(
                    attempted=status in {"pending", "pass", "fail"},
                    status=status,
                    applied_at=utc_now_timestamp() if status in {"pass", "fail"} else "",
                    detail=detail,
                ),
                "updated_at": utc_now_timestamp(),
                "source_label": "odoo-post-deploy-driver",
            }
        )
        postgres_store.write_odoo_instance_override_record(updated_record)
    finally:
        postgres_store.close()


def _skipped_destination_health(
    request: ShipRequest, *, detail_status: str = "skipped"
) -> HealthcheckEvidence:
    return request.destination_health.model_copy(
        update={"verified": False, "status": detail_status}
    )


def _execute_ship(
    *,
    state_dir: Path,
    database_url: str | None = None,
    env_file: Path | None,
    request: ShipRequest,
    mint_release_tuple: bool = True,
) -> tuple[Path | None, DeploymentRecord | ShipRequest]:
    record_store = _store(state_dir, database_url=database_url)
    return promotion_ship_execution.execute_ship(
        record_store=record_store,
        env_file=env_file,
        request=request,
        mint_release_tuple=mint_release_tuple,
        callbacks=promotion_ship_execution.ShipExecutionCallbacks(
            require_artifact_id=_require_artifact_id,
            read_artifact_manifest=_read_artifact_manifest,
            resolve_artifact_native_execution_request=_resolve_artifact_native_execution_request,
            resolve_dokploy_target=_resolve_dokploy_target,
            sync_artifact_image_reference_for_target=_sync_artifact_image_reference_for_target,
            execute_dokploy_deploy=_execute_dokploy_deploy,
            run_compose_post_deploy_update=_run_compose_post_deploy_update,
            skipped_destination_health=_skipped_destination_health,
            verify_ship_healthchecks=_verify_ship_healthchecks,
            write_environment_inventory=_write_environment_inventory,
            write_release_tuple_from_deployment=_write_release_tuple_from_deployment,
        ),
    )


def _require_artifact_id(*, requested_artifact_id: str) -> str:
    normalized_artifact_id = requested_artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("Artifact-backed execution requires an explicit artifact_id.")
    return normalized_artifact_id


def _read_artifact_manifest(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    artifact_id: str,
) -> ArtifactIdentityManifest:
    try:
        return record_store.read_artifact_manifest(artifact_id)
    except FileNotFoundError:
        raise click.ClickException(
            f"Ship requires stored artifact manifest '{artifact_id}'."
        ) from None


def _read_backup_gate_record(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    record_id: str,
) -> BackupGateRecord:
    try:
        return record_store.read_backup_gate_record(record_id)
    except FileNotFoundError:
        raise click.ClickException(
            f"Promotion requires stored backup gate record '{record_id}'."
        ) from None


def _resolve_backup_gate_for_promotion(
    *,
    request: PromotionRequest,
    record_store: FilesystemRecordStore | PostgresRecordStore,
) -> tuple[PromotionRequest, BackupGateRecord | None]:
    if not request.backup_gate.required:
        resolved_request = request.model_copy(
            update={
                "backup_record_id": "",
                "backup_gate": {"required": False, "status": "skipped", "evidence": {}},
            }
        )
        return resolved_request, None

    normalized_record_id = request.backup_record_id.strip()
    if not normalized_record_id:
        raise click.ClickException(
            "Promotion requires backup_record_id when backup gate is required."
        )

    backup_gate_record = _read_backup_gate_record(
        record_store=record_store, record_id=normalized_record_id
    )
    if backup_gate_record.context != request.context:
        raise click.ClickException(
            "Backup gate record context does not match promotion request. "
            f"Record={backup_gate_record.context} request={request.context}."
        )
    if backup_gate_record.instance != request.to_instance:
        raise click.ClickException(
            "Backup gate record instance does not match promotion destination. "
            f"Record={backup_gate_record.instance} request={request.to_instance}."
        )
    if not backup_gate_record.required:
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' is marked required=false and cannot satisfy promotion gating."
        )
    if backup_gate_record.status != "pass":
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' must have status=pass before promotion."
        )

    resolved_request = request.model_copy(
        update={
            "backup_record_id": backup_gate_record.record_id,
            "backup_gate": {
                "required": backup_gate_record.required,
                "status": backup_gate_record.status,
                "evidence": backup_gate_record.evidence,
            },
        }
    )
    return resolved_request, backup_gate_record


def _resolve_artifact_native_execution_request(
    *,
    request: ShipRequest,
    artifact_id: str,
    artifact_manifest: ArtifactIdentityManifest,
) -> ShipRequest:
    if artifact_manifest.artifact_id != artifact_id:
        raise click.ClickException(
            "Artifact manifest id mismatch during ship execution: "
            f"request={artifact_id} manifest={artifact_manifest.artifact_id}."
        )
    return request.model_copy(update={"artifact_id": artifact_id})


def _artifact_image_reference_from_manifest(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _artifact_target_runtime_contract_findings(
    *,
    target_payload: Mapping[str, object],
    env_map: dict[str, str],
) -> dict[str, object]:
    legacy_source_values = []
    for key_name in (
        "customGitUrl",
        "repository",
        "githubRepository",
        "gitlabRepository",
        "giteaRepository",
        "bitbucketRepository",
    ):
        raw_value = target_payload.get(key_name)
        if not isinstance(raw_value, str):
            continue
        normalized_value = raw_value.strip()
        if not normalized_value:
            continue
        if _LEGACY_MONOREPO_MARKER in normalized_value.lower():
            legacy_source_values.append(normalized_value)

    mutable_addon_entries = []
    for env_key in ("ODOO_ADDON_REPOSITORIES", "OPENUPGRADE_ADDON_REPOSITORY"):
        raw_value = env_map.get(env_key, "")
        if not raw_value:
            continue
        for entry in _parse_addon_repository_entries(raw_value):
            _, repo_ref = _split_addon_repository_entry(entry)
            if not repo_ref or not control_plane_release_tuples.GIT_SHA_PATTERN.match(repo_ref):
                mutable_addon_entries.append(entry)

    legacy_source_values = sorted(set(legacy_source_values))
    mutable_addon_entries = sorted(set(mutable_addon_entries))
    blockers = []
    if legacy_source_values:
        blockers.append(
            "Target still references legacy monorepo source(s): " + ", ".join(legacy_source_values)
        )
    if mutable_addon_entries:
        blockers.append("Target still has mutable addon refs: " + ", ".join(mutable_addon_entries))

    return {
        "artifact_ready": not blockers,
        "legacy_monorepo_sources": legacy_source_values,
        "mutable_addon_refs": mutable_addon_entries,
        "blockers": blockers,
    }


def _validate_artifact_target_runtime_contract(
    *,
    artifact_manifest: ArtifactIdentityManifest | None,
    resolved_target: ResolvedTargetEvidence,
    target_payload: Mapping[str, object],
    env_map: dict[str, str],
) -> None:
    if artifact_manifest is None:
        return

    findings = _artifact_target_runtime_contract_findings(
        target_payload=target_payload,
        env_map=env_map,
    )
    legacy_source_values = findings["legacy_monorepo_sources"]
    if isinstance(legacy_source_values, list) and legacy_source_values:
        legacy_sources = ", ".join(legacy_source_values)
        raise click.ClickException(
            "Artifact-backed deploy cannot target a legacy monorepo Dokploy source. "
            f"Target {resolved_target.target_name or resolved_target.target_id} still references {legacy_sources}."
        )

    mutable_addon_entries = findings["mutable_addon_refs"]
    if isinstance(mutable_addon_entries, list) and mutable_addon_entries:
        addon_entry_list = ", ".join(mutable_addon_entries)
        raise click.ClickException(
            "Artifact-backed deploy requires Dokploy addon repositories to use exact git SHAs. "
            f"Target {resolved_target.target_name or resolved_target.target_id} still has mutable addon refs: {addon_entry_list}."
        )


def _parse_addon_repository_entries(raw_value: str) -> tuple[str, ...]:
    normalized_value = raw_value.replace("\n", ",")
    entries = []
    for raw_entry in normalized_value.split(","):
        entry = raw_entry.strip()
        if entry:
            entries.append(entry)
    return tuple(entries)


def _split_addon_repository_entry(entry: str) -> tuple[str, str]:
    normalized_entry = entry.strip()
    if "@" not in normalized_entry:
        return normalized_entry, ""
    repository_name, repository_ref = normalized_entry.rsplit("@", maxsplit=1)
    return repository_name.strip(), repository_ref.strip()


def _runtime_contract_env_payload(env_map: dict[str, str]) -> dict[str, str]:
    return {
        env_key: env_map[env_key]
        for env_key in _RUNTIME_CONTRACT_ENV_KEYS
        if env_key in env_map and env_map[env_key].strip()
    }


def _build_live_target_runtime_contract_payload(
    *,
    context_name: str,
    instance_name: str,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = _require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Live target inspection",
    )
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    findings = _artifact_target_runtime_contract_findings(
        target_payload=target_payload,
        env_map=env_map,
    )
    return {
        "context": context_name,
        "instance": instance_name,
        "tracked_target": {
            "target_id": target_definition.target_id,
            "target_type": target_definition.target_type,
            "target_name": target_definition.target_name,
            "source_git_ref": target_definition.source_git_ref,
        },
        "live_target": {
            "name": str(target_payload.get("name") or "").strip(),
            "app_name": str(target_payload.get("appName") or "").strip(),
            "source_type": str(target_payload.get("sourceType") or "").strip(),
            "custom_git_url": str(target_payload.get("customGitUrl") or "").strip(),
            "custom_git_branch": str(target_payload.get("customGitBranch") or "").strip(),
            "repository": str(target_payload.get("repository") or "").strip(),
            "branch": str(target_payload.get("branch") or "").strip(),
            "compose_path": str(target_payload.get("composePath") or "").strip(),
            "environment": _runtime_contract_env_payload(env_map),
        },
        "artifact_runtime_contract": findings,
    }


def _sync_live_target_from_tracked_contract(
    *,
    context_name: str,
    instance_name: str,
    apply_changes: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = _require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Live target sync",
    )
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_env_map = dict(env_map)
    for env_key, env_value in target_definition.env.items():
        desired_env_map[env_key] = env_value

    source_changes = {}
    tracked_source_fields = {
        "source_type": target_definition.source_type.strip(),
        "custom_git_url": target_definition.custom_git_url.strip(),
        "custom_git_branch": target_definition.custom_git_branch.strip(),
        "compose_path": target_definition.compose_path.strip(),
        "watch_paths": list(target_definition.watch_paths),
        "enable_submodules": target_definition.enable_submodules,
    }
    raw_watch_paths = target_payload.get("watchPaths")
    live_watch_paths = (
        [str(item) for item in raw_watch_paths] if isinstance(raw_watch_paths, list) else []
    )
    live_source_fields = {
        "source_type": str(target_payload.get("sourceType") or "").strip(),
        "custom_git_url": str(target_payload.get("customGitUrl") or "").strip(),
        "custom_git_branch": str(target_payload.get("customGitBranch") or "").strip(),
        "compose_path": str(target_payload.get("composePath") or "").strip(),
        "watch_paths": live_watch_paths,
        "enable_submodules": bool(target_payload.get("enableSubmodules"))
        if target_payload.get("enableSubmodules") is not None
        else None,
    }
    for field_name, desired_value in tracked_source_fields.items():
        if desired_value in ("", (), [], None):
            continue
        if live_source_fields.get(field_name) != desired_value:
            source_changes[field_name] = {
                "live": live_source_fields.get(field_name),
                "tracked": desired_value,
            }

    env_changes = {}
    for env_key, tracked_value in target_definition.env.items():
        live_value = env_map.get(env_key, "")
        if live_value != tracked_value:
            env_changes[env_key] = {"live": live_value, "tracked": tracked_value}

    if apply_changes:
        if source_changes:
            control_plane_dokploy.update_dokploy_target_source(
                host=host,
                token=token,
                target_definition=target_definition,
                target_payload=target_payload,
            )
        if env_changes:
            refreshed_payload = control_plane_dokploy.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
            )
            refreshed_env_map = control_plane_dokploy.parse_dokploy_env_text(
                str(refreshed_payload.get("env") or "")
            )
            for env_key, env_value in target_definition.env.items():
                refreshed_env_map[env_key] = env_value
            control_plane_dokploy.update_dokploy_target_env(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
                target_payload=refreshed_payload,
                env_text=control_plane_dokploy.serialize_dokploy_env_text(refreshed_env_map),
            )

    payload = _build_live_target_runtime_contract_payload(
        context_name=context_name,
        instance_name=instance_name,
    )
    payload["sync_preview"] = {
        "apply_changes": apply_changes,
        "source_changes": source_changes,
        "env_changes": env_changes,
    }
    return payload


def _apply_live_target_runtime_environment(
    *,
    context_name: str,
    instance_name: str,
    apply_changes: bool,
    deploy: bool,
    no_cache: bool,
    deploy_timeout_seconds: int | None,
) -> dict[str, object]:
    try:
        return control_plane_live_target_runtime.apply_live_target_runtime_environment(
            control_plane_root=_control_plane_root(),
            context_name=context_name,
            instance_name=instance_name,
            apply_changes=apply_changes,
            deploy=deploy,
            no_cache=no_cache,
            deploy_timeout_seconds=deploy_timeout_seconds,
            deploy_trigger=_trigger_and_wait_for_dokploy_target_deploy,
        )
    except control_plane_live_target_runtime.LiveTargetRuntimeError as error:
        raise click.ClickException(str(error)) from error


def _sync_artifact_image_reference_for_target(
    *,
    context_name: str,
    instance_name: str,
    artifact_manifest: ArtifactIdentityManifest | None,
    resolved_target: ResolvedTargetEvidence,
) -> dict[str, str]:
    control_plane_root = Path(__file__).resolve().parent.parent
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    runtime_environment_values = (
        control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
        )
    )
    database_url = resolve_database_url(None)
    runtime_key_safety = control_plane_live_target_runtime.skipped_runtime_key_safety_summary()
    if database_url is not None:
        postgres_store = PostgresRecordStore(database_url=database_url)
        postgres_store.ensure_schema()
        try:
            try:
                runtime_key_safety = control_plane_live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync(
                    record_store=postgres_store,
                    context_name=context_name,
                    instance_name=instance_name,
                )
            except control_plane_live_target_runtime.LiveTargetRuntimeError as error:
                raise click.ClickException(str(error)) from error
        finally:
            postgres_store.close()
    desired_env_map = dict(env_map)
    desired_env_map.update(runtime_environment_values)

    desired_image_reference = ""
    if artifact_manifest is not None:
        desired_image_reference = _artifact_image_reference_from_manifest(artifact_manifest)
    if desired_image_reference:
        desired_env_map[ARTIFACT_IMAGE_REFERENCE_ENV_KEY] = desired_image_reference
    else:
        desired_env_map.pop(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, None)

    _validate_artifact_target_runtime_contract(
        artifact_manifest=artifact_manifest,
        resolved_target=resolved_target,
        target_payload=target_payload,
        env_map=desired_env_map,
    )

    runtime_source_evidence: dict[str, str] = {}
    if desired_image_reference and resolved_target.target_type == "compose":
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference=desired_image_reference
        )
        runtime_source_evidence = control_plane_dokploy.sync_dokploy_compose_raw_source(
            host=host,
            token=token,
            compose_id=resolved_target.target_id,
            compose_name=resolved_target.target_name,
            target_payload=target_payload,
            compose_file=compose_file,
        )

    runtime_source_evidence.update(
        _build_runtime_environment_sync_evidence(
            runtime_environment_values=runtime_environment_values,
            changed=desired_env_map != env_map,
        )
    )
    runtime_source_evidence["runtime_key_safety_status"] = str(runtime_key_safety["status"])
    runtime_source_evidence["runtime_key_safety_required"] = (
        "true" if runtime_key_safety["required"] else "false"
    )
    runtime_source_evidence["runtime_key_safety_policy_sha256"] = str(
        runtime_key_safety.get("policy_sha256", "")
    )
    if desired_env_map != env_map:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            target_payload=target_payload,
            env_text=control_plane_dokploy.serialize_dokploy_env_text(desired_env_map),
        )
        target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
        )
    refreshed_env_map = control_plane_dokploy.parse_dokploy_env_text(
        str(target_payload.get("env") or "")
    )
    missing_or_mismatched_keys = sorted(
        env_key
        for env_key, env_value in desired_env_map.items()
        if refreshed_env_map.get(env_key, "") != env_value
    )
    if missing_or_mismatched_keys:
        raise click.ClickException(
            "Dokploy target env did not persist Launchplane DB-backed runtime key(s): "
            + ", ".join(missing_or_mismatched_keys)
        )
    runtime_source_evidence["runtime_env_verified"] = "true"
    return runtime_source_evidence


def _build_runtime_environment_sync_evidence(
    *,
    runtime_environment_values: dict[str, str],
    changed: bool,
) -> dict[str, str]:
    runtime_environment_keys = sorted(runtime_environment_values)
    key_digest = hashlib.sha256("\n".join(runtime_environment_keys).encode("utf-8")).hexdigest()
    return {
        "runtime_env_source": "launchplane-db",
        "runtime_env_key_count": str(len(runtime_environment_keys)),
        "runtime_env_keys_sha256": key_digest,
        "runtime_env_changed": "true" if changed else "false",
    }


def _write_environment_inventory(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    deployment_record: DeploymentRecord,
    promotion_record_id: str = "",
    promoted_from_instance: str = "",
) -> Path | None:
    inventory_record = build_environment_inventory(
        deployment_record=deployment_record,
        updated_at=utc_now_timestamp(),
        promotion_record_id=promotion_record_id,
        promoted_from_instance=promoted_from_instance,
    )
    return record_store.write_environment_inventory(inventory_record)


def _write_environment_inventory_from_promotion(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    promotion_record: PromotionRecord,
) -> Path | None:
    deployment_record_id = promotion_record.deployment_record_id.strip()
    if not deployment_record_id:
        raise click.ClickException(
            "Promotion record is missing deployment_record_id. "
            "Write a promotion record with explicit deployment linkage before refreshing inventory from it."
        )

    deployment_record = record_store.read_deployment_record(deployment_record_id)
    if deployment_record.context != promotion_record.context:
        raise click.ClickException(
            "Promotion record context does not match linked deployment record context."
        )
    if deployment_record.instance != promotion_record.to_instance:
        raise click.ClickException(
            "Promotion destination instance does not match linked deployment record instance."
        )

    promotion_artifact_id = _artifact_id_or_empty(promotion_record.artifact_identity)
    deployment_artifact_id = _artifact_id_or_empty(deployment_record.artifact_identity)
    if (
        promotion_artifact_id
        and deployment_artifact_id
        and promotion_artifact_id != deployment_artifact_id
    ):
        raise click.ClickException(
            "Promotion artifact_id does not match linked deployment record artifact_id."
        )

    return _write_environment_inventory(
        record_store=record_store,
        deployment_record=deployment_record,
        promotion_record_id=promotion_record.record_id,
        promoted_from_instance=promotion_record.from_instance,
    )


def _write_release_tuple_from_deployment(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    deployment_record: DeploymentRecord,
    artifact_manifest: ArtifactIdentityManifest,
) -> Path | None:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        deployment_record.instance
    ):
        return None
    release_tuple = control_plane_release_tuples.build_release_tuple_record_from_artifact_manifest(
        context_name=deployment_record.context,
        channel_name=deployment_record.instance,
        artifact_manifest=artifact_manifest,
        deployment_record_id=deployment_record.record_id,
        minted_at=utc_now_timestamp(),
    )
    return record_store.write_release_tuple_record(release_tuple)


def _read_source_release_tuple_for_promotion(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    request: PromotionRequest,
) -> ReleaseTupleRecord | None:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        request.from_instance
    ):
        return None
    try:
        source_tuple = record_store.read_release_tuple_record(
            context_name=request.context,
            channel_name=request.from_instance,
        )
    except FileNotFoundError:
        raise click.ClickException(
            "Promotion requires a current source release tuple before it can deploy. "
            f"Ship artifact {request.artifact_id} into {request.context}/{request.from_instance} first."
        ) from None
    return control_plane_release_tuples.require_source_release_tuple_for_promotion(
        source_tuple=source_tuple,
        artifact_id=request.artifact_id,
        context_name=request.context,
        from_channel_name=request.from_instance,
    )


def _read_source_release_tuple_for_promotion_record(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    promotion_record: PromotionRecord,
) -> ReleaseTupleRecord:
    artifact_id = _artifact_id_or_empty(promotion_record.artifact_identity)
    if not artifact_id:
        raise click.ClickException(
            "Promotion record is missing artifact_identity.artifact_id required for release tuple promotion."
        )
    try:
        source_tuple = record_store.read_release_tuple_record(
            context_name=promotion_record.context,
            channel_name=promotion_record.from_instance,
        )
    except FileNotFoundError:
        raise click.ClickException(
            "Promotion requires a current source release tuple before Launchplane can mint the destination tuple. "
            f"Write or mint {promotion_record.context}/{promotion_record.from_instance} first."
        ) from None
    return control_plane_release_tuples.require_source_release_tuple_for_promotion(
        source_tuple=source_tuple,
        artifact_id=artifact_id,
        context_name=promotion_record.context,
        from_channel_name=promotion_record.from_instance,
    )


def _write_promoted_release_tuple(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    source_tuple: ReleaseTupleRecord | None,
    deployment_record: DeploymentRecord,
    promotion_record: PromotionRecord,
) -> Path | None:
    if source_tuple is None:
        return None
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        promotion_record.to_instance
    ):
        return None
    promoted_tuple = control_plane_release_tuples.build_promoted_release_tuple_record(
        source_tuple=source_tuple,
        to_channel_name=promotion_record.to_instance,
        deployment_record_id=deployment_record.record_id,
        promotion_record_id=promotion_record.record_id,
        minted_at=utc_now_timestamp(),
    )
    return record_store.write_release_tuple_record(promoted_tuple)


def _artifact_id_or_empty(artifact_identity: object) -> str:
    if artifact_identity is None:
        return ""
    artifact_id = getattr(artifact_identity, "artifact_id", "")
    if isinstance(artifact_id, str):
        return artifact_id
    return ""


def _summarize_backup_gate_record(record: BackupGateRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "context": record.context,
        "instance": record.instance,
        "created_at": record.created_at,
        "source": record.source,
        "required": record.required,
        "status": record.status,
        "evidence": dict(record.evidence),
    }


def _summarize_promotion_record(record: PromotionRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "context": record.context,
        "from_instance": record.from_instance,
        "to_instance": record.to_instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "deployment_record_id": record.deployment_record_id,
        "backup_record_id": record.backup_record_id,
        "backup_status": record.backup_gate.status,
        "deploy_status": record.deploy.status,
        "deployment_id": record.deploy.deployment_id,
        "started_at": record.deploy.started_at,
        "finished_at": record.deploy.finished_at,
        "post_deploy_update_status": record.post_deploy_update.status,
        "source_health_status": record.source_health.status,
        "destination_health_status": record.destination_health.status,
    }


def _summarize_deployment_record(record: DeploymentRecord) -> dict[str, object]:
    target_id = ""
    if record.resolved_target is not None:
        target_id = record.resolved_target.target_id
    return {
        "record_id": record.record_id,
        "context": record.context,
        "instance": record.instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "source_git_ref": record.source_git_ref,
        "target_name": record.deploy.target_name,
        "target_type": record.deploy.target_type,
        "target_id": target_id,
        "deploy_status": record.deploy.status,
        "deployment_id": record.deploy.deployment_id,
        "started_at": record.deploy.started_at,
        "finished_at": record.deploy.finished_at,
        "post_deploy_update_status": record.post_deploy_update.status,
        "destination_health_status": record.destination_health.status,
    }


def _summarize_environment_inventory(record: EnvironmentInventory) -> dict[str, object]:
    return {
        "context": record.context,
        "instance": record.instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "source_git_ref": record.source_git_ref,
        "updated_at": record.updated_at,
        "deployment_record_id": record.deployment_record_id,
        "promotion_record_id": record.promotion_record_id,
        "promoted_from_instance": record.promoted_from_instance,
        "deploy_status": record.deploy.status,
        "post_deploy_update_status": record.post_deploy_update.status,
        "destination_health_status": record.destination_health.status,
    }


def _build_environment_status_payload(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    context_name: str,
    instance_name: str,
) -> dict[str, object]:
    live_inventory = record_store.read_environment_inventory(
        context_name=context_name, instance_name=instance_name
    )
    live_promotion_summary: dict[str, object] | None = None
    authorized_backup_gate_summary: dict[str, object] | None = None

    if live_inventory.promotion_record_id.strip():
        try:
            live_promotion_record = record_store.read_promotion_record(
                live_inventory.promotion_record_id
            )
        except FileNotFoundError:
            raise click.ClickException(
                "Environment inventory references missing promotion record "
                f"'{live_inventory.promotion_record_id}'."
            ) from None
        live_promotion_summary = _summarize_promotion_record(live_promotion_record)
        if live_promotion_record.backup_record_id.strip():
            try:
                live_backup_gate_record = record_store.read_backup_gate_record(
                    live_promotion_record.backup_record_id
                )
            except FileNotFoundError:
                raise click.ClickException(
                    "Promotion record references missing backup gate record "
                    f"'{live_promotion_record.backup_record_id}'."
                ) from None
            authorized_backup_gate_summary = _summarize_backup_gate_record(live_backup_gate_record)

    recent_promotion_records = record_store.list_promotion_records(
        context_name=context_name,
        to_instance_name=instance_name,
        limit=ENVIRONMENT_STATUS_HISTORY_LIMIT,
    )
    recent_deployment_records = record_store.list_deployment_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=ENVIRONMENT_STATUS_HISTORY_LIMIT,
    )
    recent_promotions = tuple(
        _summarize_promotion_record(record) for record in recent_promotion_records
    )
    recent_deployments = tuple(
        _summarize_deployment_record(record) for record in recent_deployment_records
    )
    latest_promotion = recent_promotions[0] if recent_promotions else None
    latest_deployment = recent_deployments[0] if recent_deployments else None

    return {
        "context": context_name,
        "instance": instance_name,
        "live": _summarize_environment_inventory(live_inventory),
        "live_promotion": live_promotion_summary,
        "authorized_backup_gate": authorized_backup_gate_summary,
        "latest_promotion": latest_promotion,
        "latest_deployment": latest_deployment,
        "recent_promotions": recent_promotions,
        "recent_deployments": recent_deployments,
    }


def _build_environment_overview_payloads(
    *,
    record_store: FilesystemRecordStore | PostgresRecordStore,
    context_name: str,
) -> list[dict[str, object]]:
    inventory_records = sorted(
        (
            record
            for record in record_store.list_environment_inventory()
            if not context_name or record.context == context_name
        ),
        key=lambda record: (record.context, record.instance),
    )
    return [
        _build_environment_status_payload(
            record_store=record_store,
            context_name=inventory_record.context,
            instance_name=inventory_record.instance,
        )
        for inventory_record in inventory_records
    ]


def _build_runtime_environment_rows(
    *,
    entries: dict[str, object],
    source_name: str,
) -> list[dict[str, object]]:
    return [
        {
            "key": key_name,
            "value": str(entries[key_name]),
            "source": source_name,
            "overrides": (),
        }
        for key_name in sorted(entries)
    ]


def _summarize_secret_binding_record(binding: SecretBinding) -> dict[str, object]:
    return {
        "binding_id": binding.binding_id,
        "integration": binding.integration,
        "binding_type": binding.binding_type,
        "binding_key": binding.binding_key,
        "context": binding.context,
        "instance": binding.instance,
        "status": binding.status,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


@click.group()
def main() -> None:
    """Control-plane CLI."""


@main.group("work-graph")
def work_graph() -> None:
    """Work graph recommendation commands."""


register_runner_lane_commands(cast(click.Group, work_graph))  # type: ignore[redundant-cast]
register_preview_workflow_commands(cast(click.Group, work_graph))  # type: ignore[redundant-cast]
register_work_graph_core_commands(cast(click.Group, work_graph))  # type: ignore[redundant-cast]
register_every_code_commands(cast(click.Group, main), store_factory=_store)  # type: ignore[redundant-cast]
register_storage_secret_commands(cast(click.Group, main))  # type: ignore[redundant-cast]
register_product_config_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    summarize_product_profile_record=summarize_product_profile_record,
    summarize_dokploy_target_record=summarize_dokploy_target_record,
    dokploy_target_route=dokploy_target_route,
    target_id_map=target_id_map,
    summarize_runtime_environment_record=summarize_runtime_environment_record,
    summarize_secret_binding_record=_summarize_secret_binding_record,
)
register_runtime_environment_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    control_plane_root=_control_plane_root,
    build_live_target_runtime_contract_payload=_build_live_target_runtime_contract_payload,
    sync_live_target_from_tracked_contract=_sync_live_target_from_tracked_contract,
    apply_live_target_runtime_environment=_apply_live_target_runtime_environment,
)
register_dokploy_target_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    control_plane_root=_control_plane_root,
    normalize_dokploy_target_type=_normalize_dokploy_target_type,
    mutate_dokploy_payload_for_target_creation=_mutate_dokploy_payload_for_target_creation,
)
register_odoo_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    callbacks=OdooCliCallbacks(
        control_plane_root=_control_plane_root,
        store_factory=_odoo_store_factory,
        execute_odoo_prod_rollback=_execute_odoo_prod_rollback_from_cli,
        normalize_odoo_apply_status=_normalize_odoo_apply_status,
        read_dokploy_config=control_plane_dokploy.read_dokploy_config,
    ),
)
register_service_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    callbacks=ServiceCliCallbacks(
        store_factory=_store,
        control_plane_root=_control_plane_root,
        build_bootstrap_policy_payload=_build_bootstrap_policy_payload,
        sync_launchplane_bootstrap_policy=_sync_launchplane_bootstrap_policy,
        inspect_launchplane_service_dokploy_target=_inspect_launchplane_service_dokploy_target,
        launchplane_service_target_preflight_error_message=(
            _launchplane_service_target_preflight_error_message
        ),
        apply_dokploy_image_reference=_apply_dokploy_image_reference,
        restore_dokploy_image_reference=_restore_dokploy_image_reference,
        trigger_and_wait_for_dokploy_target_deploy=_trigger_and_wait_for_dokploy_target_deploy,
        verify_healthcheck_urls=_verify_healthcheck_urls,
        inspect_local_launchplane_config_boundary=_inspect_local_launchplane_config_boundary,
    ),
)
register_record_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    callbacks=RecordsCliCallbacks(
        store_factory=_store,
        load_json_file=_load_json_file,
        summarize_backup_gate_record=_summarize_backup_gate_record,
        summarize_promotion_record=_summarize_promotion_record,
        summarize_deployment_record=_summarize_deployment_record,
        write_environment_inventory=_write_environment_inventory,
        write_environment_inventory_from_promotion=_write_environment_inventory_from_promotion,
        read_source_release_tuple_for_promotion_record=_read_source_release_tuple_for_promotion_record,
        write_promoted_release_tuple=_write_promoted_release_tuple,
        build_environment_status_payload=_build_environment_status_payload,
        build_environment_overview_payloads=_build_environment_overview_payloads,
        summarize_environment_inventory=_summarize_environment_inventory,
    ),
)
register_policy_profile_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    callbacks=PolicyProfileCliCallbacks(
        post_launchplane_service_json=lambda **kwargs: _post_launchplane_service_json(**kwargs),
    ),
)
register_promotion_ship_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    callbacks=PromotionShipCliCallbacks(
        store_factory=lambda *args, **kwargs: _store(*args, **kwargs),
        load_json_file=lambda input_file: _load_json_file(input_file),
        normalize_dokploy_target_type=lambda target_type: _normalize_dokploy_target_type(
            target_type
        ),
        resolve_native_promotion_request=lambda **kwargs: _resolve_native_promotion_request(
            **kwargs
        ),
        require_artifact_id=lambda **kwargs: _require_artifact_id(**kwargs),
        resolve_backup_gate_for_promotion=lambda **kwargs: _resolve_backup_gate_for_promotion(
            **kwargs
        ),
        read_artifact_manifest=lambda **kwargs: _read_artifact_manifest(**kwargs),
        read_source_release_tuple_for_promotion=(
            lambda **kwargs: _read_source_release_tuple_for_promotion(**kwargs)
        ),
        resolve_ship_request_for_promotion=lambda **kwargs: _resolve_ship_request_for_promotion(
            **kwargs
        ),
        execute_ship=lambda **kwargs: _execute_ship(**kwargs),
        write_environment_inventory=lambda **kwargs: _write_environment_inventory(**kwargs),
        write_promoted_release_tuple=lambda **kwargs: _write_promoted_release_tuple(**kwargs),
        resolve_native_ship_request=lambda **kwargs: _resolve_native_ship_request(**kwargs),
    ),
)


register_launchplane_preview_commands(
    cast(click.Group, main),  # type: ignore[redundant-cast]
    callbacks=LaunchplanePreviewCliCallbacks(
        store_factory=_store,
        control_plane_root=_control_plane_root,
        load_json_file=_load_json_file,
        require_preview_status_payload=_require_launchplane_preview_status_payload,
        build_tenant_payload=_build_launchplane_tenant_payload,
        render_index_page_html=_render_launchplane_preview_index_page_html,
        render_policy_page_html=_render_launchplane_preview_policy_page_html,
        write_site_bundle=_write_launchplane_site_bundle,
        render_status_page_html=_render_launchplane_preview_status_page_html,
        preview_profile_rows=_launchplane_preview_profile_rows,
        build_preview_enablement_record=_build_launchplane_preview_enablement_record,
        load_github_webhook_json_bytes=_load_github_webhook_json_bytes,
        json_object=_json_object,
    ),
)
