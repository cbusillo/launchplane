from collections.abc import Callable
from contextlib import contextmanager
from html import escape
import json
import os
import subprocess
import time
from json import JSONDecodeError
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from pydantic import ValidationError

from control_plane import dokploy as control_plane_dokploy
from control_plane import release_tuples as control_plane_release_tuples
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.github_webhook_replay_envelope import GitHubWebhookReplayEnvelope
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import PreviewPullRequestSummary
from control_plane.contracts.preview_mutation_request import (
    HarborPullRequestMutationIntent,
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_manifest import HarborResolvedPreviewManifest
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_request_metadata import (
    HarborCompanionPullRequestReference,
    HarborPreviewRequestMetadata,
    HARBOR_ALLOWED_COMPANION_REPOS,
    HARBOR_PREVIEW_REQUEST_BLOCK_INFO_STRING,
    HarborPreviewRequestParseResult,
)
from control_plane.contracts.promotion_record import (
    BackupGateEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
    PromotionRequest,
    ReleaseStatus,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.ship_request import ShipRequest
from control_plane.harbor_mutations import (
    apply_harbor_destroy_preview as shared_apply_harbor_destroy_preview,
    apply_harbor_generation_evidence as shared_apply_harbor_generation_evidence,
    control_plane_root as shared_control_plane_root,
    upsert_harbor_preview_from_request as shared_upsert_harbor_preview_from_request,
)
from control_plane.service import serve_harbor_service
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.harbor import (
    adapt_github_webhook_pull_request_event,
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    build_pull_request_feedback_payload,
    build_preview_generation_record_from_request,
    build_preview_history_payload,
    build_preview_inventory_payload,
    build_pull_request_event_action_payload,
    build_preview_record_from_request,
    build_preview_status_payload,
    deliver_pull_request_feedback,
    find_preview_record,
    harbor_anchor_repo_context,
    harbor_preview_label_enabled,
    DEFAULT_HARBOR_BASELINE_CHANNEL,
    HARBOR_PREVIEW_ENABLE_LABEL,
    HARBOR_TENANT_ANCHOR_CONTEXTS,
    resolve_pull_request_event_manifest,
    resolve_harbor_github_webhook_secret,
    verify_github_webhook_signature,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.promote import (
    build_executed_promotion_record,
    build_promotion_record,
    generate_promotion_record_id,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)

ARTIFACT_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"
DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF = "origin/main"
ENVIRONMENT_STATUS_HISTORY_LIMIT = 3
_LEGACY_MONOREPO_MARKER = "odoo-ai"
_RUNTIME_CONTRACT_ENV_KEYS = (
    ARTIFACT_IMAGE_REFERENCE_ENV_KEY,
    "ODOO_BASE_RUNTIME_IMAGE",
    "ODOO_BASE_DEVTOOLS_IMAGE",
    "ODOO_ADDON_REPOSITORIES",
    "OPENUPGRADE_ADDON_REPOSITORY",
)


@contextmanager
def _harbor_release_tuples_file_override(release_tuples_file: Path | None):
    if release_tuples_file is None:
        yield
        return
    previous_value = os.environ.get(control_plane_release_tuples.RELEASE_TUPLES_FILE_ENV_VAR)
    os.environ[control_plane_release_tuples.RELEASE_TUPLES_FILE_ENV_VAR] = str(release_tuples_file)
    try:
        yield
    finally:
        if previous_value is None:
            os.environ.pop(control_plane_release_tuples.RELEASE_TUPLES_FILE_ENV_VAR, None)
        else:
            os.environ[control_plane_release_tuples.RELEASE_TUPLES_FILE_ENV_VAR] = previous_value


def _store(state_dir: Path) -> FilesystemRecordStore:
    return FilesystemRecordStore(state_dir=state_dir)


def _control_plane_root() -> Path:
    return shared_control_plane_root()


def _require_harbor_preview_status_payload(
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
            f"No Harbor preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    return payload


def _status_tone(value: str) -> str:
    normalized_value = value.strip().lower()
    if normalized_value in {"pass", "ready", "healthy", "serving"}:
        return "good"
    if normalized_value in {"fail", "failed", "destroyed"}:
        return "bad"
    if normalized_value in {"pending", "building", "deploying", "verifying", "requested", "paused", "unavailable"}:
        return "warn"
    return "neutral"


def _status_label(value: str) -> str:
    normalized_value = value.strip().replace("_", " ")
    return normalized_value or "unknown"


def _generation_in_progress(value: str) -> bool:
    return value.strip().lower() in {"resolving", "building", "deploying", "verifying"}


def _harbor_action_slug(value: str) -> str:
    compact = "".join(character.lower() if character.isalnum() else "-" for character in value.strip())
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "harbor-preview"


def _render_harbor_action_recipe(
    *,
    title: str,
    summary: str,
    tone: str,
    script: str,
    command_label: str,
    recipe_id: str,
    footer_html: str = "",
) -> str:
    return f"""
    <article class=\"action-card tone-{tone}\">
      <div class=\"action-card-head\">
        <div>
          <div class=\"action-command\">{escape(command_label)}</div>
          <h3>{escape(title)}</h3>
          <p>{escape(summary)}</p>
        </div>
        <button class=\"copy-button\" type=\"button\" data-copy-target=\"{escape(recipe_id)}\">Copy recipe</button>
      </div>
      <details class=\"action-details\">
        <summary>Show shell recipe</summary>
        <pre id=\"{escape(recipe_id)}\" class=\"action-pre\">{escape(script)}</pre>
      </details>
      {footer_html}
    </article>
    """


def _build_harbor_action_script(
    *,
    command_name: str,
    file_payloads: tuple[tuple[str, str, dict[str, object]], ...],
    command_args: tuple[str, ...],
) -> str:
    lines = ['STATE_DIR="/path/to/state"']
    for variable_name, file_path, payload in file_payloads:
        lines.append(f'{variable_name}="{file_path}"')
        lines.append(f'cat >"${variable_name}" <<\'JSON\'')
        lines.append(json.dumps(payload, indent=2, sort_keys=True))
        lines.append("JSON")
    command_parts = ["uv", "run", "control-plane", "harbor-previews", command_name, "--state-dir", '"$STATE_DIR"']
    command_parts.extend(command_args)
    lines.append(" ".join(command_parts))
    return "\n".join(lines)


def _render_harbor_shell_document(
    *,
    page_title: str,
    context_name: str,
    active_nav: str,
    body_class: str,
    body_html: str,
    extra_css: str,
    nav_links: dict[str, str] | None = None,
) -> str:
    nav_items = (
        ("overview", "Tenant overview"),
        ("detail", "Detail"),
        ("policy", "Policy"),
    )
    nav_html = "".join(
        _render_harbor_shell_nav_item(
            key=key,
            label=label,
            active_nav=active_nav,
            href=(nav_links or {}).get(key, ""),
        )
        for key, label in nav_items
    )
    context_html = escape(context_name) if context_name else "all contexts"
    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{escape(page_title)}</title>
  <style>
    :root {{
      --bg: #f6f3ec;
      --surface: #fbfaf6;
      --text: #171512;
      --muted: #665f55;
      --line: rgba(23, 21, 18, 0.16);
      --line-strong: rgba(23, 21, 18, 0.28);
      --good: #1c5d3d;
      --warn: #8a6208;
      --bad: #8a312c;
      --neutral: #505050;
      --sans: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      --serif: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
      --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--sans);
      color: var(--text);
      background: var(--bg);
    }}
    a {{ color: inherit; }}
    .app-shell {{ max-width: 1180px; margin: 0 auto; padding: 24px 20px 72px; }}
    .shell-topbar {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 18px;
      align-items: end;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line-strong);
    }}
    .shell-brand {{ display: grid; gap: 8px; }}
    .shell-brand-mark {{
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .shell-brand h1 {{
      margin: 0;
      font-family: var(--serif);
      font-size: 28px;
      line-height: 0.98;
      letter-spacing: -0.02em;
    }}
    .shell-brand p {{ margin: 0; color: var(--muted); font-size: 14px; line-height: 1.5; max-width: 62ch; }}
    .shell-nav {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .shell-nav-item {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
      font-family: var(--mono);
      font-size: 12px;
      color: var(--muted);
    }}
    .shell-nav-item.active {{ color: var(--text); border-color: var(--line-strong); background: var(--surface); }}
    main.page-body {{ margin-top: 28px; }}
    main.page-body.detail-layout {{ max-width: 760px; }}
    main.page-body.index-layout {{ max-width: 1120px; }}
    {extra_css}
    @media (max-width: 760px) {{
      .app-shell {{ padding: 18px 16px 48px; }}
      .shell-brand h1 {{ font-size: 24px; }}
      .shell-topbar {{ align-items: start; }}
    }}
  </style>
</head>
<body>
  <div class=\"app-shell\">
    <header class=\"shell-topbar\">
      <div class=\"shell-brand\">
        <div class=\"shell-brand-mark\">Harbor control plane</div>
        <h1>Tenant environments and PR previews</h1>
        <p>Harbor links testing, prod, and PR preview lanes for {context_html}. GitHub remains the review source; Harbor carries environment state, routing, and promotion evidence.</p>
      </div>
      <nav class=\"shell-nav\" aria-label=\"Harbor sections\">{nav_html}</nav>
    </header>
    <main class=\"page-body {body_class}\">{body_html}</main>
  </div>
</body>
</html>
"""


def _render_harbor_shell_nav_item(*, key: str, label: str, active_nav: str, href: str) -> str:
    class_name = "shell-nav-item active" if key == active_nav else "shell-nav-item"
    if href:
        return f'<a class="{class_name}" href="{escape(href)}">{escape(label)}</a>'
    return f'<span class="{class_name}">{escape(label)}</span>'


def _harbor_preview_bundle_relative_path(
    *,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> Path:
    return Path("previews") / context_name / anchor_repo / f"pr-{anchor_pr_number}.html"


def _harbor_environment_bundle_relative_path(*, context_name: str, instance_name: str) -> Path:
    return Path("environments") / context_name / f"{instance_name}.html"


def _harbor_promotion_bundle_relative_path(*, context_name: str) -> Path:
    return Path("promotions") / context_name / "testing-to-prod.html"


def _relative_href(*, from_file: Path, to_file: Path) -> str:
    return os.path.relpath(to_file, start=from_file.parent)


def _harbor_context_anchor_repo(*, context_name: str) -> str:
    normalized_context = context_name.strip()
    if not normalized_context:
        return ""
    for repo_name, repo_context in sorted(HARBOR_TENANT_ANCHOR_CONTEXTS.items()):
        if repo_context == normalized_context:
            return repo_name
    return ""


def _harbor_inventory_bucket(row: dict[str, object]) -> str:
    state = str(row.get("state", "")).strip().lower()
    health = str(row.get("overall_health_status", "")).strip().lower()
    latest_id = str(row.get("latest_generation_id", "")).strip()
    serving_id = str(row.get("serving_generation_id", "")).strip()
    if state == "destroyed":
        return "retained"
    if state in {"failed", "paused", "teardown_pending"}:
        return "attention"
    if latest_id and not serving_id:
        return "attention"
    if health in {"fail", "failed", "unavailable"}:
        return "attention"
    if state == "pending":
        return "in_flight"
    if latest_id and latest_id != serving_id:
        return "in_flight"
    return "live"


def _harbor_preview_enablement_record_id(*, context_name: str, anchor_repo: str, anchor_pr_number: int) -> str:
    return f"{context_name}-{anchor_repo}-pr-{anchor_pr_number}"


def _build_harbor_promotion_resolve_recipe_script(
    *,
    context_name: str,
    artifact_id: str,
    backup_record_id: str,
) -> str:
    resolved_backup_record_id = backup_record_id.strip() or "backup-prod-pass-record-id"
    return "\n".join(
        (
            'PROMOTION_REQUEST_FILE="/tmp/harbor-promotion-request.json"',
            f'uv run control-plane promote resolve --context "{context_name}" --from-instance testing --to-instance prod --artifact-id "{artifact_id}" --backup-record-id "{resolved_backup_record_id}" >"$PROMOTION_REQUEST_FILE"',
            'cat "$PROMOTION_REQUEST_FILE"',
        )
    )


def _build_harbor_backup_gate_write_recipe_script(
    *,
    context_name: str,
    source: str,
    evidence: dict[str, str],
) -> str:
    payload = {
        "schema_version": 1,
        "record_id": f"backup-{context_name}-prod-<utc-timestamp>",
        "context": context_name,
        "instance": "prod",
        "created_at": "<utc-timestamp>",
        "source": source.strip() or "prod-gate",
        "required": True,
        "status": "pass",
        "evidence": evidence or {"snapshot": "s3://path/to/prod-backup"},
    }
    lines = [
        'STATE_DIR="/path/to/state"',
        'BACKUP_GATE_FILE="/tmp/harbor-backup-gate.json"',
        'cat >"$BACKUP_GATE_FILE" <<\'JSON\'',
        json.dumps(payload, indent=2, sort_keys=True),
        'JSON',
        'uv run control-plane backup-gates write --state-dir "$STATE_DIR" --input-file "$BACKUP_GATE_FILE"',
    ]
    return "\n".join(lines)


def _build_harbor_promotion_execute_recipe_script(*, state_dir: str) -> str:
    return "\n".join(
        (
            f'STATE_DIR="{state_dir or "/path/to/state"}"',
            'PROMOTION_REQUEST_FILE="/tmp/harbor-promotion-request.json"',
            'uv run control-plane promote execute --state-dir "$STATE_DIR" --input-file "$PROMOTION_REQUEST_FILE"',
        )
    )


def _build_harbor_environment_ship_recipe_script(
    *,
    context_name: str,
    instance_name: str,
    artifact_id: str,
    source_git_ref: str,
) -> str:
    request_file = f"/tmp/harbor-{context_name}-{instance_name}-ship-request.json"
    return "\n".join(
        (
            'STATE_DIR="/path/to/state"',
            f'SHIP_REQUEST_FILE="{request_file}"',
            f'uv run control-plane ship resolve --context "{context_name}" --instance "{instance_name}" --artifact-id "{artifact_id}" --source-ref "{source_git_ref}" >"$SHIP_REQUEST_FILE"',
            'cat "$SHIP_REQUEST_FILE"',
            'uv run control-plane ship execute --state-dir "$STATE_DIR" --input-file "$SHIP_REQUEST_FILE"',
        )
    )


def _build_harbor_environment_action_payload(
    *,
    context_name: str,
    instance_name: str,
    environment_payload: dict[str, object] | None,
) -> dict[str, object]:
    live_payload = environment_payload.get("live") if isinstance(environment_payload, dict) else None
    artifact_id = str(live_payload.get("artifact_id", "")).strip() if isinstance(live_payload, dict) else ""
    source_git_ref = str(live_payload.get("source_git_ref", "")).strip() if isinstance(live_payload, dict) else ""
    deploy_status = str(live_payload.get("deploy_status", "")).strip().lower() if isinstance(live_payload, dict) else ""
    if not artifact_id or not source_git_ref:
        return {
            "instance": instance_name,
            "status": "missing_evidence",
            "tone": "neutral",
            "headline": f"{instance_name.capitalize()} has no actionable ship evidence yet.",
            "summary": "Harbor needs a live artifact id and source ref before it can emit a typed ship recipe for this lane.",
            "recipe": "",
        }
    tone = "good" if deploy_status == "pass" else "warn"
    return {
        "instance": instance_name,
        "status": "actionable",
        "tone": tone,
        "headline": f"Re-ship current {instance_name} artifact",
        "summary": (
            f"Resolve and execute Harbor's typed ship flow for the current {instance_name} artifact without re-deriving inputs by hand."
        ),
        "artifact_id": artifact_id,
        "source_git_ref": source_git_ref,
        "recipe": _build_harbor_environment_ship_recipe_script(
            context_name=context_name,
            instance_name=instance_name,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
        ),
    }


def _build_harbor_preview_enablement_action_payload(
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
    request_metadata_companions: tuple[HarborCompanionPullRequestReference, ...],
    request_metadata_companion_summaries: tuple[PreviewPullRequestSummary, ...],
    preview_row: dict[str, object] | None,
) -> dict[str, object]:
    def actionable_payload(
        *,
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
        action_slug = _harbor_action_slug(f"{context_name}-{anchor_repo}-pr-{anchor_pr_number}")
        recipe = _build_harbor_action_script(
            command_name="request-generation",
            file_payloads=(
                ("PREVIEW_FILE", f"/tmp/harbor-{action_slug}-preview.json", preview_request_payload),
                (
                    "GENERATION_FILE",
                    f"/tmp/harbor-{action_slug}-generation.json",
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
            headline = "Materialize requested Harbor preview"
        elif label_enabled:
            headline = "Request Harbor preview from saved label state"
        else:
            headline = "Request Harbor preview"
        return {
            "status": "actionable",
            "tone": "warn",
            "headline": headline,
            "summary": summary,
            "recipe": recipe,
            "recipe_id": f"enablement-{action_slug}-request-generation",
        }

    def companion_snapshot_source_map_payload() -> list[dict[str, object]]:
        return [
            {
                "repo": anchor_repo,
                "git_sha": anchor_head_sha,
                "selection": "anchor",
            },
            *[
                {
                    "repo": summary.repo,
                    "git_sha": summary.head_sha,
                    "selection": "companion",
                }
                for summary in request_metadata_companion_summaries
            ],
        ]

    def companion_summaries_payload() -> list[dict[str, object]]:
        return [
            summary.model_dump(mode="json")
            for summary in request_metadata_companion_summaries
        ]

    def unresolved_companion_payload(*, detail: str) -> dict[str, object]:
        return {
            "status": "blocked",
            "tone": "bad",
            "headline": "Companion PR snapshots are required before Harbor can request this preview.",
            "summary": detail,
            "recipe": "",
        }

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
            "summary": "This preview row does not need an initial Harbor request recipe right now.",
            "recipe": "",
        }
    if request_metadata_status == "invalid":
        return {
            "status": "blocked",
            "tone": "bad",
            "headline": "Preview metadata must be fixed before Harbor can request this preview.",
            "summary": "The saved PR snapshot says the Harbor preview metadata is invalid, so Harbor avoids printing a request recipe that would drift from the PR contract.",
            "recipe": "",
        }
    request_metadata = HarborPreviewRequestParseResult(status="missing")
    if request_metadata_status == "valid":
        request_metadata = HarborPreviewRequestParseResult(
            status="valid",
            metadata=HarborPreviewRequestMetadata(
                baseline_channel=request_metadata_baseline_channel,
                companions=request_metadata_companions,
            ),
        )
    if control_plane_root is None:
        if request_metadata_companions and not request_metadata_companion_summaries:
            return unresolved_companion_payload(
                detail="The saved PR metadata asks Harbor to include companion pull requests, but Harbor has no exact companion head SHA snapshot for this enablement record."
            )
        return actionable_payload(
            summary=(
                "Harbor cannot resolve the default baseline contract from this workspace snapshot, so this request recipe keeps explicit placeholders for the baseline tuple id and manifest fingerprint before execution."
            ),
            baseline_release_tuple_id="<resolved-baseline-tuple-id>",
            resolved_manifest_fingerprint="<resolved-manifest-fingerprint>",
            source_map=(
                companion_snapshot_source_map_payload()
                if request_metadata_companion_summaries
                else None
            ),
            companion_summaries=companion_summaries_payload(),
        )
    if not anchor_pr_url or not anchor_head_sha:
        return {
            "status": "missing_evidence",
            "tone": "neutral",
            "headline": "Harbor needs the latest PR URL and head SHA before it can request this preview.",
            "summary": "The current tenant snapshot is missing the exact anchor PR evidence needed for a typed request-generation recipe.",
            "recipe": "",
        }

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
        label_names=(HARBOR_PREVIEW_ENABLE_LABEL,) if label_enabled else (),
        action_label=HARBOR_PREVIEW_ENABLE_LABEL if label_enabled else "",
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
            return unresolved_companion_payload(
                detail=(
                    "The saved PR metadata asks Harbor to include companion pull requests, but Harbor could not prove their exact head SHAs from stored evidence. "
                    f"Resolve the companion snapshots before running the request recipe: {exc}"
                ),
            )
        return actionable_payload(
            summary=(
                "Harbor could not resolve the default preview manifest automatically for this PR yet. "
                f"Use the typed request recipe below after replacing the baseline placeholders: {exc}"
            ),
            baseline_release_tuple_id="<resolved-baseline-tuple-id>",
            resolved_manifest_fingerprint="<resolved-manifest-fingerprint>",
            source_map=(
                companion_snapshot_source_map_payload()
                if request_metadata_companion_summaries
                else None
            ),
            companion_summaries=companion_summaries_payload(),
        )
    if resolved_manifest is None:
        if request_metadata_companions and not request_metadata_companion_summaries:
            return unresolved_companion_payload(
                detail="The saved PR metadata asks Harbor to include companion pull requests, but Harbor has no exact companion head SHA snapshot for this enablement record."
            )
        return actionable_payload(
            summary=(
                "Harbor could not resolve the default preview manifest automatically for this PR yet. "
                "Use the typed request recipe below after replacing the baseline tuple id and manifest fingerprint placeholders."
            ),
            baseline_release_tuple_id="<resolved-baseline-tuple-id>",
            resolved_manifest_fingerprint="<resolved-manifest-fingerprint>",
            source_map=(
                companion_snapshot_source_map_payload()
                if request_metadata_companion_summaries
                else None
            ),
            companion_summaries=companion_summaries_payload(),
        )

    return actionable_payload(
        summary=(
            "This tenant PR is preview-eligible but still inactive. Run Harbor's typed request-generation flow to create the initial preview route from the default testing baseline."
            if state != "requested" and not label_enabled
            else "GitHub already marked this PR for preview. Run Harbor's typed request-generation flow to turn that saved label state into a live preview route."
            if label_enabled and state != "requested"
            else "A preview request exists, but Harbor has not produced a serving route yet. Run the typed request-generation flow to materialize the preview from the saved PR snapshot."
        ),
        baseline_release_tuple_id=resolved_manifest.baseline_release_tuple_id,
        resolved_manifest_fingerprint=resolved_manifest.resolved_manifest_fingerprint,
        source_map=[item.model_dump(mode="json") for item in resolved_manifest.source_map],
        companion_summaries=[
            item.model_dump(mode="json") for item in resolved_manifest.companion_summaries
        ],
    )


def _build_harbor_promotion_action_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    testing_environment: dict[str, object] | None,
    prod_environment: dict[str, object] | None,
) -> dict[str, object]:
    testing_live = testing_environment.get("live") if isinstance(testing_environment, dict) else None
    prod_live = prod_environment.get("live") if isinstance(prod_environment, dict) else None
    testing_artifact_id = str(testing_live.get("artifact_id", "")).strip() if isinstance(testing_live, dict) else ""
    prod_artifact_id = str(prod_live.get("artifact_id", "")).strip() if isinstance(prod_live, dict) else ""
    testing_source_git_ref = (
        str(testing_live.get("source_git_ref", "")).strip() if isinstance(testing_live, dict) else ""
    )
    testing_deploy_status = (
        str(testing_live.get("deploy_status", "")).strip().lower() if isinstance(testing_live, dict) else ""
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

    def check_status(value: str, *, allow_skipped: bool = False) -> str:
        normalized_value = value.strip().lower()
        if normalized_value == "pass":
            return "pass"
        if allow_skipped and normalized_value == "skipped":
            return "pass"
        if normalized_value in {"pending", ""}:
            return "pending"
        return "fail"

    evidence_checks: list[dict[str, str]] = []
    candidate_status = "pending"
    candidate_detail = "Harbor is waiting for testing/prod inventory evidence before it can name a promotion candidate."
    if testing_artifact_id and prod_artifact_id and testing_artifact_id == prod_artifact_id:
        candidate_status = "pass"
        candidate_detail = f"Testing and prod are already aligned on {testing_artifact_id}."
    elif testing_artifact_id:
        candidate_status = "pass"
        candidate_detail = (
            f"Testing is carrying {testing_artifact_id or 'an artifact'} while prod is carrying {prod_artifact_id or 'nothing recorded yet'}."
        )
    elif prod_artifact_id:
        candidate_status = "fail"
        candidate_detail = "Prod has an artifact, but Harbor has no current testing artifact to promote from."
    evidence_checks.append(
        {
            "label": "Promotion candidate",
            "status": candidate_status,
            "detail": candidate_detail,
        }
    )

    deploy_check_status = check_status(testing_deploy_status)
    evidence_checks.append(
        {
            "label": "Testing deploy",
            "status": deploy_check_status,
            "detail": (
                f"Latest testing deployment status is {testing_deploy_status or 'unavailable'}."
                if testing_live is not None
                else "Harbor has not recorded a live testing deployment yet."
            ),
        }
    )

    health_check_status = check_status(testing_health_status, allow_skipped=True)
    evidence_checks.append(
        {
            "label": "Testing health",
            "status": health_check_status,
            "detail": (
                f"Latest testing health status is {testing_health_status or 'unavailable'}."
                if testing_live is not None
                else "Harbor has not recorded testing health evidence yet."
            ),
        }
    )

    backup_check_status = "pending"
    backup_detail = "Harbor has no prod backup-gate evidence yet. Promotion stays blocked until one is recorded."
    backup_record_id = ""
    backup_gate_source = "prod-gate"
    backup_gate_evidence: dict[str, str] = {"snapshot": "s3://path/to/prod-backup"}
    if latest_backup_gate is not None:
        backup_record_id = latest_backup_gate.record_id
        backup_gate_source = latest_backup_gate.source
        backup_gate_evidence = dict(latest_backup_gate.evidence)
        if latest_backup_gate.required and latest_backup_gate.status == "pass":
            backup_check_status = "pass"
            backup_detail = f"Latest prod backup gate {latest_backup_gate.record_id} passed and can authorize promotion."
        elif latest_backup_gate.status == "fail":
            backup_check_status = "fail"
            backup_detail = f"Latest prod backup gate {latest_backup_gate.record_id} failed. Promotion is blocked until a passing gate is recorded."
        else:
            backup_check_status = "pending"
            backup_detail = f"Latest prod backup gate {latest_backup_gate.record_id} is {latest_backup_gate.status} and does not yet authorize promotion."
    evidence_checks.append(
        {
            "label": "Prod backup gate",
            "status": backup_check_status,
            "detail": backup_detail,
        }
    )

    promotion_status = "unknown"
    headline = "Harbor cannot plan the next promotion yet."
    summary = "Testing and prod need clearer environment evidence before Harbor can describe the next action."
    next_action = "Wait for Harbor to record current tenant environment evidence."
    tone = "neutral"
    backup_gate_recipe = ""
    resolve_recipe = ""
    execute_recipe = ""
    if testing_artifact_id and prod_artifact_id and testing_artifact_id == prod_artifact_id:
        promotion_status = "in_sync"
        headline = "Prod is already serving the current testing artifact."
        summary = "No promotion is pending because the tenant's long-lived lanes are already aligned."
        next_action = "Keep shipping new artifacts into testing until a new promotion candidate appears."
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
            summary = "Harbor has the artifact, testing evidence, and backup authorization needed to plan the next promotion request."
            next_action = "Resolve the typed promotion request, review it, then execute it against prod."
            tone = "good"
            resolve_recipe = _build_harbor_promotion_resolve_recipe_script(
                context_name=context_name,
                artifact_id=testing_artifact_id,
                backup_record_id=backup_record_id,
            )
            execute_recipe = _build_harbor_promotion_execute_recipe_script(state_dir="/path/to/state")
        else:
            promotion_status = "blocked"
            headline = "A newer testing artifact exists, but Harbor cannot promote it yet."
            summary = "The tenant has a promotion candidate, but one or more required evidence checks are still missing or failing."
            next_action = "Clear the failing evidence checks before using Harbor's promotion flow."
            tone = "warn"
            if backup_check_status != "pass":
                backup_gate_recipe = _build_harbor_backup_gate_write_recipe_script(
                    context_name=context_name,
                    source=backup_gate_source,
                    evidence=backup_gate_evidence,
                )
    elif prod_artifact_id:
        promotion_status = "prod_only"
        headline = "Prod has live evidence, but Harbor has no current testing candidate."
        summary = "Harbor cannot promote until testing is carrying the next exact artifact for this tenant."
        next_action = "Ship a new artifact into testing first, then return here to plan promotion."
        tone = "neutral"

    retained_evidence = (
        "Harbor will append a promotion record, keep backup-gate evidence, and refresh prod inventory when a waited promotion completes successfully."
    )
    return {
        "status": promotion_status,
        "tone": tone,
        "headline": headline,
        "summary": summary,
        "next_action": next_action,
        "candidate_artifact_id": testing_artifact_id,
        "current_prod_artifact_id": prod_artifact_id,
        "source_git_ref": testing_source_git_ref,
        "latest_backup_gate": _summarize_backup_gate_record(latest_backup_gate) if latest_backup_gate is not None else None,
        "latest_promotion": latest_promotion if isinstance(latest_promotion, dict) else None,
        "evidence_checks": evidence_checks,
        "retained_evidence": retained_evidence,
        "backup_gate_recipe": backup_gate_recipe,
        "resolve_recipe": resolve_recipe,
        "execute_recipe": execute_recipe,
    }


def _build_harbor_promotion_detail_payload(
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
    testing_live = testing_environment.get("live") if isinstance(testing_environment, dict) else None
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
    evidence_checks = (
        promotion_action.get("evidence_checks")
        if isinstance(promotion_action.get("evidence_checks"), list)
        else []
    )
    if (
        testing_live is None
        and prod_live is None
        and not recent_promotions
        and not recent_backup_gates
        and not any(str(promotion_action.get(key, "")).strip() for key in ("candidate_artifact_id", "current_prod_artifact_id"))
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
            promotion_action.get("headline", "Harbor cannot describe the promotion path yet.")
        ),
        "summary": str(promotion_action.get("summary", "No promotion summary recorded.")),
        "next_action": str(promotion_action.get("next_action", "No next action recorded.")),
        "candidate_artifact_id": str(promotion_action.get("candidate_artifact_id", "")).strip(),
        "current_prod_artifact_id": str(promotion_action.get("current_prod_artifact_id", "")).strip(),
        "source_git_ref": str(promotion_action.get("source_git_ref", "")).strip(),
        "retained_evidence": str(promotion_action.get("retained_evidence", "")).strip(),
        "evidence_checks": [item for item in evidence_checks if isinstance(item, dict)],
        "latest_backup_gate": latest_backup_gate,
        "latest_promotion": latest_promotion,
        "recent_backup_gates": tuple(
            _summarize_backup_gate_record(record) for record in recent_backup_gates
        ),
        "recent_promotions": tuple(
            item for item in recent_promotions if isinstance(item, dict)
        ),
        "testing_live": testing_live if isinstance(testing_live, dict) else None,
        "prod_live": prod_live if isinstance(prod_live, dict) else None,
        "backup_gate_recipe": str(promotion_action.get("backup_gate_recipe", "")).strip(),
        "resolve_recipe": str(promotion_action.get("resolve_recipe", "")).strip(),
        "execute_recipe": str(promotion_action.get("execute_recipe", "")).strip(),
    }


def _build_harbor_preview_enablement_record(
    *,
    context_name: str,
    event: GitHubPullRequestEvent,
    request_metadata: HarborPreviewRequestParseResult,
    resolved_manifest: HarborResolvedPreviewManifest | None = None,
) -> PreviewEnablementRecord | None:
    resolved_context = context_name.strip()
    if not resolved_context:
        return None
    updated_at = event.occurred_at.strip() or utc_now_timestamp()
    return PreviewEnablementRecord(
        record_id=_harbor_preview_enablement_record_id(
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
        label_enabled=harbor_preview_label_enabled(label_names=event.label_names),
        action_label=event.action_label,
        request_metadata_status=request_metadata.status,
        request_metadata_error=request_metadata.error,
        request_metadata_baseline_channel=(
            request_metadata.metadata.baseline_channel if request_metadata.metadata is not None else ""
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
    request_metadata: HarborPreviewRequestParseResult,
    resolved_manifest: HarborResolvedPreviewManifest | None,
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


def _build_harbor_preview_enablement_items(
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
        row_pr_number = int(row.get("anchor_pr_number", 0) or 0)
        if not row_anchor_repo or row_pr_number <= 0:
            continue
        preview_by_key.setdefault((row_anchor_repo, row_pr_number), row)

    def item_tone(state: str) -> str:
        if state == "running":
            return "good"
        if state in {"requested", "paused"}:
            return "warn"
        return "neutral"

    def item_source(
        *,
        label_enabled: bool,
        preview_row: dict[str, object] | None,
        latest_requested_reason: str,
    ) -> str:
        if label_enabled:
            return "github_label"
        if latest_requested_reason.startswith("operator_requested"):
            return "harbor"
        if preview_row is not None and not label_enabled:
            return "history"
        return "none"

    def item_state(*, preview_row: dict[str, object] | None, label_enabled: bool, pr_state: str) -> str:
        preview_state = str(preview_row.get("state", "")).strip().lower() if preview_row is not None else ""
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

    def item_request_summary(
        *,
        state: str,
        source: str,
        label_enabled: bool,
        request_metadata_status: str,
        request_metadata_error: str,
        preview_row: dict[str, object] | None,
    ) -> str:
        if state == "candidate":
            return "Eligible tenant PR. No preview request is active yet."
        if state == "retained":
            return "Harbor is keeping this PR's preview history as retained evidence."
        if source == "github_label":
            if request_metadata_status == "invalid":
                return (
                    "GitHub label harbor-preview requested a preview, but Harbor preview metadata is invalid: "
                    f"{request_metadata_error}"
                )
            if preview_row is None:
                return (
                    "GitHub label harbor-preview requested a preview, but Harbor has not created the preview record yet."
                )
            return "GitHub label harbor-preview is the current preview request source."
        if source == "harbor":
            return "Harbor explicitly requested this preview without relying on the GitHub label."
        if state == "paused":
            return "Harbor is intentionally holding this preview in place until operators resume it."
        return "Harbor still has preview evidence from an earlier request even though no current GitHub label is present."

    def item_status_summary(*, state: str, preview_row: dict[str, object] | None) -> str:
        if preview_row is not None:
            status_summary = str(preview_row.get("status_summary", "")).strip()
            if status_summary:
                return status_summary
        if state == "candidate":
            return "Ready for opt-in preview enablement."
        if state == "requested":
            return "Preview request recorded; Harbor has not materialized a serving route yet."
        return "No additional preview lifecycle evidence recorded yet."

    keys = set(enablement_by_key) | set(preview_by_key)
    items: list[dict[str, object]] = []
    for item_anchor_repo, item_pr_number in keys:
        preview_row = preview_by_key.get((item_anchor_repo, item_pr_number))
        enablement_record = enablement_by_key.get((item_anchor_repo, item_pr_number))
        pr_state = enablement_record.pr_state if enablement_record is not None else "open"
        state = item_state(
            preview_row=preview_row,
            label_enabled=enablement_record.label_enabled if enablement_record is not None else False,
            pr_state=pr_state,
        )
        if not state:
            continue

        preview_id = str(preview_row.get("preview_id", "")).strip() if preview_row is not None else ""
        latest_requested_reason = ""
        if preview_id:
            latest_generations = record_store.list_preview_generation_records(preview_id=preview_id, limit=1)
            if latest_generations:
                latest_requested_reason = latest_generations[0].requested_reason

        label_enabled = enablement_record.label_enabled if enablement_record is not None else False
        source = item_source(
            label_enabled=label_enabled,
            preview_row=preview_row,
            latest_requested_reason=latest_requested_reason,
        )
        request_metadata_status = (
            enablement_record.request_metadata_status if enablement_record is not None else "missing"
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
            enablement_record.request_metadata_companions
            if enablement_record is not None
            else ()
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
            else enablement_record.anchor_pr_url if enablement_record is not None else ""
        )
        preview_label = (
            str(preview_row.get("preview_label", "")).strip()
            if preview_row is not None
            else f"{context_name}/{item_anchor_repo}/pr-{item_pr_number}"
        )
        action_payload = _build_harbor_preview_enablement_action_payload(
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
                "canonical_url": str(preview_row.get("canonical_url", "")).strip() if preview_row is not None else "",
                "state": state,
                "tone": item_tone(state),
                "request_source": source,
                "request_summary": item_request_summary(
                    state=state,
                    source=source,
                    label_enabled=label_enabled,
                    request_metadata_status=request_metadata_status,
                    request_metadata_error=request_metadata_error,
                    preview_row=preview_row,
                ),
                "status_summary": item_status_summary(state=state, preview_row=preview_row),
                "updated_at": updated_at,
                "label_enabled": label_enabled,
                "request_metadata_status": request_metadata_status,
                "request_metadata_baseline_channel": request_metadata_baseline_channel,
                "request_metadata_companion_summaries": [
                    item.model_dump(mode="json")
                    for item in request_metadata_companion_summaries
                ],
                "preview_state": str(preview_row.get("state", "")).strip() if preview_row is not None else "",
                "action": action_payload,
            }
        )

    priority = {"candidate": 0, "requested": 1, "paused": 2, "running": 3, "retained": 4}
    items.sort(key=lambda item: int(item.get("anchor_pr_number", 0) or 0), reverse=True)
    items.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    items.sort(key=lambda item: priority.get(str(item.get("state", "")).strip(), 9))

    counts = {"candidate": 0, "requested": 0, "running": 0, "paused": 0, "retained": 0}
    for item in items:
        item_state_value = str(item.get("state", "")).strip()
        if item_state_value in counts:
            counts[item_state_value] += 1
    return items, counts


def _build_harbor_tenant_payload(
    *,
    control_plane_root: Path | None = None,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str = "",
) -> dict[str, object] | None:
    resolved_context = context_name.strip()
    resolved_anchor_repo = anchor_repo.strip()
    if resolved_context and not resolved_anchor_repo:
        resolved_anchor_repo = _harbor_context_anchor_repo(context_name=resolved_context)
    if resolved_anchor_repo and not resolved_context:
        resolved_context = harbor_anchor_repo_context(repo=resolved_anchor_repo)

    if not resolved_context and not resolved_anchor_repo:
        return None

    preview_inventory = build_preview_inventory_payload(
        record_store=record_store,
        context_name=resolved_context,
    )
    preview_rows = preview_inventory.get("previews") if isinstance(preview_inventory.get("previews"), list) else []
    previews = [item for item in preview_rows if isinstance(item, dict)]
    if resolved_anchor_repo:
        previews = [
            item for item in previews if str(item.get("anchor_repo", "")).strip() == resolved_anchor_repo
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

    preview_enablement, preview_enablement_counts = _build_harbor_preview_enablement_items(
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
        "attention": sum(1 for row in previews if _harbor_inventory_bucket(row) == "attention"),
        "in_flight": sum(1 for row in previews if _harbor_inventory_bucket(row) == "in_flight"),
        "live": sum(1 for row in previews if _harbor_inventory_bucket(row) == "live"),
        "retained": sum(1 for row in previews if _harbor_inventory_bucket(row) == "retained"),
        "reviewable": sum(
            1
            for row in previews
            if _harbor_inventory_bucket(row) == "live"
            and str(row.get("canonical_url", "")).strip()
            and str(row.get("serving_generation_id", "")).strip()
        ),
    }
    preview_candidates = [item for item in preview_enablement if str(item.get("state", "")).strip() == "candidate"]

    testing_environment = environments.get("testing")
    prod_environment = environments.get("prod")
    testing_live = testing_environment.get("live") if isinstance(testing_environment, dict) else None
    prod_live = prod_environment.get("live") if isinstance(prod_environment, dict) else None

    testing_artifact_id = (
        str(testing_live.get("artifact_id", "")).strip() if isinstance(testing_live, dict) else ""
    )
    prod_artifact_id = str(prod_live.get("artifact_id", "")).strip() if isinstance(prod_live, dict) else ""
    promotion_summary = {
        "status": "unknown",
        "summary": "Harbor has not recorded enough tenant environment evidence to describe promotion state yet.",
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
            "summary": "Prod has live deployment evidence, but Harbor does not yet have current testing evidence for comparison.",
            "artifact_id": prod_artifact_id,
        }
    promotion_action = _build_harbor_promotion_action_payload(
        record_store=record_store,
        context_name=resolved_context,
        testing_environment=testing_environment if isinstance(testing_environment, dict) else None,
        prod_environment=prod_environment if isinstance(prod_environment, dict) else None,
    )
    promotion_detail = _build_harbor_promotion_detail_payload(
        record_store=record_store,
        context_name=resolved_context,
        testing_environment=testing_environment if isinstance(testing_environment, dict) else None,
        prod_environment=prod_environment if isinstance(prod_environment, dict) else None,
        promotion_action=promotion_action,
    )
    environment_actions = {
        instance_name: _build_harbor_environment_action_payload(
            context_name=resolved_context,
            instance_name=instance_name,
            environment_payload=environments.get(instance_name) if isinstance(environments, dict) else None,
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


def _write_harbor_site_bundle(
    *,
    state_dir: Path,
    output_dir: Path,
    context_name: str,
    release_tuples_file: Path | None = None,
) -> None:
    record_store = _store(state_dir)
    with _harbor_release_tuples_file_override(release_tuples_file):
        inventory_payload = build_preview_inventory_payload(
            record_store=record_store,
            context_name=context_name,
        )
        tenant_payload = _build_harbor_tenant_payload(
            control_plane_root=_control_plane_root(),
            record_store=record_store,
            context_name=context_name,
        )
    preview_rows = inventory_payload.get("previews") if isinstance(inventory_payload.get("previews"), list) else []
    preview_items = [item for item in preview_rows if isinstance(item, dict)]
    tenant_environments = tenant_payload.get("environments") if isinstance(tenant_payload, dict) else None
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
            promotion_output_map[item_context] = output_dir / _harbor_promotion_bundle_relative_path(
                context_name=item_context,
            )

    environment_output_map: dict[tuple[str, str], Path] = {}
    if isinstance(tenant_environments, dict):
        for instance_name in ("testing", "prod"):
            environment_payload = tenant_environments.get(instance_name)
            if not isinstance(environment_payload, dict):
                continue
            item_context = str(environment_payload.get("context", "")).strip() or context_name
            relative_path = _harbor_environment_bundle_relative_path(
                context_name=item_context,
                instance_name=instance_name,
            )
            environment_output_map[(item_context, instance_name)] = output_dir / relative_path

    preview_output_map: dict[tuple[str, str, int], Path] = {}
    for item in preview_items:
        item_context = str(item.get("context", ""))
        anchor_repo = str(item.get("anchor_repo", ""))
        anchor_pr_number = int(item.get("anchor_pr_number", 0) or 0)
        relative_path = _harbor_preview_bundle_relative_path(
            context_name=item_context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        )
        preview_output_map[(item_context, anchor_repo, anchor_pr_number)] = output_dir / relative_path

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

    index_html = _render_harbor_preview_index_page_html(
        inventory_payload,
        tenant_payload=tenant_payload,
        detail_href_builder=detail_href_builder,
        environment_detail_href_builder=environment_detail_href_builder,
        promotion_detail_href_builder=promotion_detail_href_builder,
        nav_links=index_nav_links,
    )
    index_file.write_text(index_html, encoding="utf-8")

    policy_nav_links = {"overview": _relative_href(from_file=policy_file, to_file=index_file), "policy": "policy.html"}
    if first_detail_file is not None:
        policy_nav_links["detail"] = _relative_href(from_file=policy_file, to_file=first_detail_file)
    policy_html = _render_harbor_preview_policy_page_html(
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
            detail_html = _render_harbor_promotion_status_page_html(
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
            detail_html = _render_harbor_environment_status_page_html(
                environment_payload,
                action_payload=action_payload,
                nav_links=detail_nav_links,
            )
            detail_file.write_text(detail_html, encoding="utf-8")

    for item_context, anchor_repo, anchor_pr_number in preview_output_map:
        status_payload = _require_harbor_preview_status_payload(
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
        detail_html = _render_harbor_preview_status_page_html(
            status_payload,
            nav_links=detail_nav_links,
        )
        detail_file.write_text(detail_html, encoding="utf-8")


def _render_harbor_preview_index_page_html(
    payload: dict[str, object],
    *,
    tenant_payload: dict[str, object] | None = None,
    detail_href_builder: Callable[[str, str, int], str] | None = None,
    environment_detail_href_builder: Callable[[str, str], str] | None = None,
    promotion_detail_href_builder: Callable[[str], str] | None = None,
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", ""))
    previews = payload.get("previews") if isinstance(payload.get("previews"), list) else []
    preview_rows = [item for item in previews if isinstance(item, dict)]

    def preview_matches_filter(row: dict[str, object], filter_key: str) -> bool:
        bucket = _harbor_inventory_bucket(row)
        canonical_url = str(row.get("canonical_url", "")).strip()
        serving_id = str(row.get("serving_generation_id", "")).strip()
        if filter_key == "all":
            return True
        if filter_key == "reviewable":
            return bucket == "live" and bool(canonical_url and serving_id)
        return bucket == filter_key

    def row_priority(row: dict[str, object]) -> int:
        state = str(row.get("state", "")).strip().lower()
        health = str(row.get("overall_health_status", "")).strip().lower()
        latest_id = str(row.get("latest_generation_id", "")).strip()
        serving_id = str(row.get("serving_generation_id", "")).strip()
        canonical_url = str(row.get("canonical_url", "")).strip()
        if state in {"failed", "teardown_pending"} or health in {"fail", "failed", "unavailable"}:
            return 0
        if latest_id and not serving_id:
            return 0
        if state == "paused":
            return 1
        if state == "pending" or (latest_id and latest_id != serving_id):
            return 2
        if not canonical_url:
            return 3
        if state == "destroyed":
            return 5
        return 4

    def row_filter_keys(row: dict[str, object]) -> list[str]:
        keys = ["all", _harbor_inventory_bucket(row)]
        if preview_matches_filter(row, "reviewable"):
            keys.append("reviewable")
        return keys

    def row_scope_keys(row: dict[str, object]) -> list[str]:
        keys = ["all"]
        context_value = str(row.get("context", "")).strip()
        anchor_repo_value = str(row.get("anchor_repo", "")).strip()
        if context_value:
            keys.append(f"context:{context_value}")
        if anchor_repo_value:
            keys.append(f"repo:{anchor_repo_value}")
        return keys

    def signal_badges(row: dict[str, object]) -> list[tuple[str, str]]:
        state = str(row.get("state", "")).strip().lower()
        latest_id = str(row.get("latest_generation_id", "")).strip()
        serving_id = str(row.get("serving_generation_id", "")).strip()
        canonical_url = str(row.get("canonical_url", "")).strip()
        destroy_after = str(row.get("destroy_after", "")).strip()
        destroyed_at = str(row.get("destroyed_at", "")).strip()
        destroy_reason = str(row.get("destroy_reason", "")).strip().replace("_", " ")
        badges: list[tuple[str, str]] = []
        if state == "destroyed":
            badges.append(("Evidence only", "neutral"))
            if destroy_reason:
                badges.append((f"Cause {destroy_reason}", "neutral"))
            elif destroyed_at:
                badges.append((f"Destroyed {destroyed_at}", "neutral"))
            return badges[:2]
        if latest_id and not serving_id:
            badges.append(("Route gap", "bad"))
        elif latest_id and latest_id != serving_id:
            badges.append(("Serving older generation", "warn"))
        elif canonical_url and serving_id:
            badges.append(("Route live", "good"))
        elif canonical_url:
            badges.append(("Route reserved", "neutral"))
        if state == "pending":
            badges.append(("Build forming", "warn"))
        elif state == "paused":
            badges.append(("Operator hold", "warn"))
        elif state == "teardown_pending":
            badges.append(("Cleanup queued", "warn"))
        elif state == "failed":
            badges.append(("Needs intervention", "bad"))
        if destroy_after:
            badges.append((f"Cleanup {destroy_after}", "neutral"))
        return badges[:3]

    bucket_specs = (
        ("attention", "Needs attention", "Previews that are blocked, degraded, or no longer serving cleanly."),
        ("in_flight", "In flight", "Preview generations that are still forming or replacing existing review environments."),
        ("live", "Live review", "Serving previews that are currently usable for review work."),
        ("retained", "Retained evidence", "Destroyed previews that still matter as historical evidence."),
    )
    filter_specs = (
        ("all", "All fleet", "Scan the full Harbor queue without losing lane structure."),
        ("attention", "Needs attention", "Surface broken, blocked, or non-serving previews first."),
        ("in_flight", "In flight", "Track previews that are building or rotating toward a new generation."),
        ("reviewable", "Reviewable now", "Show only previews that are currently serving a stable review route."),
        ("retained", "Retained", "Limit the queue to historical evidence kept after cleanup."),
    )
    scope_specs: list[tuple[str, str, str]] = [("all", "All scopes", "Across every Harbor context and anchor repo.")]
    contexts = sorted(
        {str(row.get("context", "")).strip() for row in preview_rows if str(row.get("context", "")).strip()}
    )
    repos = sorted(
        {
            str(row.get("anchor_repo", "")).strip()
            for row in preview_rows
            if str(row.get("anchor_repo", "")).strip()
        }
    )
    if len(contexts) > 1:
        scope_specs.extend(
            (
                f"context:{context_value}",
                f"Context {context_value}",
                f"Limit the queue to Harbor context {context_value}.",
            )
            for context_value in contexts
        )
    if len(repos) > 1:
        scope_specs.extend(
            (
                f"repo:{repo_value}",
                f"Repo {repo_value}",
                f"Limit the queue to anchor repo {repo_value}.",
            )
            for repo_value in repos
        )
    grouped_rows = {key: [] for key, _, _ in bucket_specs}
    for row in preview_rows:
        grouped_rows[_harbor_inventory_bucket(row)].append(row)
    for rows in grouped_rows.values():
        rows.sort(key=lambda row: escape(str(row.get("preview_label", ""))))
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        rows.sort(key=row_priority)

    summary_counts = {
        "attention": len(grouped_rows["attention"]),
        "in_flight": len(grouped_rows["in_flight"]),
        "live": len(grouped_rows["live"]),
        "retained": len(grouped_rows["retained"]),
    }
    filter_counts = {
        key: sum(1 for row in preview_rows if preview_matches_filter(row, key)) for key, _, _ in filter_specs
    }
    filter_notes = {key: note for key, _, note in filter_specs}
    scope_counts = {
        key: sum(1 for row in preview_rows if key == "all" or key in row_scope_keys(row))
        for key, _, _ in scope_specs
    }
    scope_notes = {key: note for key, _, note in scope_specs}
    show_scope_controls = len(scope_specs) > 1

    def render_preview_row(row: dict[str, object]) -> str:
        preview_label = escape(str(row.get("preview_label", "Harbor preview")))
        detail_href = ""
        if detail_href_builder is not None:
            detail_href = str(
                detail_href_builder(
                    str(row.get("context", "")),
                    str(row.get("anchor_repo", "")),
                    int(row.get("anchor_pr_number", 0) or 0),
                )
            )
        canonical_url = escape(str(row.get("canonical_url", "")))
        anchor_pr_url = escape(str(row.get("anchor_pr_url", "")))
        state = str(row.get("state", ""))
        health = str(row.get("overall_health_status", ""))
        artifact_id = escape(str(row.get("artifact_id", "")))
        manifest = escape(str(row.get("manifest_fingerprint", "")))
        updated_at = escape(str(row.get("updated_at", "")))
        next_action = escape(str(row.get("next_action", "")))
        anchor_repo = escape(str(row.get("anchor_repo", "")))
        anchor_pr_number = escape(str(row.get("anchor_pr_number", "")))
        status_summary = escape(str(row.get("status_summary", "")))
        state_tone = _status_tone(state)
        health_tone = _status_tone(health)
        bucket = _harbor_inventory_bucket(row)
        filters = " ".join(row_filter_keys(row))
        scopes = " ".join(row_scope_keys(row))
        signal_html = "".join(
            f'<span class="signal-chip signal-{tone}">{escape(label)}</span>'
            for label, tone in signal_badges(row)
        )
        route_html = (
            f'<a href="{canonical_url}">{canonical_url}</a>' if canonical_url else "No preview route recorded."
        )
        title_html = (
            f'<a class="preview-row-title" href="{escape(detail_href)}">{preview_label}</a>'
            if detail_href
            else preview_label
        )
        action_links: list[str] = []
        if detail_href:
            action_links.append(f'<a href="{escape(detail_href)}">Detail</a>')
            action_links.append(f'<a href="{escape(detail_href)}#operator-actions">Actions</a>')
        if canonical_url:
            action_links.append(f'<a href="{canonical_url}">Preview</a>')
        if anchor_pr_url:
            action_links.append(f'<a href="{anchor_pr_url}">PR</a>')
        actions_html = "".join(action_links)
        return f"""
        <article class=\"preview-row\" data-preview-row data-bucket=\"{escape(bucket)}\" data-filters=\"{escape(filters)}\" data-scopes=\"{escape(scopes)}\">
          <div class=\"preview-row-head\">
            <div>
              <h3>{title_html}</h3>
              <p>{route_html}</p>
            </div>
            <div class=\"preview-row-tones\">
              <span class=\"tone-pill tone-{state_tone}\">Preview {escape(_status_label(state))}</span>
              <span class=\"tone-pill tone-{health_tone}\">Health {escape(_status_label(health))}</span>
            </div>
          </div>
          <div class=\"preview-row-signals\">{signal_html}</div>
          <p class=\"preview-row-summary\">{next_action or status_summary or 'No next action recorded.'}</p>
          <div class=\"preview-row-actions\">{actions_html}</div>
          <dl class=\"preview-row-meta\">
            <div><dt>Anchor</dt><dd>{anchor_repo or 'Unknown'} PR {anchor_pr_number or 'Unknown'}</dd></div>
            <div><dt>Artifact</dt><dd><code>{artifact_id or 'Unavailable'}</code></dd></div>
            <div><dt>Manifest</dt><dd><code>{manifest or 'Unavailable'}</code></dd></div>
            <div><dt>Updated</dt><dd>{updated_at or 'Unavailable'}</dd></div>
          </dl>
        </article>
        """

    lane_html = ""
    for key, label, description in bucket_specs:
        rows = grouped_rows[key]
        rows_html = "".join(render_preview_row(row) for row in rows) or "<p class=\"lane-empty\">No previews in this lane.</p>"
        lane_html += f"""
        <section class=\"lane-section\" data-lane-section data-bucket=\"{escape(key)}\">
          <div class=\"lane-section-head\">
            <div>
              <div class=\"section-label\">{label}</div>
              <h2>{label}</h2>
              <p>{description}</p>
            </div>
            <div class=\"lane-count\" data-lane-count>{len(rows)} visible</div>
          </div>
          <div class=\"lane-stack\">{rows_html}</div>
        </section>
        """

    focus_controls_html = "".join(
        (
            f'<button class="focus-chip{" is-active" if key == "all" else ""}" '
            f'type="button" data-filter-control="{escape(key)}" '
            f'aria-pressed="{"true" if key == "all" else "false"}">'
            f'<span>{escape(label)}</span><strong>{filter_counts[key]}</strong></button>'
        )
        for key, label, _ in filter_specs
    )
    scope_controls_html = "".join(
        (
            f'<button class="scope-chip{" is-active" if key == "all" else ""}" '
            f'type="button" data-scope-control="{escape(key)}" '
            f'aria-pressed="{"true" if key == "all" else "false"}">'
            f'<span>{escape(label)}</span><strong>{scope_counts[key]}</strong></button>'
        )
        for key, label, _ in scope_specs
    )
    scope_panel_html = ""
    if show_scope_controls:
        scope_panel_html = f"""
            <div class=\"scope-panel\">
              <div class=\"section-label\">Scope</div>
              <div class=\"scope-chip-row\" role=\"toolbar\" aria-label=\"Fleet scope filters\">{scope_controls_html}</div>
            </div>
        """
    focus_status_class = "focus-status" if not show_scope_controls else "focus-status focus-status-hidden"
    summary_strip_html = f"""
        <dl class=\"summary-strip\">
          <div><dt>Needs attention</dt><dd>{summary_counts['attention']}</dd></div>
          <div><dt>In flight</dt><dd>{summary_counts['in_flight']}</dd></div>
          <div><dt>Live review</dt><dd>{summary_counts['live']}</dd></div>
          <div><dt>Retained evidence</dt><dd>{summary_counts['retained']}</dd></div>
        </dl>
    """
    if show_scope_controls:
        summary_strip_html = ""

    def environment_tone(environment_payload: dict[str, object] | None) -> str:
        if not isinstance(environment_payload, dict):
            return "neutral"
        live_payload = environment_payload.get("live")
        if not isinstance(live_payload, dict):
            return "neutral"
        deploy_status = str(live_payload.get("deploy_status", "")).strip().lower()
        destination_health = str(live_payload.get("destination_health_status", "")).strip().lower()
        if deploy_status == "fail" or destination_health == "fail":
            return "bad"
        if deploy_status == "pass" and destination_health in {"pass", "skipped"}:
            return "good"
        if deploy_status or destination_health:
            return "warn"
        return "neutral"

    def render_environment_lane(instance_name: str, environment_payload: dict[str, object] | None) -> str:
        lane_label = "Testing lane" if instance_name == "testing" else "Prod lane"
        if not isinstance(environment_payload, dict):
            return f"""
            <section class=\"environment-lane environment-lane-empty\">
              <div class=\"environment-lane-head\">
                <div>
                  <div class=\"section-label\">{lane_label}</div>
                  <h3>{escape(instance_name)}</h3>
                </div>
                <span class=\"tone-pill tone-neutral\">No evidence</span>
              </div>
              <p class=\"environment-summary\">Harbor has not recorded a live {escape(instance_name)} deployment for this tenant yet.</p>
            </section>
            """
        detail_href = ""
        item_context = str(environment_payload.get("context", "")).strip()
        if environment_detail_href_builder is not None and item_context:
            detail_href = environment_detail_href_builder(item_context, instance_name)
        live_payload = environment_payload.get("live") if isinstance(environment_payload.get("live"), dict) else {}
        latest_promotion = (
            environment_payload.get("latest_promotion")
            if isinstance(environment_payload.get("latest_promotion"), dict)
            else None
        )
        tone = environment_tone(environment_payload)
        deploy_status = escape(str(live_payload.get("deploy_status", "pending") or "pending"))
        destination_health = escape(
            str(live_payload.get("destination_health_status", "pending") or "pending")
        )
        artifact_id = escape(str(live_payload.get("artifact_id", "")))
        source_git_ref = escape(str(live_payload.get("source_git_ref", "")))
        updated_at = escape(str(live_payload.get("updated_at", "")))
        promoted_from = escape(str(live_payload.get("promoted_from_instance", "")))
        lane_summary = (
            "Testing carries the current integration artifact for this tenant."
            if instance_name == "testing"
            else "Prod is the currently promoted artifact for this tenant."
        )
        promotion_meta = ""
        if latest_promotion is not None and instance_name == "prod":
            promotion_meta = (
                "<p class=\"environment-note\">"
                f"Latest promotion moved <code>{escape(str(latest_promotion.get('artifact_id', '')) or 'Unavailable')}</code> "
                f"from {escape(str(latest_promotion.get('from_instance', 'testing')) or 'testing')} into prod."
                "</p>"
            )
        elif promoted_from:
            promotion_meta = f'<p class="environment-note">This lane was last promoted from {promoted_from}.</p>'
        detail_link_html = ""
        if detail_href:
            detail_link_html = (
                '<div class="environment-links">'
                f'<a class="lane-detail-link" href="{escape(detail_href)}">Open lane detail</a>'
                "</div>"
            )
        return f"""
        <section class=\"environment-lane\">
          <div class=\"environment-lane-head\">
            <div>
              <div class=\"section-label\">{lane_label}</div>
              <h3>{escape(instance_name)}</h3>
            </div>
            <div class=\"environment-tones\">
              <span class=\"tone-pill tone-{tone}\">Deploy {deploy_status}</span>
              <span class=\"tone-pill tone-{_status_tone(destination_health)}\">Health {destination_health}</span>
            </div>
          </div>
          <p class=\"environment-summary\">{lane_summary}</p>
          {promotion_meta}
          <dl class=\"environment-meta\">
            <div><dt>Artifact</dt><dd><code>{artifact_id or 'Unavailable'}</code></dd></div>
            <div><dt>Source ref</dt><dd><code>{source_git_ref or 'Unavailable'}</code></dd></div>
            <div><dt>Updated</dt><dd>{updated_at or 'Unavailable'}</dd></div>
            <div><dt>Deploy record</dt><dd><code>{escape(str(live_payload.get('deployment_record_id', '')) or 'Unavailable')}</code></dd></div>
          </dl>
          {detail_link_html}
        </section>
        """

    def render_enablement_action(action_payload: dict[str, object]) -> str:
        action_status = str(action_payload.get("status", "none")).strip()
        action_tone = escape(str(action_payload.get("tone", "neutral")).strip() or "neutral")
        headline = escape(str(action_payload.get("headline", "No preview action available.")))
        summary = escape(str(action_payload.get("summary", "")))
        if action_status == "actionable":
            recipe = str(action_payload.get("recipe", "")).strip()
            recipe_id = escape(str(action_payload.get("recipe_id", "preview-enable-request") or "preview-enable-request"))
            return f"""
            <div class=\"enablement-inline-action tone-{action_tone}\">
              <div class=\"enablement-inline-action-head\">
                <div>
                  <div class=\"action-command\">request-generation</div>
                  <h4>{headline}</h4>
                  <p>{summary}</p>
                </div>
                <button class=\"copy-button\" type=\"button\" data-copy-target=\"{recipe_id}\">Copy recipe</button>
              </div>
              <details class=\"action-details\">
                <summary>Show Harbor request recipe</summary>
                <pre id=\"{recipe_id}\" class=\"action-pre\">{escape(recipe)}</pre>
              </details>
            </div>
            """
        if action_status in {"blocked", "manual_review_required", "missing_context", "missing_evidence"}:
            return f"""
            <div class=\"enablement-inline-note tone-{action_tone}\">
              <h4>{headline}</h4>
              <p>{summary}</p>
            </div>
            """
        return ""

    def render_enablement_row(item: dict[str, object]) -> str:
        anchor_repo = str(item.get("anchor_repo", "")).strip()
        anchor_pr_number = int(item.get("anchor_pr_number", 0) or 0)
        anchor_pr_url = escape(str(item.get("anchor_pr_url", "")).strip())
        state = escape(str(item.get("state", "candidate")).strip() or "candidate")
        tone = escape(str(item.get("tone", "neutral")).strip() or "neutral")
        request_source = str(item.get("request_source", "none")).strip()
        source_label = {
            "github_label": "GitHub label",
            "harbor": "Harbor request",
            "history": "Earlier request",
            "none": "Not requested",
        }.get(request_source, "Harbor")
        request_summary = escape(str(item.get("request_summary", "")).strip())
        status_summary = escape(str(item.get("status_summary", "")).strip())
        canonical_url = escape(str(item.get("canonical_url", "")).strip())
        preview_id = escape(str(item.get("preview_id", "")).strip())
        action_payload = item.get("action") if isinstance(item.get("action"), dict) else None
        detail_href = ""
        if detail_href_builder is not None and anchor_repo and anchor_pr_number > 0:
            detail_href = detail_href_builder(context_name, anchor_repo, anchor_pr_number)
        actions = [f'<a href="{anchor_pr_url}">PR</a>'] if anchor_pr_url else []
        if detail_href:
            actions.append(f'<a href="{escape(detail_href)}">Detail</a>')
        if canonical_url:
            actions.append(f'<a href="{canonical_url}">Preview</a>')
        if preview_id:
            actions.append(f'<span class="enablement-meta"><code>{preview_id}</code></span>')
        actions_html = "".join(actions)
        action_html = render_enablement_action(action_payload) if action_payload is not None else ""
        return f"""
        <article class=\"enablement-row\">
          <div class=\"enablement-row-main\">
            <div class=\"enablement-row-head\">
              <h3>PR #{anchor_pr_number}</h3>
              <div class=\"enablement-row-tones\">
                <span class=\"tone-pill tone-{tone}\">{state}</span>
                <span class=\"signal-chip\">{escape(source_label)}</span>
              </div>
            </div>
            <p>{request_summary}</p>
            <p class=\"enablement-status\">{status_summary}</p>
          </div>
          <div class=\"enablement-row-actions\">{actions_html}</div>
          {action_html}
        </article>
        """

    def render_promotion_evidence_check(check: dict[str, object]) -> str:
        status = str(check.get("status", "pending")).strip().lower() or "pending"
        tone = "good" if status == "pass" else "bad" if status == "fail" else "warn"
        return f"""
        <article class=\"promotion-check promotion-check-{tone}\">
          <div class=\"promotion-check-head\">
            <h4>{escape(str(check.get('label', 'Evidence')))}</h4>
            <span class=\"signal-chip signal-{tone}\">{escape(_status_label(status))}</span>
          </div>
          <p>{escape(str(check.get('detail', 'No evidence detail recorded.')))}</p>
        </article>
        """

    def render_promotion_action_panel(
        promotion_action: dict[str, object],
        *,
        context_name: str,
    ) -> str:
        tone = str(promotion_action.get("tone", "neutral")).strip() or "neutral"
        evidence_checks = (
            promotion_action.get("evidence_checks")
            if isinstance(promotion_action.get("evidence_checks"), list)
            else []
        )
        evidence_html = "".join(
            render_promotion_evidence_check(check)
            for check in evidence_checks
            if isinstance(check, dict)
        )
        recipe_cards: list[str] = []
        backup_gate_recipe = str(promotion_action.get("backup_gate_recipe", "")).strip()
        if backup_gate_recipe:
            recipe_cards.append(
                _render_harbor_action_recipe(
                    title="Record prod backup gate",
                    summary="Persist the exact backup authorization Harbor expects before trying to promote into prod.",
                    tone="warn",
                    script=backup_gate_recipe,
                    command_label="backup-gates write",
                    recipe_id=f"promotion-{escape(context_name)}-backup-gate",
                )
            )
        resolve_recipe = str(promotion_action.get("resolve_recipe", "")).strip()
        if resolve_recipe:
            recipe_cards.append(
                _render_harbor_action_recipe(
                    title="Plan promotion request",
                    summary="Resolve Harbor's typed promotion request from the current tenant evidence before execution.",
                    tone=tone,
                    script=resolve_recipe,
                    command_label="promote resolve",
                    recipe_id=f"promotion-{escape(context_name)}-resolve",
                )
            )
        execute_recipe = str(promotion_action.get("execute_recipe", "")).strip()
        if execute_recipe:
            recipe_cards.append(
                _render_harbor_action_recipe(
                    title="Execute promotion",
                    summary="Run the resolved promotion request once the typed payload looks correct.",
                    tone=tone,
                    script=execute_recipe,
                    command_label="promote execute",
                    recipe_id=f"promotion-{escape(context_name)}-execute",
                )
            )
        detail_href = ""
        if promotion_detail_href_builder is not None:
            detail_href = promotion_detail_href_builder(context_name)
        detail_link_html = ""
        if detail_href:
            detail_link_html = (
                '<p class="promotion-detail-link">'
                f'<a class="lane-detail-link" href="{escape(detail_href)}">Open promotion detail</a>'
                "</p>"
            )
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
        latest_backup_gate_html = "Unavailable"
        if latest_backup_gate is not None:
            latest_backup_gate_html = (
                f"<code>{escape(str(latest_backup_gate.get('record_id', '')) or 'Unavailable')}</code>"
            )
        latest_promotion_html = "Unavailable"
        if latest_promotion is not None:
            latest_promotion_html = (
                f"<code>{escape(str(latest_promotion.get('record_id', '')) or 'Unavailable')}</code>"
            )
        recipe_html = "".join(recipe_cards) or (
            '<p class="action-empty">Harbor is not exposing a promotion recipe for the current tenant state yet.</p>'
        )
        return f"""
        <section class=\"promotion-stage\">
          <div class=\"promotion-stage-head\">
            <div>
              <div class=\"section-label\">Next promotion</div>
              <h3>{escape(str(promotion_action.get('headline', 'Harbor cannot describe the next promotion yet.')))}</h3>
              <p class=\"promotion-stage-copy\">{escape(str(promotion_action.get('summary', 'No promotion summary recorded.')))}</p>
            </div>
            <span class=\"tone-pill tone-{escape(tone)}\">{escape(str(promotion_action.get('status', 'unknown')).replace('_', ' '))}</span>
          </div>
          <div class=\"promotion-stage-grid\">
            <div class=\"promotion-primary\">
              <dl class=\"promotion-meta\">
                <div><dt>Candidate artifact</dt><dd><code>{escape(str(promotion_action.get('candidate_artifact_id', '')) or 'Unavailable')}</code></dd></div>
                <div><dt>Current prod</dt><dd><code>{escape(str(promotion_action.get('current_prod_artifact_id', '')) or 'Unavailable')}</code></dd></div>
                <div><dt>Testing source ref</dt><dd><code>{escape(str(promotion_action.get('source_git_ref', '')) or 'Unavailable')}</code></dd></div>
                <div><dt>Latest backup gate</dt><dd>{latest_backup_gate_html}</dd></div>
                <div><dt>Latest promotion</dt><dd>{latest_promotion_html}</dd></div>
                <div><dt>Harbor retains</dt><dd>{escape(str(promotion_action.get('retained_evidence', 'Unavailable')))}</dd></div>
              </dl>
              <p class=\"promotion-next-action\">{escape(str(promotion_action.get('next_action', 'No next action recorded.')))}</p>
            </div>
            <div class=\"promotion-evidence\">{evidence_html}</div>
          </div>
          <div class=\"promotion-recipes\">{recipe_html}</div>
          {detail_link_html}
        </section>
        """

    def render_environment_action_panel(
        environment_actions: dict[str, object],
        *,
        context_name: str,
    ) -> str:
        action_cards: list[str] = []
        for instance_name in ("testing", "prod"):
            action_payload = environment_actions.get(instance_name)
            if not isinstance(action_payload, dict):
                continue
            if str(action_payload.get("status", "")).strip() == "actionable":
                recipe = str(action_payload.get("recipe", "")).strip()
                if recipe:
                    detail_href = ""
                    if environment_detail_href_builder is not None:
                        detail_href = environment_detail_href_builder(context_name, instance_name)
                    footer_html = ""
                    if detail_href:
                        footer_html = (
                            '<p class="action-footer">'
                            f'<a class="lane-detail-link" href="{escape(detail_href)}">Open {escape(instance_name)} lane detail</a>'
                            "</p>"
                        )
                    action_cards.append(
                        _render_harbor_action_recipe(
                            title=str(action_payload.get("headline", f"Re-ship current {instance_name} artifact")),
                            summary=str(action_payload.get("summary", "")),
                            tone=str(action_payload.get("tone", "neutral")),
                            script=recipe,
                            command_label="ship resolve -> ship execute",
                            recipe_id=f"environment-{escape(context_name)}-{escape(instance_name)}-ship",
                            footer_html=footer_html,
                        )
                    )
                continue
            detail_href = ""
            if environment_detail_href_builder is not None:
                detail_href = environment_detail_href_builder(context_name, instance_name)
            detail_link_html = ""
            if detail_href:
                detail_link_html = (
                    '<p class="lane-action-links">'
                    f'<a class="lane-detail-link" href="{escape(detail_href)}">Open {escape(instance_name)} lane detail</a>'
                    "</p>"
                )
            action_cards.append(
                f"""
                <article class=\"lane-action-note\">
                  <div class=\"section-label\">{escape(instance_name)} lane</div>
                  <h3>{escape(str(action_payload.get('headline', 'No lane action available.')))}</h3>
                  <p>{escape(str(action_payload.get('summary', 'Harbor does not have enough evidence for a typed lane action yet.')))}</p>
                  {detail_link_html}
                </article>
                """
            )
        if not action_cards:
            return ""
        return f"""
        <section class=\"lane-actions\">
          <div class=\"lane-actions-head\">
            <div>
              <div class=\"section-label\">Lane actions</div>
              <h3>Rebuild long-lived lanes</h3>
            </div>
            <p class=\"lane-actions-copy\">Use current environment evidence to re-ship the live artifact without reconstructing the request by hand.</p>
          </div>
          <div class=\"lane-actions-grid\">{''.join(action_cards)}</div>
        </section>
        """

    tenant_stage_html = ""
    roster_label = "Preview queue"
    roster_title = "Harbor-native review lanes"
    roster_summary = (
        "GitHub remains the PR and event source. Harbor owns the preview inventory, lifecycle, routing, and operator triage surface."
    )
    if isinstance(tenant_payload, dict):
        tenant_label = escape(str(tenant_payload.get("tenant_label", "")).strip() or context_name or "tenant")
        preview_counts = tenant_payload.get("preview_counts") if isinstance(tenant_payload.get("preview_counts"), dict) else {}
        preview_candidates = (
            tenant_payload.get("preview_candidates")
            if isinstance(tenant_payload.get("preview_candidates"), list)
            else []
        )
        preview_enablement = (
            tenant_payload.get("preview_enablement")
            if isinstance(tenant_payload.get("preview_enablement"), list)
            else []
        )
        preview_enablement_counts = (
            tenant_payload.get("preview_enablement_counts")
            if isinstance(tenant_payload.get("preview_enablement_counts"), dict)
            else {}
        )
        environments = tenant_payload.get("environments") if isinstance(tenant_payload.get("environments"), dict) else {}
        promotion_summary = (
            tenant_payload.get("promotion_summary")
            if isinstance(tenant_payload.get("promotion_summary"), dict)
            else {}
        )
        promotion_action = (
            tenant_payload.get("promotion_action")
            if isinstance(tenant_payload.get("promotion_action"), dict)
            else {}
        )
        environment_actions = (
            tenant_payload.get("environment_actions")
            if isinstance(tenant_payload.get("environment_actions"), dict)
            else {}
        )
        preview_enablement_rows = [item for item in preview_enablement if isinstance(item, dict)]
        preview_enablement_html = ""
        if preview_enablement_rows:
            visible_enablement_rows = preview_enablement_rows[:4]
            remaining_enablement_count = len(preview_enablement_rows) - len(visible_enablement_rows)
            overflow_note = (
                f"<p class=\"enablement-overflow\">{remaining_enablement_count} more PRs stay visible in the queue below.</p>"
                if remaining_enablement_count > 0
                else ""
            )
            preview_enablement_html = f"""
            <section class=\"tenant-enablement\">
              <div class=\"tenant-enablement-head\">
                <div>
                  <div class=\"section-label\">Preview enablement</div>
                  <h3>Why each PR does or does not have a preview</h3>
                </div>
                <p class=\"tenant-enablement-copy\">Candidates, label-driven requests, Harbor-driven requests, and retained history now stay visible before the deeper queue.</p>
              </div>
              <div class=\"enablement-list\">{''.join(render_enablement_row(item) for item in visible_enablement_rows)}</div>
              {overflow_note}
            </section>
            """
        has_environment_evidence = any(
            isinstance(environments.get(instance_name), dict) for instance_name in ("testing", "prod")
        )
        promotion_status = str(promotion_action.get("status", "")).strip().lower() if promotion_action else ""
        show_promotion_stage = bool(has_environment_evidence or promotion_status not in {"", "unknown"})
        lane_actions_html = render_environment_action_panel(environment_actions, context_name=context_name)
        promotion_stage_html = render_promotion_action_panel(promotion_action, context_name=context_name)
        sparse_preview_html = ""
        if not has_environment_evidence:
            sparse_preview_html = f"""
            {preview_enablement_html}
            <dl class=\"tenant-preview-strip\">
              <div><dt>Candidate PRs</dt><dd>{preview_enablement_counts.get('candidate', len(preview_candidates))}</dd></div>
              <div><dt>Requested</dt><dd>{preview_enablement_counts.get('requested', 0)}</dd></div>
              <div><dt>Running</dt><dd>{preview_enablement_counts.get('running', preview_counts.get('live', 0))}</dd></div>
              <div><dt>Paused</dt><dd>{preview_enablement_counts.get('paused', 0)}</dd></div>
              <div><dt>Retained</dt><dd>{preview_enablement_counts.get('retained', preview_counts.get('retained', 0))}</dd></div>
            </dl>
            """
        dense_environment_html = ""
        if has_environment_evidence:
            dense_environment_html = f"""
            <div class=\"environment-board\">
              {render_environment_lane('testing', environments.get('testing') if isinstance(environments, dict) else None)}
              {render_environment_lane('prod', environments.get('prod') if isinstance(environments, dict) else None)}
            </div>
            {lane_actions_html}
            {promotion_stage_html if show_promotion_stage else ''}
            <dl class=\"tenant-preview-strip\">
              <div><dt>Candidate PRs</dt><dd>{preview_enablement_counts.get('candidate', len(preview_candidates))}</dd></div>
              <div><dt>Requested</dt><dd>{preview_enablement_counts.get('requested', 0)}</dd></div>
              <div><dt>Running</dt><dd>{preview_enablement_counts.get('running', preview_counts.get('live', 0))}</dd></div>
              <div><dt>Paused</dt><dd>{preview_enablement_counts.get('paused', 0)}</dd></div>
              <div><dt>Retained</dt><dd>{preview_enablement_counts.get('retained', preview_counts.get('retained', 0))}</dd></div>
            </dl>
            {preview_enablement_html}
            """
        tenant_stage_copy = (
            "Main feeds testing. Tested artifacts promote into prod. Pull requests become opt-in preview environments instead of shared dev branches."
            if has_environment_evidence
            else "Harbor has preview request evidence for this tenant, but it has not recorded current testing or prod lane evidence yet. Preview enablement is the first meaningful control surface until long-lived lane evidence arrives."
        )
        tenant_brief_label = "Promotion path" if has_environment_evidence else "Current Harbor focus"
        tenant_brief_copy = (
            escape(str(promotion_summary.get("summary", "No promotion evidence recorded yet.")))
            if has_environment_evidence
            else "Preview request state is available even before Harbor has current long-lived lane evidence for this tenant."
        )
        tenant_stage_html = f"""
        <section class=\"tenant-stage\">
          <div class=\"tenant-stage-grid\">
            <div>
              <div class=\"section-label\">Tenant environment</div>
              <h2>{tenant_label}</h2>
              <p class=\"tenant-stage-copy\">{tenant_stage_copy}</p>
            </div>
            <aside class=\"tenant-brief\">
              <div class=\"section-label\">{tenant_brief_label}</div>
              <p class=\"tenant-brief-copy\">{tenant_brief_copy}</p>
            </aside>
          </div>
          {dense_environment_html}
          {sparse_preview_html}
        </section>
        """
        roster_label = "Preview roster"
        roster_title = "Pull request previews"
        roster_summary = (
            "This tenant page keeps testing, prod, preview enablement, and lifecycle evidence together. The queue below is still where Harbor shows deeper preview detail."
        )

    body_html = f"""
    <div data-harbor-overview>
      {tenant_stage_html}
      <section class=\"index-mast\">
        <div class=\"index-mast-grid\">
          <div>
            <div class=\"section-label\">{roster_label}</div>
            <h2>{roster_title}</h2>
            <p data-overview-summary>{roster_summary}</p>
          </div>
          <aside class=\"focus-panel\">
            <div class=\"section-label\">Fleet focus</div>
            <p class=\"focus-kicker\">Filter the queue before opening detail pages.</p>
            <div class=\"focus-chip-row\" role=\"toolbar\" aria-label=\"Fleet focus filters\">{focus_controls_html}</div>
            {scope_panel_html}
            <p class=\"{focus_status_class}\" data-focus-status>{escape(filter_notes['all'])} Showing {len(preview_rows)} of {len(preview_rows)} previews.</p>
          </aside>
        </div>
        {summary_strip_html}
      </section>

      <div class=\"lane-grid\">{lane_html}</div>

      <section class=\"policy-strip\">
        <div class=\"section-label\">Policy snapshot</div>
        <h2>Preview control stance</h2>
        <ul>
          <li>Stable lanes such as local, testing, and prod stay separate from preview traffic.</li>
          <li>One Harbor preview identity maps to one anchor PR and rotates generations behind a stable route.</li>
          <li>Cleanup, retention, and companion-policy evidence should be visible here before Harbor grows write-side UI.</li>
        </ul>
      </section>
    </div>
    <script>
    (() => {{
      const root = document.querySelector('[data-harbor-overview]');
      if (!root) {{
        return;
      }}
      const filterNotes = {json.dumps(filter_notes)};
      const scopeNotes = {json.dumps(scope_notes)};
      const controls = Array.from(root.querySelectorAll('[data-filter-control]'));
      const scopeControls = Array.from(root.querySelectorAll('[data-scope-control]'));
      const rows = Array.from(root.querySelectorAll('[data-preview-row]'));
      const sections = Array.from(root.querySelectorAll('[data-lane-section]'));
      const status = root.querySelector('[data-focus-status]');
      const overviewSummary = root.querySelector('[data-overview-summary]');
      const defaultOverviewSummary = overviewSummary ? overviewSummary.textContent || '' : '';
      const validFocusKeys = new Set(controls.map((control) => control.dataset.filterControl || 'all'));
      const validScopeKeys = new Set(scopeControls.map((control) => control.dataset.scopeControl || 'all'));
      let activeFocus = 'all';
      let activeScope = 'all';
      const initialParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const initialFocus = initialParams.get('focus') || '';
      const initialScope = initialParams.get('scope') || '';
      if (validFocusKeys.has(initialFocus)) {{
        activeFocus = initialFocus;
      }}
      if (validScopeKeys.has(initialScope)) {{
        activeScope = initialScope;
      }}

      const matchesFilter = (row, filterKey) => {{
        const filters = (row.dataset.filters || '').split(' ').filter(Boolean);
        return filterKey === 'all' || filters.includes(filterKey);
      }};

      const matchesScope = (row, scopeKey) => {{
        const scopes = (row.dataset.scopes || '').split(' ').filter(Boolean);
        return scopeKey === 'all' || scopes.includes(scopeKey);
      }};

      const applyFilters = () => {{
        let visibleRows = 0;
        controls.forEach((control) => {{
          const isActive = control.dataset.filterControl === activeFocus;
          control.classList.toggle('is-active', isActive);
          control.setAttribute('aria-pressed', isActive ? 'true' : 'false');
        }});
        scopeControls.forEach((control) => {{
          const isActive = control.dataset.scopeControl === activeScope;
          control.classList.toggle('is-active', isActive);
          control.setAttribute('aria-pressed', isActive ? 'true' : 'false');
        }});
        rows.forEach((row) => {{
          const visible = matchesFilter(row, activeFocus) && matchesScope(row, activeScope);
          row.classList.toggle('is-hidden', !visible);
          if (visible) {{
            visibleRows += 1;
          }}
        }});
        sections.forEach((section) => {{
          const laneRows = Array.from(section.querySelectorAll('[data-preview-row]'));
          const visibleLaneRows = laneRows.filter((row) => !row.classList.contains('is-hidden'));
          section.classList.toggle('is-hidden', visibleLaneRows.length === 0);
          const count = section.querySelector('[data-lane-count]');
          if (count) {{
            count.textContent = `${{visibleLaneRows.length}} visible`;
          }}
        }});
        if (status) {{
          const focusNote = filterNotes[activeFocus] || filterNotes.all || '';
          const scopeNote = scopeNotes[activeScope] || scopeNotes.all || '';
          status.textContent = `${{focusNote}} ${{scopeNote}} Showing ${{visibleRows}} of ${{rows.length}} previews.`.trim();
        }}
        if (overviewSummary) {{
          if (activeFocus === 'all' && activeScope === 'all') {{
            overviewSummary.textContent = defaultOverviewSummary;
          }} else {{
            const fragments = [];
            if (activeScope !== 'all') {{
              fragments.push(scopeNotes[activeScope] || '');
            }}
            if (activeFocus !== 'all') {{
              fragments.push(filterNotes[activeFocus] || '');
            }}
            overviewSummary.textContent = `Showing ${{visibleRows}} of ${{rows.length}} previews. ${{fragments.filter(Boolean).join(' ')}}`.trim();
          }}
        }}
        const nextParams = new URLSearchParams();
        if (activeFocus !== 'all') {{
          nextParams.set('focus', activeFocus);
        }}
        if (activeScope !== 'all') {{
          nextParams.set('scope', activeScope);
        }}
        const nextHash = nextParams.toString();
        const nextUrl = `${{window.location.pathname}}${{window.location.search}}${{nextHash ? `#${{nextHash}}` : ''}}`;
        if (`${{window.location.pathname}}${{window.location.search}}${{window.location.hash}}` !== nextUrl) {{
          window.history.replaceState(null, '', nextUrl);
        }}
      }};

      controls.forEach((control) => {{
        control.addEventListener('click', () => {{
          activeFocus = control.dataset.filterControl || 'all';
          applyFilters();
        }});
      }});
      scopeControls.forEach((control) => {{
        control.addEventListener('click', () => {{
          activeScope = control.dataset.scopeControl || 'all';
          applyFilters();
        }});
      }});
      applyFilters();
    }})();
    </script>
    """

    extra_css = """
    .section-label {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .tenant-stage {
      display: grid;
      gap: 18px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 22px;
      margin-bottom: 18px;
    }
    .tenant-stage-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(300px, 0.75fr);
      gap: 18px;
      align-items: start;
    }
    .tenant-stage h2,
    .environment-lane h3 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.02;
    }
    .tenant-stage h2 {
      font-size: 42px;
    }
    .tenant-stage-copy,
    .tenant-brief-copy,
    .environment-summary,
    .environment-note {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .tenant-brief {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 16px;
      padding: 14px 16px 16px;
      display: grid;
      gap: 10px;
    }
    .tenant-brief-copy {
      color: var(--text);
    }
    .promotion-stage {
      display: grid;
      gap: 14px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }
    .lane-actions {
      display: grid;
      gap: 14px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }
    .lane-actions-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
    }
    .lane-actions h3,
    .lane-action-note h3 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.04;
    }
    .lane-actions h3 {
      font-size: 28px;
    }
    .lane-actions-copy,
    .lane-action-note p {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }
    .lane-actions-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .lane-action-note {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 14px;
      padding: 14px 16px 16px;
      display: grid;
      gap: 8px;
    }
    .lane-action-note h3 {
      font-size: 22px;
    }
    .promotion-stage-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
    }
    .promotion-stage h3,
    .promotion-check h4 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.04;
    }
    .promotion-stage h3 {
      font-size: 30px;
    }
    .promotion-stage-copy,
    .promotion-next-action,
    .promotion-check p {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .promotion-stage-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(0, 0.95fr);
      gap: 16px;
      align-items: start;
    }
    .promotion-primary {
      display: grid;
      gap: 12px;
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 16px;
      padding: 14px 16px 16px;
    }
    .promotion-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
      margin: 0;
    }
    .promotion-meta > div {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .promotion-meta dd {
      margin: 7px 0 0;
      overflow-wrap: anywhere;
    }
    .promotion-meta code {
      font-family: var(--mono);
      font-size: 12px;
    }
    .promotion-next-action {
      color: var(--text);
    }
    .promotion-evidence {
      display: grid;
      gap: 12px;
    }
    .promotion-check {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 14px;
      padding: 14px 16px;
      display: grid;
      gap: 8px;
    }
    .promotion-check-good { border-left: 3px solid var(--good); }
    .promotion-check-warn { border-left: 3px solid var(--warn); }
    .promotion-check-bad { border-left: 3px solid var(--bad); }
    .promotion-check-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .promotion-check h4 {
      font-size: 22px;
    }
    .promotion-recipes {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .action-card {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 14px;
      padding: 14px 16px 16px;
      display: grid;
      gap: 12px;
    }
    .action-card.tone-good,
    .action-card.tone-warn,
    .action-card.tone-bad,
    .action-card.tone-neutral {
      background: var(--surface);
      color: inherit;
    }
    .action-card.tone-good { border-left: 3px solid var(--good); }
    .action-card.tone-warn { border-left: 3px solid var(--warn); }
    .action-card.tone-bad { border-left: 3px solid var(--bad); }
    .action-card.tone-neutral { border-left: 3px solid var(--neutral); }
    .action-card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .action-command {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .action-card h3 {
      margin: 0;
      font-family: var(--serif);
      font-size: 22px;
      line-height: 1.08;
    }
    .action-card p {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.55;
    }
    .action-footer,
    .lane-action-links,
    .environment-links {
      margin: 0;
    }
    .lane-detail-link {
      font-family: var(--mono);
      font-size: 12px;
      text-decoration: none;
      text-underline-offset: 0.18em;
    }
    .lane-detail-link:hover {
      text-decoration: underline;
    }
    .copy-button {
      -webkit-appearance: none;
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f2ede3;
      padding: 7px 10px;
      color: var(--text);
      cursor: pointer;
      font: inherit;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .copy-button:hover {
      background: #e7dfd0;
    }
    .action-details {
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .action-details summary {
      cursor: pointer;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      list-style: none;
    }
    .action-details summary::-webkit-details-marker {
      display: none;
    }
    .action-pre {
      margin: 12px 0 0;
      overflow: auto;
      padding: 16px;
      background: #13110f;
      color: #e7e0d4;
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.55;
    }
    .action-empty {
      margin: 0;
      color: var(--muted);
    }
    .tenant-preview-strip {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin: 0;
    }
    .tenant-preview-strip > div,
    .environment-meta > div {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .tenant-preview-strip dd,
    .environment-meta dd {
      margin: 7px 0 0;
      overflow-wrap: anywhere;
    }
    .tenant-preview-strip dd {
      font-family: var(--serif);
      font-size: 22px;
    }
    .tenant-enablement {
      display: grid;
      gap: 12px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }
    .tenant-enablement-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
    }
    .tenant-enablement h3,
    .enablement-row h3 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.02;
    }
    .tenant-enablement h3 {
      font-size: 28px;
    }
    .tenant-enablement-copy,
    .enablement-overflow,
    .enablement-row p,
    .enablement-meta {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }
    .enablement-list {
      display: grid;
      gap: 12px;
    }
    .enablement-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 14px;
      padding: 14px 16px;
      align-items: center;
    }
    .enablement-inline-action,
    .enablement-inline-note {
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px 14px;
      margin-top: 2px;
    }
    .enablement-inline-action {
      display: grid;
      gap: 10px;
    }
    .enablement-inline-action.tone-good,
    .enablement-inline-action.tone-warn,
    .enablement-inline-action.tone-bad,
    .enablement-inline-action.tone-neutral,
    .enablement-inline-note.tone-good,
    .enablement-inline-note.tone-warn,
    .enablement-inline-note.tone-bad,
    .enablement-inline-note.tone-neutral {
      background: var(--surface);
      color: inherit;
    }
    .enablement-inline-action.tone-good,
    .enablement-inline-note.tone-good { border-left: 3px solid var(--good); }
    .enablement-inline-action.tone-warn,
    .enablement-inline-note.tone-warn { border-left: 3px solid var(--warn); }
    .enablement-inline-action.tone-bad,
    .enablement-inline-note.tone-bad { border-left: 3px solid var(--bad); }
    .enablement-inline-action.tone-neutral,
    .enablement-inline-note.tone-neutral { border-left: 3px solid var(--neutral); }
    .enablement-inline-action-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .enablement-inline-action h4,
    .enablement-inline-note h4 {
      margin: 0;
      font-family: var(--serif);
      font-size: 20px;
      line-height: 1.08;
    }
    .enablement-inline-action p,
    .enablement-inline-note p {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.55;
    }
    .enablement-inline-note.tone-bad h4 {
      color: var(--bad);
    }
    .enablement-row-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .enablement-row-head h3 {
      font-size: 24px;
    }
    .enablement-row-tones {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: end;
    }
    .enablement-status {
      margin-top: 6px;
      color: var(--text);
    }
    .enablement-row-actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: end;
      gap: 12px;
      align-items: center;
    }
    .enablement-row-actions a,
    .enablement-meta {
      font-family: var(--mono);
      font-size: 12px;
      text-decoration: none;
      text-underline-offset: 0.18em;
    }
    .enablement-row-actions a:hover {
      text-decoration: underline;
    }
    .environment-board {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }
    .environment-lane {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 16px;
      padding: 16px 18px 18px;
      display: grid;
      gap: 12px;
    }
    .environment-lane-empty {
      background: rgba(251, 250, 246, 0.7);
    }
    .environment-lane-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .environment-lane h3 {
      font-size: 28px;
      text-transform: capitalize;
    }
    .environment-tones {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: end;
    }
    .environment-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
      margin: 0;
    }
    .environment-meta code {
      font-family: var(--mono);
      font-size: 12px;
    }
    .index-mast {
      display: grid;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }
    .index-mast-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
      gap: 18px;
      align-items: start;
    }
    .index-mast h2,
    .policy-strip h2,
    .lane-section h2 {
      margin: 0;
      font-family: var(--serif);
      font-size: 34px;
      line-height: 1.02;
    }
    .index-mast p,
    .policy-strip p,
    .lane-section p {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.65;
      max-width: 58ch;
    }
    .focus-panel {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 14px;
      padding: 12px 14px 14px;
      display: grid;
      gap: 8px;
    }
    .focus-panel p {
      margin: 0;
      max-width: none;
    }
    .focus-kicker {
      color: var(--text);
      font-size: 15px;
      line-height: 1.45;
    }
    .focus-chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .focus-chip {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      border: 1px solid var(--line);
      background: transparent;
      border-radius: 999px;
      padding: 8px 12px;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
    }
    .focus-chip strong {
      font-family: var(--mono);
      font-size: 12px;
      color: var(--text);
    }
    .focus-chip.is-active {
      background: var(--text);
      border-color: var(--text);
      color: #f8f4ed;
    }
    .focus-chip.is-active strong { color: inherit; }
    .focus-status {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .focus-status-hidden {
      display: none;
    }
    .scope-panel {
      display: grid;
      gap: 8px;
    }
    .scope-panel .section-label {
      margin-bottom: 0;
    }
    .scope-chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .scope-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      background: #f2ede3;
      border-radius: 999px;
      padding: 7px 10px;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
    }
    .scope-chip strong {
      font-family: var(--mono);
      font-size: 11px;
      color: var(--text);
    }
    .scope-chip.is-active {
      border-color: var(--line-strong);
      background: #e7dfd0;
      color: var(--text);
    }
    .scope-chip.is-active strong { color: inherit; }
    .summary-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin: 4px 0 0;
    }
    .summary-strip > div {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 12px;
      padding: 12px 14px;
    }
    .summary-strip dt,
    .preview-row-meta dt {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .summary-strip dd {
      margin: 8px 0 0;
      font-family: var(--serif);
      font-size: 22px;
    }
    .policy-strip,
    .lane-section {
      border-top: 1px solid var(--line);
      padding-top: 18px;
      margin-top: 24px;
    }
    .policy-strip ul {
      margin: 14px 0 0;
      padding-left: 18px;
      color: var(--muted);
      display: grid;
      gap: 10px;
      line-height: 1.6;
    }
    .lane-section-head {
      display: flex;
      gap: 14px;
      justify-content: space-between;
      align-items: end;
    }
    .lane-count {
      flex: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .lane-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 24px;
      margin-top: 20px;
    }
    .lane-stack {
      display: grid;
      gap: 16px;
      margin-top: 16px;
    }
    .preview-row {
      border: 1px solid var(--line);
      background: var(--surface);
      padding: 18px;
      border-radius: 14px;
    }
    .preview-row.is-hidden,
    .lane-section.is-hidden {
      display: none;
    }
    .preview-row-head {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .preview-row-head h3 {
      margin: 0;
      font-family: var(--serif);
      font-size: 26px;
      line-height: 1.04;
    }
    .preview-row-title { text-decoration: none; }
    .preview-row-title:hover { text-decoration: underline; text-underline-offset: 0.18em; }
    .preview-row-head p,
    .preview-row-head a {
      margin: 8px 0 0;
      color: var(--muted);
      overflow-wrap: anywhere;
      text-underline-offset: 0.18em;
    }
    .preview-row-tones {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .tone-pill {
      border-radius: 999px;
      padding: 8px 10px;
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #f5f2ec;
    }
    .tone-good { background: var(--good); }
    .tone-warn { background: var(--warn); }
    .tone-bad { background: var(--bad); }
    .tone-neutral { background: var(--neutral); }
    .preview-row-signals {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .signal-chip {
      border-radius: 999px;
      padding: 6px 10px;
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      border: 1px solid var(--line);
      background: #f2ede3;
      color: var(--muted);
    }
    .signal-good {
      background: rgba(28, 93, 61, 0.1);
      border-color: rgba(28, 93, 61, 0.22);
      color: var(--good);
    }
    .signal-warn {
      background: rgba(138, 98, 8, 0.1);
      border-color: rgba(138, 98, 8, 0.22);
      color: var(--warn);
    }
    .signal-bad {
      background: rgba(138, 49, 44, 0.1);
      border-color: rgba(138, 49, 44, 0.24);
      color: var(--bad);
    }
    .preview-row-summary {
      margin: 14px 0 0;
      color: var(--text);
      line-height: 1.6;
    }
    .preview-row-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 14px;
    }
    .preview-row-actions a {
      font-family: var(--mono);
      font-size: 12px;
      text-decoration: none;
      text-underline-offset: 0.18em;
    }
    .preview-row-actions a:hover { text-decoration: underline; }
    .preview-row-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin: 16px 0 0;
    }
    .preview-row-meta dd { margin: 8px 0 0; overflow-wrap: anywhere; }
    .preview-row-meta code { font-family: var(--mono); font-size: 12px; }
    .lane-empty { margin: 16px 0 0; color: var(--muted); }
    @media (max-width: 900px) {
      .tenant-stage-grid,
      .environment-board,
      .lane-actions-grid,
      .promotion-stage-grid,
      .promotion-recipes,
      .index-mast-grid,
      .tenant-preview-strip,
      .lane-grid,
      .preview-row-meta,
      .summary-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .tenant-stage-grid,
      .index-mast-grid {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 640px) {
      .section-label {
        margin-bottom: 6px;
      }
      .tenant-preview-strip,
      .lane-actions-grid,
      .promotion-stage-grid,
      .promotion-recipes,
      .enablement-row,
      .environment-board,
      .summary-strip,
      .lane-grid,
      .preview-row-meta,
      .environment-meta,
      .promotion-meta { grid-template-columns: 1fr; }
      .shell-brand h1,
      .tenant-stage h2,
      .index-mast h2,
      .policy-strip h2,
      .lane-section h2 {
        font-size: 24px;
      }
      .tenant-stage {
        gap: 12px;
        padding-bottom: 14px;
        margin-bottom: 12px;
      }
      .shell-brand p,
      .tenant-stage-copy,
      .tenant-brief-copy,
      .environment-summary,
      .environment-note,
      .index-mast p,
      .policy-strip p,
      .lane-section p,
      .focus-kicker,
      .focus-status {
        font-size: 14px;
        line-height: 1.45;
      }
      .shell-nav {
        gap: 8px;
      }
      .shell-nav-item {
        padding: 7px 10px;
        font-size: 11px;
      }
      .index-mast {
        gap: 8px;
        padding-bottom: 10px;
      }
      .focus-panel {
        gap: 6px;
        padding: 10px 12px 12px;
      }
      .tenant-brief,
      .environment-lane,
      .promotion-primary,
      .promotion-check {
        padding: 12px 14px 14px;
      }
      .tenant-enablement-head,
      .lane-actions-head,
      .promotion-stage-head,
      .promotion-check-head,
      .enablement-inline-action-head,
      .enablement-row-head,
      .enablement-row-actions {
        display: grid;
        gap: 10px;
      }
      .environment-lane h3 {
        font-size: 22px;
      }
      .promotion-stage h3,
      .lane-actions h3,
      .promotion-check h4 {
        font-size: 22px;
      }
      .focus-status,
      .summary-strip {
        display: none;
      }
      .focus-chip-row {
        flex-wrap: nowrap;
        overflow-x: auto;
        overscroll-behavior-x: contain;
        scrollbar-width: none;
        padding-bottom: 2px;
        margin-right: -2px;
      }
      .focus-chip-row::-webkit-scrollbar {
        display: none;
      }
      .scope-chip-row {
        flex-wrap: nowrap;
        overflow-x: auto;
        overscroll-behavior-x: contain;
        scrollbar-width: none;
        padding-bottom: 2px;
      }
      .scope-chip-row::-webkit-scrollbar {
        display: none;
      }
      .focus-chip {
        flex: 0 0 auto;
        padding: 7px 10px;
        gap: 8px;
      }
      .scope-chip {
        flex: 0 0 auto;
      }
      .focus-chip strong,
      .focus-chip span,
      .scope-chip strong,
      .scope-chip span {
        white-space: nowrap;
      }
      .lane-section-head,
      .preview-row-head {
        align-items: start;
      }
      .lane-section-head p {
        display: none;
      }
      .lane-count {
        padding: 6px 8px;
        font-size: 10px;
      }
      .policy-strip,
      .lane-section {
        padding-top: 12px;
        margin-top: 14px;
      }
      .preview-row {
        padding: 16px;
      }
    }
    """

    return _render_harbor_shell_document(
        page_title=f"Harbor preview index{' · ' + context_name if context_name else ''}",
        context_name=context_name,
        active_nav="overview",
        body_class="index-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def _render_harbor_preview_policy_page_html(
    payload: dict[str, object],
    *,
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", ""))
    previews = payload.get("previews") if isinstance(payload.get("previews"), list) else []
    preview_rows = [item for item in previews if isinstance(item, dict)]
    active_preview_count = sum(1 for row in preview_rows if str(row.get("state", "")).strip().lower() != "destroyed")
    retained_preview_count = sum(1 for row in preview_rows if str(row.get("state", "")).strip().lower() == "destroyed")
    overview_href = escape((nav_links or {}).get("overview", "index.html") or "index.html")
    context_distribution_rows = []
    for context_value in sorted(
        {str(row.get("context", "")).strip() for row in preview_rows if str(row.get("context", "")).strip()}
    ):
        matching_rows = [row for row in preview_rows if str(row.get("context", "")).strip() == context_value]
        active_count = sum(1 for row in matching_rows if str(row.get("state", "")).strip().lower() != "destroyed")
        retained_count = sum(1 for row in matching_rows if str(row.get("state", "")).strip().lower() == "destroyed")
        context_link = f"{overview_href}#scope=context:{escape(context_value)}"
        context_distribution_rows.append(
            "<tr>"
            f"<td><a href=\"{context_link}\">{escape(context_value)}</a></td>"
            f"<td>{len(matching_rows)}</td>"
            f"<td>{active_count}</td>"
            f"<td>{retained_count}</td>"
            "</tr>"
        )
    context_distribution_html = ""
    if len(context_distribution_rows) > 1:
        context_distribution_html = f"""
    <section class=\"policy-section\">
      <div class=\"section-label\">Fleet footprint</div>
      <h2>Context distribution</h2>
      <p>When Harbor is showing more than one tenant context, this page should still reveal how the current preview fleet is distributed across those contexts.</p>
      <table>
        <thead><tr><th>Context</th><th>Total</th><th>Active</th><th>Retained</th></tr></thead>
        <tbody>{''.join(context_distribution_rows)}</tbody>
      </table>
    </section>
    """
    eligible_context_rows = "".join(
        f"<tr><td>{escape(repo)}</td><td>{escape(context)}</td></tr>"
        for repo, context in sorted(HARBOR_TENANT_ANCHOR_CONTEXTS.items())
    )
    companion_items = "".join(
        f"<li><code>{escape(repo)}</code></li>" for repo in HARBOR_ALLOWED_COMPANION_REPOS
    )
    preview_label_example = escape("<context>/<anchor-repo>/pr-<number>")
    preview_route_example = escape("/previews/<context>/<anchor-repo>/pr-<number>")

    body_html = f"""
    <section class=\"policy-mast\">
      <div class=\"section-label\">Read-only policy</div>
      <h2>How Harbor decides what becomes a preview</h2>
      <p>This page exposes the current preview contract as operator evidence. GitHub supplies PR events and identity; Harbor decides eligibility, route shape, baseline input defaults, and preview retention behavior.</p>
    </section>

    <section class=\"policy-grid\">
      <article class=\"policy-card\">
        <div class=\"section-label\">Current queue</div>
        <h3>Observed state</h3>
        <dl class=\"policy-stats\">
          <div><dt>Context</dt><dd>{escape(context_name) or 'all contexts'}</dd></div>
          <div><dt>Active previews</dt><dd>{active_preview_count}</dd></div>
          <div><dt>Retained evidence</dt><dd>{retained_preview_count}</dd></div>
          <div><dt>Total records</dt><dd>{len(preview_rows)}</dd></div>
        </dl>
      </article>
      <article class=\"policy-card\">
        <div class=\"section-label\">Enablement</div>
        <h3>Preview request gate</h3>
        <p>Harbor can enable a PR preview from the anchor PR label <code>{escape(HARBOR_PREVIEW_ENABLE_LABEL)}</code> or from an explicit Harbor-side request. Once requested, manifest-changing PR events can refresh the same preview identity.</p>
      </article>
    </section>

    {context_distribution_html}

    <section class=\"policy-section\">
      <div class=\"section-label\">Anchor policy</div>
      <h2>Eligible anchor repositories</h2>
      <p>Harbor only anchors preview identities from tenant repositories that resolve to a known control-plane context.</p>
      <table>
        <thead><tr><th>Anchor repo</th><th>Context</th></tr></thead>
        <tbody>{eligible_context_rows}</tbody>
      </table>
    </section>

    <section class=\"policy-grid\">
      <article class=\"policy-card\">
        <div class=\"section-label\">Preview metadata</div>
        <h3>PR body contract</h3>
        <p>Harbor reads one fenced metadata block from the anchor PR body using info string <code>{escape(HARBOR_PREVIEW_REQUEST_BLOCK_INFO_STRING)}</code>. The default baseline channel is <code>{escape(DEFAULT_HARBOR_BASELINE_CHANNEL)}</code>.</p>
      </article>
      <article class=\"policy-card\">
        <div class=\"section-label\">Companions</div>
        <h3>Allowlisted companion repos</h3>
        <p>Companion refs are explicit, PR-based, and allowlisted. Harbor does not accept raw branch-name or SHA overrides here.</p>
        <ul class=\"policy-list\">{companion_items or '<li>None</li>'}</ul>
      </article>
    </section>

    <section class=\"policy-grid\">
      <article class=\"policy-card\">
        <div class=\"section-label\">Identity</div>
        <h3>Preview naming</h3>
        <p>Human-readable preview labels follow <code>{preview_label_example}</code>. Harbor keeps one stable preview identity per anchor PR and rotates generations behind it.</p>
      </article>
      <article class=\"policy-card\">
        <div class=\"section-label\">Routing</div>
        <h3>Stable review route</h3>
        <p>Preview URLs are expected to follow a routed path shape such as <code>{preview_route_example}</code> rather than creating a new permanent environment lane.</p>
      </article>
    </section>

    <section class=\"policy-section\">
      <div class=\"section-label\">Lifecycle stance</div>
      <h2>Retention and cleanup</h2>
      <ul class=\"policy-list\">
        <li>Stable long-lived lanes such as local, testing, and prod remain distinct from preview traffic.</li>
        <li>Destroyed previews remain visible as retained evidence instead of disappearing from the operator surface.</li>
        <li>Harbor treats preview records and generation records as canonical control-plane evidence, not transient UI state.</li>
      </ul>
    </section>
    """

    extra_css = """
    .policy-mast,
    .policy-section {
      border-top: 1px solid var(--line);
      padding-top: 22px;
    }
    .policy-mast { border-top: 0; padding-top: 0; }
    .policy-mast h2,
    .policy-section h2,
    .policy-card h3 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.06;
    }
    .policy-mast h2,
    .policy-section h2 { font-size: 34px; }
    .policy-card h3 { font-size: 24px; }
    .policy-mast p,
    .policy-section p,
    .policy-card p {
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.65;
      max-width: 64ch;
    }
    .section-label {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .policy-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 20px;
      margin-top: 30px;
    }
    .policy-card {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 14px;
      padding: 18px;
    }
    .policy-stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin: 16px 0 0;
    }
    .policy-stats dt {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .policy-stats dd {
      margin: 8px 0 0;
      font-family: var(--serif);
      font-size: 28px;
      overflow-wrap: anywhere;
    }
    .policy-list {
      margin: 16px 0 0;
      padding-left: 18px;
      color: var(--muted);
      display: grid;
      gap: 10px;
      line-height: 1.6;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 18px;
      font-size: 14px;
      background: transparent;
    }
    th, td {
      text-align: left;
      padding: 11px 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    code { font-family: var(--mono); font-size: 12px; }
    @media (max-width: 900px) {
      .policy-grid,
      .policy-stats { grid-template-columns: 1fr; }
    }
    """

    return _render_harbor_shell_document(
        page_title=f"Harbor preview policy{' · ' + context_name if context_name else ''}",
        context_name=context_name,
        active_nav="policy",
        body_class="index-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def _render_harbor_promotion_status_page_html(
    payload: dict[str, object],
    *,
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", "")).strip()
    path_label = str(payload.get("path_label", "")).strip() or f"{context_name}/testing-to-prod"
    tone = str(payload.get("tone", "neutral")).strip() or "neutral"
    headline = escape(str(payload.get("headline", "Harbor cannot describe the promotion path yet.")))
    summary = escape(str(payload.get("summary", "No promotion summary recorded.")))
    next_action = escape(str(payload.get("next_action", "No next action recorded.")))
    retained_evidence = escape(str(payload.get("retained_evidence", "No retained evidence summary recorded.")))
    candidate_artifact_id = escape(str(payload.get("candidate_artifact_id", "")) or "Unavailable")
    current_prod_artifact_id = escape(str(payload.get("current_prod_artifact_id", "")) or "Unavailable")
    source_git_ref = escape(str(payload.get("source_git_ref", "")) or "Unavailable")
    status_label = escape(str(payload.get("status", "unknown")).replace("_", " "))
    evidence_checks = payload.get("evidence_checks") if isinstance(payload.get("evidence_checks"), list) else []
    latest_backup_gate = payload.get("latest_backup_gate") if isinstance(payload.get("latest_backup_gate"), dict) else None
    latest_promotion = payload.get("latest_promotion") if isinstance(payload.get("latest_promotion"), dict) else None
    recent_backup_gates_payload = payload.get("recent_backup_gates")
    recent_backup_gates = (
        list(recent_backup_gates_payload)
        if isinstance(recent_backup_gates_payload, (list, tuple))
        else []
    )
    recent_promotions_payload = payload.get("recent_promotions")
    recent_promotions = (
        list(recent_promotions_payload)
        if isinstance(recent_promotions_payload, (list, tuple))
        else []
    )
    testing_live = payload.get("testing_live") if isinstance(payload.get("testing_live"), dict) else None
    prod_live = payload.get("prod_live") if isinstance(payload.get("prod_live"), dict) else None

    evidence_html = "".join(
        f"""
        <article class=\"promotion-detail-check promotion-detail-check-{('good' if str(check.get('status', '')).strip().lower() == 'pass' else 'bad' if str(check.get('status', '')).strip().lower() == 'fail' else 'warn')}\">
          <div class=\"promotion-detail-check-head\">
            <h4>{escape(str(check.get('label', 'Evidence')))}</h4>
            <span class=\"signal-chip signal-{('good' if str(check.get('status', '')).strip().lower() == 'pass' else 'bad' if str(check.get('status', '')).strip().lower() == 'fail' else 'warn')}\">{escape(_status_label(str(check.get('status', 'pending'))))}</span>
          </div>
          <p>{escape(str(check.get('detail', 'No evidence detail recorded.')))}</p>
        </article>
        """
        for check in evidence_checks
        if isinstance(check, dict)
    ) or '<p class="table-empty">No promotion evidence checks recorded yet.</p>'

    recipe_cards: list[str] = []
    backup_gate_recipe = str(payload.get("backup_gate_recipe", "")).strip()
    if backup_gate_recipe:
        recipe_cards.append(
            _render_harbor_action_recipe(
                title="Record prod backup gate",
                summary="Persist the exact backup authorization Harbor expects before trying to promote into prod.",
                tone="warn",
                script=backup_gate_recipe,
                command_label="backup-gates write",
                recipe_id=f"promotion-detail-{escape(context_name)}-backup-gate",
            )
        )
    resolve_recipe = str(payload.get("resolve_recipe", "")).strip()
    if resolve_recipe:
        recipe_cards.append(
            _render_harbor_action_recipe(
                title="Plan promotion request",
                summary="Resolve Harbor's typed promotion request from the current tenant evidence before execution.",
                tone=tone,
                script=resolve_recipe,
                command_label="promote resolve",
                recipe_id=f"promotion-detail-{escape(context_name)}-resolve",
            )
        )
    execute_recipe = str(payload.get("execute_recipe", "")).strip()
    if execute_recipe:
        recipe_cards.append(
            _render_harbor_action_recipe(
                title="Execute promotion",
                summary="Run the resolved promotion request once the typed payload looks correct.",
                tone=tone,
                script=execute_recipe,
                command_label="promote execute",
                recipe_id=f"promotion-detail-{escape(context_name)}-execute",
            )
        )
    recipe_html = "".join(recipe_cards) or (
        '<p class="table-empty">Harbor is not exposing a promotion recipe for the current tenant state yet.</p>'
    )

    def render_live_lane_card(title: str, lane_payload: dict[str, object] | None) -> str:
        if lane_payload is None:
            return f"""
            <article class=\"promotion-lane-card promotion-lane-card-empty\">
              <div class=\"section-label\">{escape(title)}</div>
              <h3>No lane evidence</h3>
              <p>Harbor has not recorded current live inventory for this lane yet.</p>
            </article>
            """
        return f"""
        <article class=\"promotion-lane-card\">
          <div class=\"section-label\">{escape(title)}</div>
          <h3><code>{escape(str(lane_payload.get('artifact_id', '')) or 'Unavailable')}</code></h3>
          <p>{escape(str(lane_payload.get('source_git_ref', '')) or 'No source ref recorded.')}</p>
          <dl class=\"promotion-lane-meta\">
            <div><dt>Updated</dt><dd>{escape(str(lane_payload.get('updated_at', '')) or 'Unavailable')}</dd></div>
            <div><dt>Deploy</dt><dd>{escape(str(lane_payload.get('deploy_status', '')) or 'Unavailable')}</dd></div>
            <div><dt>Health</dt><dd>{escape(str(lane_payload.get('destination_health_status', '')) or 'Unavailable')}</dd></div>
            <div><dt>Record</dt><dd><code>{escape(str(lane_payload.get('deployment_record_id', '')) or 'Unavailable')}</code></dd></div>
          </dl>
        </article>
        """

    recent_promotions_html = "<p class=\"table-empty\">No promotion history recorded yet.</p>"
    if recent_promotions:
        recent_promotions_html = (
            "<table><thead><tr><th>Promotion record</th><th>From lane</th><th>Artifact</th><th>Backup</th><th>Health</th><th>Finished</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td><code>{escape(str(row.get('record_id', '')) or 'Unavailable')}</code></td>"
                f"<td>{escape(str(row.get('from_instance', '')) or 'Unavailable')}</td>"
                f"<td><code>{escape(str(row.get('artifact_id', '')) or 'Unavailable')}</code></td>"
                f"<td>{escape(str(row.get('backup_status', '')) or 'Unavailable')}</td>"
                f"<td>{escape(str(row.get('destination_health_status', '')) or 'Unavailable')}</td>"
                f"<td>{escape(str(row.get('finished_at', '')) or 'Unavailable')}</td>"
                "</tr>"
                for row in recent_promotions
                if isinstance(row, dict)
            )
            + "</tbody></table>"
        )

    recent_backup_gates_html = "<p class=\"table-empty\">No prod backup-gate history recorded yet.</p>"
    if recent_backup_gates:
        recent_backup_gates_html = (
            "<table><thead><tr><th>Backup gate</th><th>Status</th><th>Source</th><th>Created</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td><code>{escape(str(row.get('record_id', '')) or 'Unavailable')}</code></td>"
                f"<td>{escape(str(row.get('status', '')) or 'Unavailable')}</td>"
                f"<td>{escape(str(row.get('source', '')) or 'Unavailable')}</td>"
                f"<td>{escape(str(row.get('created_at', '')) or 'Unavailable')}</td>"
                "</tr>"
                for row in recent_backup_gates
                if isinstance(row, dict)
            )
            + "</tbody></table>"
        )

    latest_backup_gate_html = (
        f"<code>{escape(str(latest_backup_gate.get('record_id', '')) or 'Unavailable')}</code>"
        if latest_backup_gate is not None
        else "Unavailable"
    )
    latest_promotion_html = (
        f"<code>{escape(str(latest_promotion.get('record_id', '')) or 'Unavailable')}</code>"
        if latest_promotion is not None
        else "Unavailable"
    )

    body_html = f"""
    <section class=\"promotion-detail-mast\">
      <div>
        <div class=\"section-label\">Promotion detail</div>
        <h2>{escape(path_label)}</h2>
        <p>{summary}</p>
      </div>
      <aside class=\"promotion-detail-brief\">
        <span class=\"tone-pill tone-{escape(tone)}\">{status_label}</span>
        <p>{next_action}</p>
      </aside>
    </section>

    <section class=\"promotion-detail-grid\">
      <article class=\"promotion-summary-card\">
        <div class=\"section-label\">Current path</div>
        <h3>{headline}</h3>
        <dl class=\"promotion-summary-meta\">
          <div><dt>Candidate artifact</dt><dd><code>{candidate_artifact_id}</code></dd></div>
          <div><dt>Current prod</dt><dd><code>{current_prod_artifact_id}</code></dd></div>
          <div><dt>Testing source ref</dt><dd><code>{source_git_ref}</code></dd></div>
          <div><dt>Latest backup gate</dt><dd>{latest_backup_gate_html}</dd></div>
          <div><dt>Latest promotion</dt><dd>{latest_promotion_html}</dd></div>
          <div><dt>Harbor retains</dt><dd>{retained_evidence}</dd></div>
        </dl>
      </article>
      <div class=\"promotion-lane-grid\">
        {render_live_lane_card('Testing lane', testing_live)}
        {render_live_lane_card('Prod lane', prod_live)}
      </div>
    </section>

    <section class=\"promotion-detail-section\">
      <div class=\"section-label\">Evidence checks</div>
      <h3>What Harbor is using to gate promotion</h3>
      <div class=\"promotion-detail-check-grid\">{evidence_html}</div>
    </section>

    <section class=\"promotion-detail-section\">
      <div class=\"section-label\">Typed actions</div>
      <h3>What Harbor can do next</h3>
      <div class=\"promotion-detail-recipes\">{recipe_html}</div>
    </section>

    <section class=\"promotion-detail-section\">
      <div class=\"section-label\">Promotion history</div>
      <h3>Recent promotions into prod</h3>
      {recent_promotions_html}
    </section>

    <section class=\"promotion-detail-section\">
      <div class=\"section-label\">Backup-gate history</div>
      <h3>Recent prod backup authorization</h3>
      {recent_backup_gates_html}
    </section>
    """

    extra_css = """
    .section-label {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .promotion-detail-mast {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 18px;
      align-items: start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 20px;
    }
    .promotion-detail-mast h2,
    .promotion-summary-card h3,
    .promotion-lane-card h3,
    .promotion-detail-section h3,
    .promotion-detail-check h4 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.04;
    }
    .promotion-detail-mast h2 { font-size: 40px; }
    .promotion-detail-mast p,
    .promotion-detail-brief p,
    .promotion-summary-card p,
    .promotion-lane-card p,
    .promotion-detail-check p,
    .table-empty {
      margin: 10px 0 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .promotion-detail-brief,
    .promotion-summary-card,
    .promotion-lane-card,
    .promotion-detail-check,
    .promotion-detail-section {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 16px;
      padding: 16px 18px 18px;
    }
    .promotion-detail-brief {
      display: grid;
      gap: 10px;
      align-content: start;
    }
    .promotion-detail-grid,
    .promotion-lane-grid,
    .promotion-detail-check-grid,
    .promotion-detail-recipes {
      display: grid;
      gap: 16px;
      margin-top: 18px;
    }
    .promotion-detail-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .promotion-lane-grid,
    .promotion-detail-recipes {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .promotion-detail-check-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .promotion-summary-meta,
    .promotion-lane-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
      margin: 16px 0 0;
    }
    .promotion-summary-meta > div,
    .promotion-lane-meta > div {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .promotion-summary-meta dt,
    .promotion-lane-meta dt,
    th {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .promotion-summary-meta dd,
    .promotion-lane-meta dd {
      margin: 7px 0 0;
      overflow-wrap: anywhere;
    }
    .promotion-summary-meta code,
    .promotion-lane-card code,
    table code,
    .action-pre {
      font-family: var(--mono);
      font-size: 12px;
    }
    .promotion-detail-check-good { border-left: 3px solid var(--good); }
    .promotion-detail-check-warn { border-left: 3px solid var(--warn); }
    .promotion-detail-check-bad { border-left: 3px solid var(--bad); }
    .promotion-detail-check-head,
    .action-card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .action-card {
      display: grid;
      gap: 12px;
    }
    .action-card.tone-good,
    .action-card.tone-warn,
    .action-card.tone-bad,
    .action-card.tone-neutral {
      background: var(--surface);
      color: inherit;
    }
    .action-card.tone-good { border-left: 3px solid var(--good); }
    .action-card.tone-warn { border-left: 3px solid var(--warn); }
    .action-card.tone-bad { border-left: 3px solid var(--bad); }
    .action-card.tone-neutral { border-left: 3px solid var(--neutral); }
    .action-command {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .copy-button {
      -webkit-appearance: none;
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f2ede3;
      padding: 7px 10px;
      color: var(--text);
      cursor: pointer;
      font: inherit;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .copy-button:hover { background: #e7dfd0; }
    .action-details {
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .action-details summary {
      cursor: pointer;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      list-style: none;
    }
    .action-details summary::-webkit-details-marker { display: none; }
    .action-pre {
      margin: 12px 0 0;
      overflow: auto;
      padding: 16px;
      background: #13110f;
      color: #e7e0d4;
      border-radius: 8px;
      line-height: 1.55;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      font-size: 14px;
      background: transparent;
    }
    th, td {
      text-align: left;
      padding: 11px 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    .promotion-detail-section { margin-top: 18px; }
    @media (max-width: 900px) {
      .promotion-detail-mast,
      .promotion-lane-grid,
      .promotion-detail-check-grid,
      .promotion-detail-recipes,
      .promotion-summary-meta,
      .promotion-lane-meta {
        grid-template-columns: 1fr;
      }
      .promotion-detail-mast h2 { font-size: 32px; }
      .promotion-detail-check-head,
      .action-card-head { flex-direction: column; }
    }
    """

    return _render_harbor_shell_document(
        page_title=f"Harbor promotion detail · {path_label}",
        context_name=context_name,
        active_nav="detail",
        body_class="detail-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def _render_harbor_environment_status_page_html(
    payload: dict[str, object],
    *,
    action_payload: dict[str, object] | None = None,
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", "")).strip()
    instance_name = str(payload.get("instance", "")).strip() or "environment"
    live_payload = payload.get("live") if isinstance(payload.get("live"), dict) else {}
    live_promotion = payload.get("live_promotion") if isinstance(payload.get("live_promotion"), dict) else None
    authorized_backup_gate = (
        payload.get("authorized_backup_gate")
        if isinstance(payload.get("authorized_backup_gate"), dict)
        else None
    )
    latest_promotion = payload.get("latest_promotion") if isinstance(payload.get("latest_promotion"), dict) else None
    latest_deployment = payload.get("latest_deployment") if isinstance(payload.get("latest_deployment"), dict) else None
    recent_promotions_payload = payload.get("recent_promotions")
    recent_promotions = (
        list(recent_promotions_payload)
        if isinstance(recent_promotions_payload, (list, tuple))
        else []
    )
    recent_deployments_payload = payload.get("recent_deployments")
    recent_deployments = (
        list(recent_deployments_payload)
        if isinstance(recent_deployments_payload, (list, tuple))
        else []
    )

    lane_title = f"{context_name}/{instance_name}" if context_name else instance_name
    role_summary = (
        "Testing carries the integration artifact Harbor would promote next."
        if instance_name == "testing"
        else "Prod is the customer-facing lane Harbor protects and promotes into deliberately."
    )
    live_tone = _status_tone(str(live_payload.get("destination_health_status", "pending") or "pending"))
    deploy_status = str(live_payload.get("deploy_status", "pending") or "pending")
    health_status = str(live_payload.get("destination_health_status", "pending") or "pending")
    action_status = str(action_payload.get("status", "")) if isinstance(action_payload, dict) else ""

    live_promotion_html = ""
    if live_promotion is not None:
        live_promotion_html = f"""
        <article class=\"detail-note\">
          <div class=\"section-label\">Attached promotion</div>
          <h3>Current lane inventory is backed by a promotion record.</h3>
          <dl class=\"detail-meta\">
            <div><dt>Promotion record</dt><dd><code>{escape(str(live_promotion.get('record_id', '')) or 'Unavailable')}</code></dd></div>
            <div><dt>Artifact</dt><dd><code>{escape(str(live_promotion.get('artifact_id', '')) or 'Unavailable')}</code></dd></div>
            <div><dt>Backup gate</dt><dd><code>{escape(str(live_promotion.get('backup_record_id', '')) or 'Unavailable')}</code></dd></div>
            <div><dt>Finished</dt><dd>{escape(str(live_promotion.get('finished_at', '')) or 'Unavailable')}</dd></div>
          </dl>
        </article>
        """
    else:
        live_promotion_html = """
        <article class=\"detail-note detail-note-muted\">
          <div class=\"section-label\">Attached promotion</div>
          <h3>No live promotion record is attached to this lane inventory.</h3>
          <p>Harbor can still show recent promotion history below, but the current environment inventory does not point at one canonical promotion record yet.</p>
        </article>
        """

    backup_gate_html = ""
    if authorized_backup_gate is not None:
        evidence_entries = "".join(
            f"<li><code>{escape(str(key))}</code> {escape(str(value))}</li>"
            for key, value in sorted(
                (authorized_backup_gate.get("evidence") if isinstance(authorized_backup_gate.get("evidence"), dict) else {}).items()
            )
        )
        backup_gate_html = f"""
        <article class=\"detail-note\">
          <div class=\"section-label\">Authorized backup gate</div>
          <h3>Harbor has a recorded backup gate for this lane.</h3>
          <dl class=\"detail-meta\">
            <div><dt>Record</dt><dd><code>{escape(str(authorized_backup_gate.get('record_id', '')) or 'Unavailable')}</code></dd></div>
            <div><dt>Status</dt><dd>{escape(str(authorized_backup_gate.get('status', 'unknown')) or 'unknown')}</dd></div>
            <div><dt>Source</dt><dd>{escape(str(authorized_backup_gate.get('source', '')) or 'Unavailable')}</dd></div>
            <div><dt>Created</dt><dd>{escape(str(authorized_backup_gate.get('created_at', '')) or 'Unavailable')}</dd></div>
          </dl>
          <ul class=\"detail-list\">{evidence_entries or '<li>No backup evidence fields recorded.</li>'}</ul>
        </article>
        """
    else:
        backup_gate_html = """
        <article class=\"detail-note detail-note-muted\">
          <div class=\"section-label\">Authorized backup gate</div>
          <h3>No authorized backup gate is attached to this lane yet.</h3>
          <p>This is normal for `testing` and is still a useful warning for `prod` when Harbor cannot prove the current lane from attached backup evidence alone.</p>
        </article>
        """

    action_html = """
    <article class=\"detail-note detail-note-muted\">
      <div class=\"section-label\">Lane action</div>
      <h3>No typed lane action is available.</h3>
      <p>Harbor does not have enough environment evidence to build a re-ship recipe for this lane yet.</p>
    </article>
    """
    if isinstance(action_payload, dict):
        if action_status == "actionable":
            action_html = _render_harbor_action_recipe(
                title=str(action_payload.get("headline", f"Re-ship current {instance_name} artifact")),
                summary=str(action_payload.get("summary", "")),
                tone=str(action_payload.get("tone", "neutral")),
                script=str(action_payload.get("recipe", "")),
                command_label="ship resolve -> ship execute",
                recipe_id=f"environment-detail-{_harbor_action_slug(lane_title)}-ship",
            )
        else:
            action_html = f"""
            <article class=\"detail-note detail-note-muted\">
              <div class=\"section-label\">Lane action</div>
              <h3>{escape(str(action_payload.get('headline', 'No typed lane action is available.')))}</h3>
              <p>{escape(str(action_payload.get('summary', 'Harbor does not have enough environment evidence to build a re-ship recipe for this lane yet.')))}</p>
            </article>
            """

    def render_activity_table(rows: list[dict[str, object]], *, table_kind: str) -> str:
        if table_kind == "deployments":
            if not rows:
                return "<p class=\"table-empty\">No deployment history recorded for this lane yet.</p>"
            table_rows = "".join(
                "<tr>"
                f"<td><code>{escape(str(row.get('record_id', '')) or 'Unavailable')}</code></td>"
                f"<td><code>{escape(str(row.get('artifact_id', '')) or 'Unavailable')}</code></td>"
                f"<td><code>{escape(str(row.get('source_git_ref', '')) or 'Unavailable')}</code></td>"
                f"<td>{escape(str(row.get('deploy_status', 'unknown')) or 'unknown')}</td>"
                f"<td>{escape(str(row.get('destination_health_status', 'unknown')) or 'unknown')}</td>"
                f"<td>{escape(str(row.get('finished_at', '')) or 'Unavailable')}</td>"
                "</tr>"
                for row in rows
                if isinstance(row, dict)
            )
            return (
                "<table><thead><tr><th>Deployment record</th><th>Artifact</th><th>Source ref</th><th>Deploy</th><th>Health</th><th>Finished</th></tr></thead>"
                f"<tbody>{table_rows}</tbody></table>"
            )
        if not rows:
            return "<p class=\"table-empty\">No promotion history recorded into this lane yet.</p>"
        table_rows = "".join(
            "<tr>"
            f"<td><code>{escape(str(row.get('record_id', '')) or 'Unavailable')}</code></td>"
            f"<td>{escape(str(row.get('from_instance', '')) or 'Unavailable')}</td>"
            f"<td><code>{escape(str(row.get('artifact_id', '')) or 'Unavailable')}</code></td>"
            f"<td>{escape(str(row.get('backup_status', 'unknown')) or 'unknown')}</td>"
            f"<td>{escape(str(row.get('destination_health_status', 'unknown')) or 'unknown')}</td>"
            f"<td>{escape(str(row.get('finished_at', '')) or 'Unavailable')}</td>"
            "</tr>"
            for row in rows
            if isinstance(row, dict)
        )
        return (
            "<table><thead><tr><th>Promotion record</th><th>From lane</th><th>Artifact</th><th>Backup</th><th>Health</th><th>Finished</th></tr></thead>"
            f"<tbody>{table_rows}</tbody></table>"
        )

    latest_deployment_summary = "No deployment record is attached to this lane yet."
    if latest_deployment is not None:
        latest_deployment_summary = (
            f"Latest deployment finished {escape(str(latest_deployment.get('finished_at', '')) or 'recently')} "
            f"with deploy {escape(str(latest_deployment.get('deploy_status', 'unknown')) or 'unknown')} and health "
            f"{escape(str(latest_deployment.get('destination_health_status', 'unknown')) or 'unknown')}."
        )
    latest_promotion_summary = "Harbor has not recorded a recent promotion into this lane yet."
    if latest_promotion is not None:
        latest_promotion_summary = (
            f"Latest promotion moved <code>{escape(str(latest_promotion.get('artifact_id', '')) or 'Unavailable')}</code> "
            f"from {escape(str(latest_promotion.get('from_instance', '')) or 'another lane')} into {escape(instance_name)}."
        )

    body_html = f"""
    <section class=\"environment-detail-mast\">
      <div>
        <div class=\"section-label\">Environment detail</div>
        <h2>{escape(lane_title)}</h2>
        <p>{role_summary}</p>
      </div>
      <aside class=\"environment-detail-brief\">
        <span class=\"tone-pill tone-{live_tone}\">Deploy {_status_label(deploy_status)}</span>
        <span class=\"tone-pill tone-{_status_tone(health_status)}\">Health {_status_label(health_status)}</span>
        <p>{latest_deployment_summary}</p>
      </aside>
    </section>

    <section class=\"environment-detail-grid\">
      <article class=\"detail-card detail-card-primary\">
        <div class=\"section-label\">Live lane snapshot</div>
        <h3>Current environment evidence</h3>
        <dl class=\"detail-meta\">
          <div><dt>Artifact</dt><dd><code>{escape(str(live_payload.get('artifact_id', '')) or 'Unavailable')}</code></dd></div>
          <div><dt>Source ref</dt><dd><code>{escape(str(live_payload.get('source_git_ref', '')) or 'Unavailable')}</code></dd></div>
          <div><dt>Updated</dt><dd>{escape(str(live_payload.get('updated_at', '')) or 'Unavailable')}</dd></div>
          <div><dt>Deploy record</dt><dd><code>{escape(str(live_payload.get('deployment_record_id', '')) or 'Unavailable')}</code></dd></div>
          <div><dt>Deploy status</dt><dd>{escape(_status_label(deploy_status))}</dd></div>
          <div><dt>Health status</dt><dd>{escape(_status_label(health_status))}</dd></div>
          <div><dt>Promoted from</dt><dd>{escape(str(live_payload.get('promoted_from_instance', '')) or 'Unavailable')}</dd></div>
          <div><dt>Promotion record</dt><dd><code>{escape(str(live_payload.get('promotion_record_id', '')) or 'Unavailable')}</code></dd></div>
        </dl>
      </article>
      <article class=\"detail-card\">
        <div class=\"section-label\">Recent changes</div>
        <h3>What Harbor saw last</h3>
        <p>{latest_promotion_summary}</p>
        <p class=\"detail-secondary\">{latest_deployment_summary}</p>
      </article>
    </section>

    <section class=\"environment-detail-ops\">
      {action_html}
      {live_promotion_html}
      {backup_gate_html}
    </section>

    <section class=\"environment-history\">
      <div class=\"section-label\">Deployment history</div>
      <h3>Recent deployments</h3>
      {render_activity_table([row for row in recent_deployments if isinstance(row, dict)], table_kind='deployments')}
    </section>

    <section class=\"environment-history\">
      <div class=\"section-label\">Promotion history</div>
      <h3>Recent promotions into this lane</h3>
      {render_activity_table([row for row in recent_promotions if isinstance(row, dict)], table_kind='promotions')}
    </section>
    """

    extra_css = """
    .section-label {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .environment-detail-mast {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 18px;
      align-items: start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 20px;
    }
    .environment-detail-mast h2,
    .detail-card h3,
    .detail-note h3,
    .environment-history h3 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.04;
    }
    .environment-detail-mast h2 {
      font-size: 40px;
    }
    .environment-detail-mast p,
    .environment-detail-brief p,
    .detail-card p,
    .detail-note p,
    .table-empty {
      margin: 10px 0 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .environment-detail-brief {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 16px;
      padding: 14px 16px 16px;
      display: grid;
      gap: 10px;
      align-content: start;
    }
    .environment-detail-grid,
    .environment-detail-ops {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 18px;
    }
    .environment-detail-ops {
      grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
      align-items: start;
    }
    .detail-card,
    .detail-note,
    .action-card,
    .environment-history {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 16px;
      padding: 16px 18px 18px;
    }
    .detail-card,
    .detail-note,
    .environment-history {
      display: grid;
      gap: 12px;
    }
    .detail-note-muted {
      background: rgba(251, 250, 246, 0.72);
    }
    .detail-secondary {
      color: var(--text);
    }
    .detail-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
      margin: 0;
    }
    .detail-meta > div {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .detail-meta dt {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .detail-meta dd {
      margin: 7px 0 0;
      overflow-wrap: anywhere;
    }
    .detail-meta code,
    table code,
    .action-pre {
      font-family: var(--mono);
      font-size: 12px;
    }
    .detail-list {
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      display: grid;
      gap: 8px;
      line-height: 1.55;
    }
    .environment-history {
      margin-top: 18px;
    }
    .action-card {
      display: grid;
      gap: 12px;
    }
    .action-card.tone-good,
    .action-card.tone-warn,
    .action-card.tone-bad,
    .action-card.tone-neutral {
      background: var(--surface);
      color: inherit;
    }
    .action-card.tone-good { border-left: 3px solid var(--good); }
    .action-card.tone-warn { border-left: 3px solid var(--warn); }
    .action-card.tone-bad { border-left: 3px solid var(--bad); }
    .action-card.tone-neutral { border-left: 3px solid var(--neutral); }
    .action-card-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .action-command {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .action-card h3 {
      font-size: 24px;
    }
    .copy-button {
      -webkit-appearance: none;
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f2ede3;
      padding: 7px 10px;
      color: var(--text);
      cursor: pointer;
      font: inherit;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .copy-button:hover {
      background: #e7dfd0;
    }
    .action-details {
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .action-details summary {
      cursor: pointer;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      list-style: none;
    }
    .action-details summary::-webkit-details-marker {
      display: none;
    }
    .action-pre {
      margin: 12px 0 0;
      overflow: auto;
      padding: 16px;
      background: #13110f;
      color: #e7e0d4;
      border-radius: 8px;
      line-height: 1.55;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      background: transparent;
    }
    th, td {
      text-align: left;
      padding: 11px 0;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    @media (max-width: 900px) {
      .environment-detail-mast,
      .environment-detail-grid,
      .environment-detail-ops,
      .detail-meta {
        grid-template-columns: 1fr;
      }
      .environment-detail-mast h2 {
        font-size: 32px;
      }
      .action-card-head {
        flex-direction: column;
      }
    }
    """

    return _render_harbor_shell_document(
        page_title=f"Harbor environment detail · {lane_title}",
        context_name=context_name,
        active_nav="detail",
        body_class="detail-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def _render_harbor_preview_status_page_html(
    payload: dict[str, object],
    *,
    nav_links: dict[str, str] | None = None,
) -> str:
    preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
    trust_summary = payload.get("trust_summary") if isinstance(payload.get("trust_summary"), dict) else {}
    health_summary = payload.get("health_summary") if isinstance(payload.get("health_summary"), dict) else {}
    input_summary = payload.get("input_summary") if isinstance(payload.get("input_summary"), dict) else {}
    lifecycle_summary = (
        payload.get("lifecycle_summary") if isinstance(payload.get("lifecycle_summary"), dict) else {}
    )
    links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
    recent_generations = (
        payload.get("recent_generations") if isinstance(payload.get("recent_generations"), list) else []
    )
    source_map = input_summary.get("source_map") if isinstance(input_summary.get("source_map"), list) else []
    companions = input_summary.get("companions") if isinstance(input_summary.get("companions"), list) else []
    serving_generation = (
        payload.get("serving_generation") if isinstance(payload.get("serving_generation"), dict) else {}
    )
    latest_generation = (
        payload.get("latest_generation") if isinstance(payload.get("latest_generation"), dict) else {}
    )

    preview_label = escape(str(preview.get("preview_label", "Harbor preview")))
    context_name = escape(str(preview.get("context", "")))
    anchor_repo_name = escape(str(preview.get("anchor_repo", "")))
    anchor_pr_number = escape(str(preview.get("anchor_pr_number", "")))
    canonical_url = escape(str(links.get("canonical_url", preview.get("canonical_url", ""))))
    anchor_pr_url = escape(str(links.get("anchor_pr_url", "")))
    preview_state = str(preview.get("state", "unknown"))
    status_summary = escape(str(health_summary.get("status_summary", "No Harbor preview summary available.")))
    next_action = escape(str(lifecycle_summary.get("next_action", "")))
    artifact_id = escape(str(trust_summary.get("artifact_id", "")))
    manifest_fingerprint = escape(str(trust_summary.get("manifest_fingerprint", "")))
    destroy_after = escape(str(lifecycle_summary.get("destroy_after", "")))
    active_generation_id = escape(
        str(trust_summary.get("active_generation_id", preview.get("active_generation_id", "")))
    )
    paused_at = escape(str(preview.get("paused_at", "")))
    destroyed_at = escape(str(lifecycle_summary.get("destroyed_at", preview.get("destroyed_at", ""))))
    destroy_reason = escape(str(lifecycle_summary.get("destroy_reason", preview.get("destroy_reason", ""))))
    overall_health_status = str(health_summary.get("overall_health_status", "pending"))
    raw_payload_json = escape(json.dumps(payload, indent=2, sort_keys=True))
    serving_matches_latest = bool(health_summary.get("serving_matches_latest", False))
    latest_failure_summary = escape(str(latest_generation.get("failure_summary", "")))
    latest_failure_stage = escape(str(latest_generation.get("failure_stage", "")))
    latest_generation_id = escape(str(latest_generation.get("generation_id", "")))
    latest_generation_state = str(latest_generation.get("state", ""))
    latest_requested_at = escape(str(latest_generation.get("requested_at", "")))
    serving_generation_id = escape(str(serving_generation.get("generation_id", "")))
    no_serving_preview = bool(latest_generation) and not serving_generation
    display_health_status = "unavailable" if no_serving_preview else overall_health_status
    summary_text = next_action or status_summary or "No next action recorded."
    healthy_live_preview = (
        preview_state.strip().lower() == "active"
        and serving_matches_latest
        and bool(serving_generation)
        and latest_generation_state.strip().lower() == "ready"
        and overall_health_status.strip().lower() == "pass"
    )
    generation_label = "Serving generation"
    generation_value = serving_generation_id or "Unavailable"
    primary_cta_label = "Open preview URL"
    primary_cta_href = canonical_url
    secondary_cta_label = "Anchor pull request"
    secondary_cta_href = anchor_pr_url
    if not latest_generation:
        generation_label = "Latest generation"
        generation_value = "Not created yet"
        primary_cta_label = "Open anchor pull request"
        primary_cta_href = anchor_pr_url
        secondary_cta_label = "Preview route (not live yet)"
        secondary_cta_href = canonical_url
    elif no_serving_preview:
        generation_label = "Latest generation"
        generation_value = latest_generation_id or "Unavailable"
        primary_cta_label = "Open anchor pull request"
        primary_cta_href = anchor_pr_url
        secondary_cta_label = "Preview route (not serving yet)"
        secondary_cta_href = canonical_url
    if preview_state.strip().lower() == "destroyed":
        generation_label = "Retained generation"
        generation_value = latest_generation_id or "Unavailable"
        primary_cta_label = "Open anchor pull request"
        primary_cta_href = anchor_pr_url
        secondary_cta_label = "Retained preview URL"
        secondary_cta_href = canonical_url
    replacement_failed = (
        preview_state.strip().lower() != "destroyed"
        and not serving_matches_latest
        and latest_generation
        and latest_generation_state.strip().lower() == "failed"
    )

    banner_label = f"{_status_label(preview_state).upper()}"
    banner_note = f"Health {_status_label(display_health_status)}"
    banner_tone = _status_tone(display_health_status)
    if preview_state.strip().lower() == "destroyed":
        banner_label = "DESTROYED"
        banner_note = "Preview evidence retained"
        banner_tone = "neutral"
    elif preview_state.strip().lower() == "paused":
        banner_label = "PAUSED"
        banner_note = "Preview intentionally held"
        banner_tone = "warn"
    elif preview_state.strip().lower() == "teardown_pending":
        banner_label = "TEARDOWN PENDING"
        banner_note = "Preview teardown pending"
        banner_tone = "warn"
    elif not latest_generation:
        banner_label = "STARTUP PENDING"
        banner_note = "Preview record created; no generation requested"
        banner_tone = "neutral"
    elif _generation_in_progress(latest_generation_state):
        banner_label = "REPLACEMENT IN FLIGHT" if serving_generation_id else "FIRST GENERATION IN FLIGHT"
        banner_note = "Current preview still serving" if serving_generation_id else "Harbor is preparing the first preview"
        banner_tone = "warn"
    elif no_serving_preview:
        banner_label = "AVAILABILITY GAP"
        banner_note = "Health unavailable"
        banner_tone = "bad"
    elif replacement_failed:
        banner_label = "FAILED REPLACEMENT"
        banner_note = "Older preview still serving"
        banner_tone = "bad"
    elif healthy_live_preview:
        banner_label = "LIVE PASS"
        banner_note = "Serving the latest requested generation."
        banner_tone = "good"

    callout_tone = banner_tone
    callout_eyebrow = "Current condition"
    callout_title = status_summary
    callout_summary = summary_text
    callout_items: list[tuple[str, str]] = []
    callout_detail = ""

    if preview_state.strip().lower() == "destroyed":
        callout_eyebrow = "Historical evidence"
        callout_title = "This preview has already been destroyed. Harbor is retaining the record as evidence."
        callout_summary = status_summary
        callout_items = [
            ("Destroyed at", destroyed_at or "Unavailable"),
            ("Destroy reason", destroy_reason or "Unavailable"),
            ("Retained generation", f"<code>{latest_generation_id or 'Unavailable'}</code>"),
        ]
        callout_tone = "neutral"
    elif preview_state.strip().lower() == "paused":
        callout_eyebrow = "Paused state"
        callout_title = "This preview is intentionally paused. Harbor is holding the current review evidence in place."
        callout_summary = status_summary
        callout_items = [
            ("Paused at", paused_at or "Unavailable"),
            (
                "Serving now",
                f"<code>{serving_generation_id or latest_generation_id or 'Unavailable'}</code>",
            ),
            ("Resume behavior", "Blocked until Harbor resumes the preview."),
        ]
        callout_tone = "warn"
    elif preview_state.strip().lower() == "teardown_pending":
        callout_eyebrow = "Scheduled cleanup"
        callout_title = "This preview is queued for teardown. Harbor is keeping the current runtime available until cleanup completes."
        callout_summary = summary_text
        callout_items = [
            ("Destroy after", destroy_after or "Unavailable"),
            (
                "Serving now",
                f"<code>{serving_generation_id or latest_generation_id or 'Unavailable'}</code>",
            ),
            ("Evidence retained", "Anchor PR and generation history remain after runtime cleanup."),
        ]
        callout_tone = "warn"
    elif not latest_generation:
        callout_eyebrow = "Startup pending"
        callout_title = "Harbor has created this preview record, but the first generation has not been requested yet."
        callout_summary = summary_text
        callout_items = [
            ("Preview route", canonical_url or "Unavailable"),
            ("Generation status", "Not created yet"),
            (
                "What happens next",
                "Harbor needs the first generation request before this preview becomes live.",
            ),
        ]
        callout_tone = "neutral"
    elif _generation_in_progress(latest_generation_state):
        callout_eyebrow = "Replacement in flight"
        callout_title = (
            "A replacement generation is in progress. Harbor is still serving the current preview."
            if serving_generation_id
            else "The first preview generation is in progress. Harbor is preparing this preview now."
        )
        callout_summary = summary_text or "Harbor is advancing the latest generation toward a reviewable preview."
        callout_items = [
            ("Current stage", escape(_status_label(latest_generation_state)) or "Unavailable"),
            (
                "Serving now",
                f"<code>{serving_generation_id or 'No serving preview yet'}</code>",
            ),
            ("Requested at", latest_requested_at or "Unavailable"),
        ]
        callout_tone = "warn"
    elif no_serving_preview:
        callout_eyebrow = "Availability gap"
        callout_title = "Harbor has generation evidence for this preview, but nothing is serving yet."
        callout_summary = summary_text
        callout_items = [
            ("Latest generation", f"<code>{latest_generation_id or 'Unavailable'}</code>"),
            ("Current state", escape(_status_label(latest_generation_state)) or "Unavailable"),
            ("Requested at", latest_requested_at or "Unavailable"),
        ]
        callout_tone = "bad"
    elif replacement_failed:
        callout_eyebrow = "Replacement status"
        callout_title = "Latest replacement failed. Harbor is still serving the older preview."
        callout_summary = status_summary
        callout_items = [
            ("Serving now", f"<code>{serving_generation_id or 'Unavailable'}</code>"),
            ("Failed replacement", f"<code>{latest_generation_id or 'Unavailable'}</code>"),
            ("Failure stage", latest_failure_stage or "Unavailable"),
        ]
        callout_detail = (
            latest_failure_summary
            or "Harbor recorded a failed replacement without an additional summary."
        )
        callout_tone = "bad"
    elif healthy_live_preview:
        callout_eyebrow = "Review is live"
        callout_title = "This preview is live at the stable Harbor route and serving the latest requested generation."
        callout_summary = summary_text
        callout_items = [
            ("Serving generation", f"<code>{serving_generation_id or 'Unavailable'}</code>"),
            ("Artifact", f"<code>{artifact_id or 'Unavailable'}</code>"),
            ("Destroy after", destroy_after or "Unavailable"),
        ]
        callout_tone = "good"

    callout_rows = "".join(
        f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in callout_items
    )
    callout_detail_html = f"<p class=\"callout-detail\">{callout_detail}</p>" if callout_detail else ""
    callout_html = f"""
    <article class=\"preview-condition-card detail-card tone-{callout_tone}\">
      <div class=\"section-label\">{callout_eyebrow}</div>
      <h2>{callout_title}</h2>
      <p>{callout_summary}</p>
      <dl>{callout_rows}</dl>
      {callout_detail_html}
    </article>
    """

    source_map_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('repo', '')))}</td>"
            f"<td><code>{escape(str(item.get('git_sha', '')))}</code></td>"
            f"<td>{escape(str(item.get('selection', '')))}</td>"
            "</tr>"
        )
        for item in source_map
        if isinstance(item, dict)
    )
    companion_items = "".join(
        f"<li><span>{escape(str(item.get('repo', '')))}</span><span><code>PR {escape(str(item.get('pr_number', '')))}</code></span></li>"
        for item in companions
        if isinstance(item, dict)
    )
    companions_section_html = ""
    if companion_items:
        companions_section_html = f"""
    <section class=\"preview-detail-section\">
      <div class=\"section-label\">Linked pull requests</div>
      <h2>Companion refs</h2>
      <p>Companion intent stays explicit and secondary to the anchor preview narrative.</p>
      <ul class=\"simple-list\">{companion_items}</ul>
    </section>
    """

    generation_rows = []
    for item in recent_generations:
        if not isinstance(item, dict):
            continue
        generation_id_value = escape(str(item.get("generation_id", "")))
        generation_state_value = str(item.get("state", ""))
        state_label = escape(_status_label(generation_state_value)) or "Unavailable"
        requested_at_value = escape(str(item.get("requested_at", ""))) or "Unavailable"
        role_parts: list[str] = []
        if generation_id_value and generation_id_value == serving_generation_id:
            role_parts.append("serving")
        if generation_id_value and generation_id_value == latest_generation_id:
            role_parts.append("latest")
        if generation_id_value and generation_id_value == active_generation_id:
            role_parts.append("active")
        role_label = escape(" / ".join(role_parts) if role_parts else "historical")
        serving_marker = "&bull; " if generation_id_value and generation_id_value == serving_generation_id else ""
        state_class = f"state-{_status_tone(generation_state_value)}"
        generation_rows.append(
            "<tr>"
            f"<td><code title=\"{generation_id_value}\">{serving_marker}{generation_id_value or 'Unavailable'}</code></td>"
            f"<td>{role_label}</td>"
            f"<td class=\"{state_class}\">{state_label}</td>"
            f"<td>{requested_at_value}</td>"
            "</tr>"
        )
        failure_stage_value = escape(str(item.get("failure_stage", "")))
        if generation_state_value.strip().lower() == "failed" and failure_stage_value:
            generation_rows.append(
                "<tr class=\"row-note\">"
                "<td></td>"
                f"<td colspan=\"3\">Failure stage {failure_stage_value}.</td>"
                "</tr>"
            )
    recent_generation_rows = "".join(generation_rows)

    metadata_items = [
        ("Artifact", f"<code>{artifact_id or 'Unavailable'}</code>"),
        ("Manifest", f"<code>{manifest_fingerprint or 'Unavailable'}</code>"),
        (generation_label, f"<code>{generation_value}</code>"),
        ("Destroy after", destroy_after or "Unavailable"),
    ]
    metadata_rows = "".join(
        f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in metadata_items
    )

    route_line_items = []
    if canonical_url:
        route_line_items.append(
            f"<div class=\"route-item\"><span>Stable route</span><a href=\"{canonical_url}\">{canonical_url}</a></div>"
        )
    if anchor_pr_url:
        route_line_items.append(
            f"<div class=\"route-item\"><span>Anchor PR</span><a href=\"{anchor_pr_url}\">{anchor_pr_url}</a></div>"
        )
    route_line = "".join(route_line_items) or "<div class=\"route-item\">No preview route recorded.</div>"
    mast_title = preview_label
    if anchor_repo_name and anchor_pr_number:
        mast_title = f"{anchor_repo_name} PR {anchor_pr_number}"
    identity_bits = []
    if context_name:
        identity_bits.append(f"Context {context_name}")
    if preview_label and mast_title != preview_label:
        identity_bits.append(f"<code>{preview_label}</code>")
    identity_html = ""
    if identity_bits:
        identity_html = f'<p class="identity-line">{"<span>&bull;</span>".join(identity_bits)}</p>'

    action_slug = _harbor_action_slug(preview_label)
    raw_anchor_head_sha = str(input_summary.get("anchor", {}).get("head_sha", "")).strip() if isinstance(input_summary.get("anchor"), dict) else ""
    raw_baseline_release_tuple_id = str(input_summary.get("baseline_release_tuple_id", "")).strip()
    raw_source_map = [item for item in source_map if isinstance(item, dict)]
    raw_companions = [item for item in companions if isinstance(item, dict)]
    raw_context_name = str(preview.get("context", "")).strip()
    raw_anchor_repo = str(preview.get("anchor_repo", "")).strip()
    raw_anchor_pr_url = str(preview.get("anchor_pr_url", links.get("anchor_pr_url", ""))).strip()
    raw_latest_generation_id = str(latest_generation.get("generation_id", "")).strip()
    raw_latest_requested_reason = str(latest_generation.get("requested_reason", "")).strip()
    raw_latest_requested_at = str(latest_generation.get("requested_at", "")).strip()
    raw_latest_artifact_id = str(latest_generation.get("artifact_id", "")).strip()
    raw_latest_manifest_fingerprint = str(latest_generation.get("resolved_manifest_fingerprint", "")).strip()
    operator_actions: list[str] = []
    if preview_state.strip().lower() != "destroyed":
        destroy_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": int(preview.get("anchor_pr_number", 0) or 0),
            "destroyed_at": "<utc-timestamp>",
            "destroy_reason": "operator_requested",
        }
        destroy_script = _build_harbor_action_script(
            command_name="destroy-preview",
            file_payloads=(("ACTION_FILE", f"/tmp/harbor-{action_slug}-destroy-preview.json", destroy_payload),),
            command_args=("--input-file", '"$ACTION_FILE"'),
        )
        operator_actions.append(
            _render_harbor_action_recipe(
                title="Destroy preview",
                summary="Tear down this preview explicitly while retaining Harbor evidence for the record.",
                tone="bad",
                script=destroy_script,
                command_label="destroy-preview",
                recipe_id=f"action-{action_slug}-destroy",
            )
        )
        request_generation_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": int(preview.get("anchor_pr_number", 0) or 0),
            "anchor_pr_url": raw_anchor_pr_url,
            "state": preview_state.strip().lower() or "pending",
            "updated_at": "<utc-timestamp>",
        }
        generation_request_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": int(preview.get("anchor_pr_number", 0) or 0),
            "anchor_pr_url": raw_anchor_pr_url,
            "anchor_head_sha": raw_anchor_head_sha or "<anchor-head-sha>",
            "state": "resolving",
            "requested_reason": "operator_requested_refresh",
            "requested_at": "<utc-timestamp>",
            "resolved_manifest_fingerprint": raw_latest_manifest_fingerprint or "<manifest-fingerprint>",
            "artifact_id": raw_latest_artifact_id,
            "baseline_release_tuple_id": raw_baseline_release_tuple_id,
            "source_map": raw_source_map,
            "companion_summaries": raw_companions,
            "deploy_status": "pending",
            "verify_status": "pending",
            "overall_health_status": "pending",
        }
        request_script = _build_harbor_action_script(
            command_name="request-generation",
            file_payloads=(
                ("PREVIEW_FILE", f"/tmp/harbor-{action_slug}-preview.json", request_generation_payload),
                ("GENERATION_FILE", f"/tmp/harbor-{action_slug}-generation.json", generation_request_payload),
            ),
            command_args=(
                "--preview-input-file",
                '"$PREVIEW_FILE"',
                "--generation-input-file",
                '"$GENERATION_FILE"',
            ),
        )
        operator_actions.insert(
            0,
            _render_harbor_action_recipe(
                title="Request replacement generation",
                summary="Queue a fresh Harbor generation for this preview using the current record as the starting template.",
                tone="warn",
                script=request_script,
                command_label="request-generation",
                recipe_id=f"action-{action_slug}-request-generation",
            ),
        )
    if raw_latest_generation_id and _generation_in_progress(latest_generation_state):
        ready_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": int(preview.get("anchor_pr_number", 0) or 0),
            "anchor_pr_url": raw_anchor_pr_url,
            "anchor_head_sha": raw_anchor_head_sha or "<anchor-head-sha>",
            "generation_id": raw_latest_generation_id,
            "state": "ready",
            "requested_reason": raw_latest_requested_reason or "operator_requested_refresh",
            "requested_at": raw_latest_requested_at or "<requested-at>",
            "ready_at": "<utc-timestamp>",
            "finished_at": "<utc-timestamp>",
            "resolved_manifest_fingerprint": raw_latest_manifest_fingerprint or "<manifest-fingerprint>",
            "artifact_id": raw_latest_artifact_id or "<artifact-id>",
            "baseline_release_tuple_id": raw_baseline_release_tuple_id,
            "source_map": raw_source_map,
            "companion_summaries": raw_companions,
            "deploy_status": "pass",
            "verify_status": "pass",
            "overall_health_status": "pass",
        }
        ready_script = _build_harbor_action_script(
            command_name="mark-generation-ready",
            file_payloads=(("ACTION_FILE", f"/tmp/harbor-{action_slug}-mark-ready.json", ready_payload),),
            command_args=("--input-file", '"$ACTION_FILE"'),
        )
        failed_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": int(preview.get("anchor_pr_number", 0) or 0),
            "anchor_pr_url": raw_anchor_pr_url,
            "anchor_head_sha": raw_anchor_head_sha or "<anchor-head-sha>",
            "generation_id": raw_latest_generation_id,
            "state": "failed",
            "requested_reason": raw_latest_requested_reason or "operator_requested_refresh",
            "requested_at": raw_latest_requested_at or "<requested-at>",
            "failed_at": "<utc-timestamp>",
            "finished_at": "<utc-timestamp>",
            "resolved_manifest_fingerprint": raw_latest_manifest_fingerprint or "<manifest-fingerprint>",
            "artifact_id": raw_latest_artifact_id,
            "baseline_release_tuple_id": raw_baseline_release_tuple_id,
            "source_map": raw_source_map,
            "companion_summaries": raw_companions,
            "deploy_status": "fail",
            "verify_status": "pending",
            "overall_health_status": "fail",
            "failure_stage": latest_failure_stage or "<failure-stage>",
            "failure_summary": latest_failure_summary or "<failure-summary>",
        }
        failed_script = _build_harbor_action_script(
            command_name="mark-generation-failed",
            file_payloads=(("ACTION_FILE", f"/tmp/harbor-{action_slug}-mark-failed.json", failed_payload),),
            command_args=("--input-file", '"$ACTION_FILE"'),
        )
        operator_actions.insert(
            0,
            _render_harbor_action_recipe(
                title="Mark latest generation failed",
                summary="Record a failed in-flight generation while preserving any still-serving preview evidence.",
                tone="bad",
                script=failed_script,
                command_label="mark-generation-failed",
                recipe_id=f"action-{action_slug}-mark-failed",
            ),
        )
        operator_actions.insert(
            0,
            _render_harbor_action_recipe(
                title="Mark latest generation ready",
                summary="Advance the current in-flight generation into Harbor's ready/serving path once deploy and verify evidence are complete.",
                tone="good",
                script=ready_script,
                command_label="mark-generation-ready",
                recipe_id=f"action-{action_slug}-mark-ready",
            ),
        )
    operator_actions_html = "".join(operator_actions)
    if not operator_actions_html:
        operator_actions_html = "<p class=\"action-empty\">No write-side recipe is exposed for this retained preview state.</p>"
    operator_actions_section_html = f"""
    <section class=\"preview-detail-section\" id=\"operator-actions\">
      <div class=\"section-label\">Operator actions</div>
      <h2>Write-side Harbor recipes</h2>
      <p>Harbor still renders as a static operator surface here, so each action is shown as the exact shell recipe for this preview identity.</p>
      <div class=\"action-stack\">{operator_actions_html}</div>
    </section>
    """

    body_html = f"""
    <section class=\"preview-detail-mast\">
      <div>
        <div class=\"section-label\">Preview detail</div>
        <h2>{mast_title}</h2>
        {identity_html}
      </div>
      <aside class=\"preview-detail-brief\">
        <div class=\"banner tone-{banner_tone}\"><strong>{escape(banner_label)}</strong><span>{escape(banner_note)}</span></div>
        <p>{summary_text}</p>
        <div class=\"actions\">
          <a class=\"primary-action\" href=\"{primary_cta_href}\">{primary_cta_label}</a>
          <a class=\"secondary-link\" href=\"{secondary_cta_href}\">{secondary_cta_label}</a>
        </div>
      </aside>
    </section>

    <section class=\"preview-detail-grid\">
      <article class=\"detail-card detail-card-primary\">
        <div class=\"section-label\">Current preview evidence</div>
        <h3>Stable route and generation state</h3>
        <dl class=\"detail-meta\">{metadata_rows}</dl>
        <div class=\"route-line\">{route_line}</div>
      </article>
      {callout_html}
    </section>

    {operator_actions_section_html}

    <section class=\"preview-detail-section\">
      <div class=\"section-label\">Exact inputs</div>
      <h2>Serving manifest evidence</h2>
      <p>Harbor keeps the exact repo-to-SHA map visible so reviewers can answer what code is running here without hidden branch assumptions.</p>
      <table>
        <thead><tr><th>Repo</th><th>SHA</th><th>Selection</th></tr></thead>
        <tbody>{source_map_rows or '<tr><td colspan="3">No source map recorded.</td></tr>'}</tbody>
      </table>
    </section>

    {companions_section_html}

    <section class=\"preview-detail-section\">
      <div class=\"section-label\">Recent activity</div>
      <h2>Generation ledger</h2>
      <p>Generation history stays visible as evidence, but the stable preview route remains the primary narrative.</p>
      <table>
        <thead><tr><th>Generation</th><th>Role</th><th>State</th><th>Requested at</th></tr></thead>
        <tbody>{recent_generation_rows or '<tr><td colspan="4">No recent generations recorded.</td></tr>'}</tbody>
      </table>
    </section>

    <section class=\"preview-detail-section\">
      <div class=\"section-label\">Lifecycle evidence</div>
      <h2>Control-plane record</h2>
      <details>
        <summary>Raw payload JSON</summary>
        <pre>{raw_payload_json}</pre>
      </details>
    </section>
    <script>
    (() => {{
      const buttons = Array.from(document.querySelectorAll('[data-copy-target]'));
      if (!buttons.length) {{
        return;
      }}
      const fallbackCopy = (text) => {{
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', 'true');
        textarea.style.position = 'absolute';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        const copied = document.execCommand('copy');
        document.body.removeChild(textarea);
        return copied;
      }};
      const selectRecipe = (target) => {{
        const details = target.closest('details');
        if (details) {{
          details.open = true;
        }}
        const selection = window.getSelection();
        if (!selection) {{
          return false;
        }}
        const range = document.createRange();
        range.selectNodeContents(target);
        selection.removeAllRanges();
        selection.addRange(range);
        target.scrollIntoView({{block: 'nearest'}});
        return true;
      }};
      buttons.forEach((button) => {{
        button.addEventListener('click', async () => {{
          const targetId = button.getAttribute('data-copy-target');
          if (!targetId) {{
            return;
          }}
          const target = document.getElementById(targetId);
          if (!target) {{
            return;
          }}
          try {{
            const text = target.textContent || '';
            if (navigator.clipboard && window.isSecureContext) {{
              await navigator.clipboard.writeText(text);
            }} else if (!fallbackCopy(text)) {{
              throw new Error('fallback-copy-failed');
            }}
            const original = button.textContent || 'Copy';
            button.textContent = 'Copied';
            window.setTimeout(() => {{
              button.textContent = original;
            }}, 1200);
          }} catch (_error) {{
            const text = target.textContent || '';
            if (fallbackCopy(text)) {{
              const original = button.textContent || 'Copy';
              button.textContent = 'Copied';
              window.setTimeout(() => {{
                button.textContent = original;
              }}, 1200);
            }} else {{
              const original = button.textContent || 'Copy';
              if (selectRecipe(target)) {{
                button.textContent = 'Selected';
                window.setTimeout(() => {{
                  button.textContent = original;
                }}, 1400);
              }} else {{
                button.textContent = 'Copy failed';
              }}
            }}
          }}
        }});
      }});
    }})();
    </script>
    """

    extra_css = """
    .preview-detail-mast {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
      gap: 18px;
      align-items: start;
      border-bottom: 1px solid var(--line);
      padding-bottom: 20px;
    }
    .identity-line {
      margin: 10px 0 0;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .identity-line code {
      font-size: 12px;
    }
    .preview-detail-mast h2,
    .detail-card h3,
    .preview-detail-section h2,
    .preview-condition-card h2,
    .action-card h3 {
      margin: 0;
      font-family: var(--serif);
      line-height: 1.04;
    }
    .preview-detail-mast h2 {
      font-size: 40px;
    }
    .preview-detail-mast p,
    .preview-detail-brief p,
    .detail-card p,
    .preview-detail-section p,
    .action-card p,
    .action-empty {
      margin: 10px 0 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .preview-detail-brief,
    .detail-card,
    .preview-detail-section,
    .action-card {
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: 8px;
      padding: 16px 18px 18px;
    }
    .preview-detail-brief {
      display: grid;
      gap: 12px;
      align-content: start;
    }
    .preview-detail-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(0, 0.95fr);
      gap: 16px;
      margin-top: 18px;
      align-items: start;
    }
    .preview-detail-section { margin-top: 18px; }
    .route-line {
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }
    .route-item { display: flex; flex-wrap: wrap; gap: 8px; }
    .route-item span { font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
    .route-line a,
    .secondary-link,
    details summary { color: inherit; }
    .route-line a,
    .secondary-link { text-underline-offset: 0.18em; }
    .banner {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: 4px;
      color: #f5f2ec;
      font-family: var(--mono);
    }
    .banner strong { font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; }
    .banner span { font-size: 12px; opacity: 0.92; }
    .tone-good { background: var(--good); }
    .tone-warn { background: var(--warn); }
    .tone-bad { background: var(--bad); }
    .tone-neutral { background: var(--neutral); }
    .actions { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }
    .primary-action {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 12px 16px;
      border-radius: 4px;
      background: var(--text);
      color: #f6f3ec;
      text-decoration: none;
      font-family: var(--mono);
      font-size: 13px;
    }
    .secondary-link { font-family: var(--mono); font-size: 13px; }
    .action-stack {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      margin-top: 18px;
    }
    .action-card { display: grid; gap: 12px; }
    .action-card.tone-good,
    .action-card.tone-warn,
    .action-card.tone-bad,
    .action-card.tone-neutral {
      background: var(--surface);
      color: inherit;
    }
    .action-card.tone-good { border-left: 3px solid var(--good); }
    .action-card.tone-warn { border-left: 3px solid var(--warn); }
    .action-card.tone-bad { border-left: 3px solid var(--bad); }
    .action-card.tone-neutral { border-left: 3px solid var(--neutral); }
    .action-card-head {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .action-command {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .action-card h3 { font-size: 22px; }
    .action-script-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .copy-button {
      -webkit-appearance: none;
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f2ede3;
      padding: 7px 10px;
      color: var(--text);
      cursor: pointer;
      font: inherit;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .copy-button:hover {
      background: #e7dfd0;
    }
    .action-details {
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .action-details summary {
      cursor: pointer;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      list-style: none;
    }
    .action-details summary::-webkit-details-marker {
      display: none;
    }
    .action-pre {
      margin: 12px 0 0;
      overflow: auto;
      padding: 16px;
      background: #13110f;
      color: #e7e0d4;
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.55;
    }
    .section-label {
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 12px;
    }
    .preview-condition-card { border-left: 3px solid var(--neutral); }
    .preview-condition-card.tone-good,
    .preview-condition-card.tone-warn,
    .preview-condition-card.tone-bad,
    .preview-condition-card.tone-neutral {
      background: var(--surface);
      color: inherit;
    }
    .preview-condition-card.tone-good { border-left-color: var(--good); }
    .preview-condition-card.tone-warn { border-left-color: var(--warn); }
    .preview-condition-card.tone-bad { border-left-color: var(--bad); }
    .preview-condition-card.tone-neutral { border-left-color: var(--neutral); }
    .preview-condition-card h2,
    .preview-detail-section h2 { font-size: 26px; }
    .detail-card h3 { font-size: 24px; }
    .preview-condition-card dl,
    .detail-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin: 18px 0 0;
    }
    .detail-meta > div {
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .preview-condition-card dt,
    .detail-meta dt {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .preview-condition-card dd,
    .detail-meta dd { margin: 8px 0 0; overflow-wrap: anywhere; }
    .callout-detail { color: var(--text); }
    table { width: 100%; border-collapse: collapse; margin-top: 18px; font-size: 14px; background: transparent; }
    th, td { text-align: left; padding: 11px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { color: var(--muted); font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
    .simple-list { list-style: none; margin: 18px 0 0; padding: 0; display: grid; gap: 10px; }
    .simple-list li { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; padding-bottom: 10px; border-bottom: 1px solid var(--line); }
    code { font-family: var(--mono); font-size: 12px; }
    .state-good { color: var(--good); }
    .state-warn { color: var(--warn); }
    .state-bad { color: var(--bad); }
    .row-note td { color: var(--muted); font-size: 13px; padding-top: 0; }
    details { margin-top: 18px; }
    summary { cursor: pointer; color: var(--muted); font-family: var(--mono); }
    pre { overflow: auto; padding: 18px; background: #13110f; color: #e7e0d4; border-radius: 4px; font-size: 12px; }
    @media (max-width: 900px) {
      .preview-detail-mast,
      .preview-detail-grid,
      .action-stack,
      .preview-condition-card dl,
      .detail-meta {
        grid-template-columns: 1fr;
      }
      .preview-detail-mast h2 { font-size: 32px; }
      .identity-line {
        gap: 6px;
        font-size: 12px;
      }
      .route-line {
        gap: 6px;
        padding-top: 12px;
        font-size: 13px;
      }
      .action-card-head { flex-direction: column; }
    }
    """

    return _render_harbor_shell_document(
        page_title=f"{preview_label} · Harbor status",
        context_name=str(preview.get("context", "")),
        active_nav="detail",
        body_class="detail-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def _load_json_file(input_file: Path) -> dict[str, object]:
    return json.loads(input_file.read_text(encoding="utf-8"))


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
    deadline = time.monotonic() + timeout_seconds
    last_error: str = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=min(5, timeout_seconds)) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"http {response.status}"
        except HTTPError as error:
            last_error = f"http {error.code}"
        except URLError as error:
            last_error = str(error.reason)
        time.sleep(1)
    raise click.ClickException(f"Healthcheck failed for {url}: {last_error or 'timeout'}")


def _verify_ship_healthchecks(*, request: ShipRequest) -> None:
    if not request.wait or not request.verify_health:
        return
    if not request.destination_health.urls:
        source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
            _control_plane_root()
        )
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            f"Define domains or ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL in {source_file} or disable with --no-verify-health."
        )
    if request.destination_health.timeout_seconds is None:
        raise click.ClickException("Healthcheck verification requested without timeout_seconds.")
    healthcheck_errors: list[str] = []
    for healthcheck_url in request.destination_health.urls:
        try:
            _wait_for_ship_healthcheck(
                url=healthcheck_url, timeout_seconds=request.destination_health.timeout_seconds
            )
            return
        except click.ClickException as error:
            healthcheck_errors.append(str(error))
    if healthcheck_errors:
        raise click.ClickException(
            "Healthcheck verification failed for all resolved URLs:\n"
            + "\n".join(healthcheck_errors)
        )


def _resolve_dokploy_target(
    *,
    request: ShipRequest,
) -> tuple[ResolvedTargetEvidence, int]:
    control_plane_root = _control_plane_root()
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root
    )
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
            f"No Dokploy target definition found for {request.context}/{request.instance} in {source_file}."
        )
    if target_definition.target_type != request.target_type:
        raise click.ClickException(
            f"Ship request target_type does not match {source_file}. "
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
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def _load_control_plane_environment_values() -> dict[str, str]:
    return control_plane_dokploy.read_control_plane_environment_values(
        control_plane_root=_control_plane_root(),
    )


def _require_dokploy_target_definition(
    *,
    source_file: Path,
    source_of_truth: control_plane_dokploy.DokploySourceOfTruth,
    context_name: str,
    instance_name: str,
    operation_name: str,
) -> control_plane_dokploy.DokployTargetDefinition:
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_definition is None:
        raise click.ClickException(
            f"{operation_name} target {context_name}/{instance_name} is missing from {source_file}."
        )
    return target_definition


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
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("ship request requires artifact_id")

    control_plane_root = _control_plane_root()
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = _require_dokploy_target_definition(
        source_file=source_file,
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Ship",
    )

    environment_values = _load_control_plane_environment_values()
    resolved_source_git_ref = (
        source_git_ref.strip()
        or target_definition.source_git_ref.strip()
        or DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF
    )
    destination_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=target_definition,
    )
    destination_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=target_definition,
        environment_values=environment_values,
    )
    should_verify_health = verify_health and wait
    if should_verify_health and not destination_healthcheck_urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            f"Define domains or ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL in {source_file} or disable with --no-verify-health."
        )

    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        context_name,
        instance_name,
        environment_values,
    )
    deploy_mode = _resolve_deploy_mode(
        configured_ship_mode=configured_ship_mode,
        target_type=target_definition.target_type,
    )

    try:
        return ShipRequest(
            artifact_id=normalized_artifact_id,
            context=context_name,
            instance=instance_name,
            source_git_ref=resolved_source_git_ref,
            target_name=target_definition.target_name.strip() or f"{context_name}-{instance_name}",
            target_type=target_definition.target_type,
            deploy_mode=deploy_mode,
            wait=wait,
            timeout_seconds=timeout_override_seconds,
            verify_health=should_verify_health,
            health_timeout_seconds=destination_health_timeout_seconds,
            dry_run=dry_run,
            no_cache=no_cache,
            allow_dirty=allow_dirty,
            destination_health=HealthcheckEvidence(
                urls=destination_healthcheck_urls,
                timeout_seconds=destination_health_timeout_seconds,
                status="pending" if should_verify_health else "skipped",
            ),
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error


def _resolve_ship_request_for_promotion(
    *,
    request: PromotionRequest,
) -> ShipRequest:
    ship_request = _resolve_native_ship_request(
        context_name=request.context,
        instance_name=request.to_instance,
        artifact_id=request.artifact_id,
        source_git_ref=request.source_git_ref,
        wait=request.wait,
        timeout_override_seconds=request.timeout_seconds,
        verify_health=request.verify_health,
        health_timeout_override_seconds=request.health_timeout_seconds,
        dry_run=request.dry_run,
        no_cache=request.no_cache,
        allow_dirty=request.allow_dirty,
    )
    if request.target_type != ship_request.target_type:
        raise click.ClickException(
            "Promotion request target_type does not match control-plane Dokploy source-of-truth. "
            f"Request={request.target_type} configured={ship_request.target_type}."
        )
    if request.target_name != ship_request.target_name:
        raise click.ClickException(
            "Promotion request target_name does not match control-plane Dokploy source-of-truth. "
            f"Request={request.target_name} configured={ship_request.target_name}."
        )
    if request.deploy_mode != ship_request.deploy_mode:
        raise click.ClickException(
            "Promotion request deploy_mode does not match resolved Dokploy ship mode. "
            f"Request={request.deploy_mode} configured={ship_request.deploy_mode}."
        )
    return ship_request


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
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("promotion request requires artifact_id")
    normalized_backup_record_id = backup_record_id.strip()
    if not normalized_backup_record_id:
        raise click.ClickException("promotion request requires backup_record_id")

    control_plane_root = _control_plane_root()
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    source_target_definition = _require_dokploy_target_definition(
        source_file=source_file,
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=from_instance_name,
        operation_name="Promotion source",
    )
    destination_target_definition = _require_dokploy_target_definition(
        source_file=source_file,
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=to_instance_name,
        operation_name="Promotion destination",
    )

    environment_values = _load_control_plane_environment_values()
    resolved_source_git_ref = (
        source_git_ref.strip()
        or source_target_definition.source_git_ref.strip()
        or DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF
    )
    source_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=source_target_definition,
    )
    source_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=source_target_definition,
        environment_values=environment_values,
    )
    source_health_status: ReleaseStatus = "pending" if source_healthcheck_urls else "skipped"
    destination_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=destination_target_definition,
    )
    destination_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=destination_target_definition,
        environment_values=environment_values,
    )
    should_verify_destination_health = verify_health and wait
    if should_verify_destination_health and not destination_healthcheck_urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            f"Define domains or ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL in {source_file} or disable with --no-verify-health."
        )
    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        context_name,
        to_instance_name,
        environment_values,
    )
    deploy_mode = _resolve_deploy_mode(
        configured_ship_mode=configured_ship_mode,
        target_type=destination_target_definition.target_type,
    )

    try:
        return PromotionRequest(
            artifact_id=normalized_artifact_id,
            backup_record_id=normalized_backup_record_id,
            source_git_ref=resolved_source_git_ref,
            context=context_name,
            from_instance=from_instance_name,
            to_instance=to_instance_name,
            target_name=destination_target_definition.target_name.strip()
            or f"{context_name}-{to_instance_name}",
            target_type=destination_target_definition.target_type,
            deploy_mode=deploy_mode,
            wait=wait,
            timeout_seconds=timeout_override_seconds,
            verify_health=should_verify_destination_health,
            health_timeout_seconds=destination_health_timeout_seconds,
            dry_run=dry_run,
            no_cache=no_cache,
            allow_dirty=allow_dirty,
            source_health=HealthcheckEvidence(
                urls=source_healthcheck_urls,
                timeout_seconds=source_health_timeout_seconds,
                status=source_health_status,
            ),
            backup_gate=BackupGateEvidence(
                status="pass",
                evidence={"backup_record_id": normalized_backup_record_id},
            ),
            destination_health=HealthcheckEvidence(
                urls=destination_healthcheck_urls,
                timeout_seconds=destination_health_timeout_seconds,
                status="pending" if should_verify_destination_health else "skipped",
            ),
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error


def _execute_dokploy_deploy(
    *,
    request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
) -> None:
    control_plane_root = _control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    latest_before = None
    if request.wait:
        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        no_cache=request.no_cache,
    )
    if not request.wait:
        return
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
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
    control_plane_dokploy.run_compose_post_deploy_update(
        host=host,
        token=token,
        target_definition=target_definition,
        env_file=env_file,
    )


def _skipped_destination_health(
    request: ShipRequest, *, detail_status: str = "skipped"
) -> HealthcheckEvidence:
    return request.destination_health.model_copy(
        update={"verified": False, "status": detail_status}
    )


def _execute_ship(
    *,
    state_dir: Path,
    env_file: Path | None,
    request: ShipRequest,
    mint_release_tuple: bool = True,
) -> tuple[Path | None, DeploymentRecord | ShipRequest]:
    record_store = _store(state_dir)
    resolved_artifact_id = _require_artifact_id(requested_artifact_id=request.artifact_id)
    artifact_manifest = _read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    resolved_request = _resolve_artifact_native_execution_request(
        request=request,
        artifact_id=resolved_artifact_id,
        artifact_manifest=artifact_manifest,
    )
    if mint_release_tuple and control_plane_release_tuples.should_mint_release_tuple_for_channel(
        resolved_request.instance
    ):
        control_plane_release_tuples.repo_shas_from_artifact_manifest(
            context_name=resolved_request.context,
            artifact_manifest=artifact_manifest,
        )

    if resolved_request.dry_run:
        click.echo(json.dumps(resolved_request.model_dump(mode="json"), indent=2, sort_keys=True))
        return None, resolved_request

    record_id = generate_deployment_record_id(
        context_name=resolved_request.context,
        instance_name=resolved_request.instance,
    )
    started_at = utc_now_timestamp()
    pending_record = build_deployment_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pending",
        started_at=started_at,
        finished_at="",
    )
    record_path = record_store.write_deployment_record(pending_record)

    try:
        resolved_target, deploy_timeout_seconds = _resolve_dokploy_target(
            request=resolved_request,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
        )
        record_store.write_deployment_record(final_record)
        raise

    try:
        _sync_artifact_image_reference_for_target(
            artifact_manifest=artifact_manifest,
            resolved_target=resolved_target,
        )
        _execute_dokploy_deploy(
            request=resolved_request,
            resolved_target=resolved_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="fail",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target)
        record_store.write_deployment_record(final_record)
        raise

    try:
        if resolved_request.wait and resolved_target.target_type == "compose":
            _run_compose_post_deploy_update(
                env_file=env_file,
                request=resolved_request,
            )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="pass",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target,
                                               post_deploy_update=PostDeployUpdateEvidence(
                                                   attempted=True,
                                                   status="fail",
                                                   detail=(
                                                       "Odoo-specific post-deploy update failed through the native "
                                                       "control-plane Dokploy schedule workflow."
                                                   ),
                                               ), destination_health=_skipped_destination_health(resolved_request))
        record_store.write_deployment_record(final_record)
        raise

    post_deploy_update_evidence = PostDeployUpdateEvidence()
    if resolved_request.wait and resolved_target.target_type == "compose":
        post_deploy_update_evidence = PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            detail=(
                "Odoo-specific post-deploy update completed through the native "
                "control-plane Dokploy schedule workflow."
            ),
        )

    try:
        _verify_ship_healthchecks(request=resolved_request)
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="pass",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target,
                                               post_deploy_update=post_deploy_update_evidence)
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="pass",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target,
                                               post_deploy_update=post_deploy_update_evidence,
                                               destination_health=_skipped_destination_health(resolved_request,
                                                                                              detail_status="fail"))
        record_store.write_deployment_record(final_record)
        raise

    record_store.write_deployment_record(final_record)
    if final_record.wait_for_completion and final_record.deploy.status == "pass":
        _write_environment_inventory(record_store=record_store, deployment_record=final_record)
        if mint_release_tuple:
            _write_release_tuple_from_deployment(
                record_store=record_store,
                deployment_record=final_record,
                artifact_manifest=artifact_manifest,
            )
    return record_path, final_record


def _require_artifact_id(*, requested_artifact_id: str) -> str:
    normalized_artifact_id = requested_artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("Artifact-backed execution requires an explicit artifact_id.")
    return normalized_artifact_id


def _read_artifact_manifest(
    *,
    record_store: FilesystemRecordStore,
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
    record_store: FilesystemRecordStore,
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
    record_store: FilesystemRecordStore,
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
    target_payload: dict[str, object],
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
        blockers.append(
            "Target still has mutable addon refs: " + ", ".join(mutable_addon_entries)
        )

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
    target_payload: dict[str, object],
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
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root,
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = _require_dokploy_target_definition(
        source_file=source_file,
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
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root,
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = _require_dokploy_target_definition(
        source_file=source_file,
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
    live_source_fields = {
        "source_type": str(target_payload.get("sourceType") or "").strip(),
        "custom_git_url": str(target_payload.get("customGitUrl") or "").strip(),
        "custom_git_branch": str(target_payload.get("customGitBranch") or "").strip(),
        "compose_path": str(target_payload.get("composePath") or "").strip(),
        "watch_paths": list(target_payload.get("watchPaths")) if isinstance(target_payload.get("watchPaths"), list) else [],
        "enable_submodules": bool(target_payload.get("enableSubmodules")) if target_payload.get("enableSubmodules") is not None else None,
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
            refreshed_env_map = control_plane_dokploy.parse_dokploy_env_text(str(refreshed_payload.get("env") or ""))
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


def _sync_artifact_image_reference_for_target(
    *,
    artifact_manifest: ArtifactIdentityManifest | None,
    resolved_target: ResolvedTargetEvidence,
) -> None:
    control_plane_root = Path(__file__).resolve().parent.parent
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    _validate_artifact_target_runtime_contract(
        artifact_manifest=artifact_manifest,
        resolved_target=resolved_target,
        target_payload=target_payload,
        env_map=env_map,
    )
    desired_image_reference = ""
    if artifact_manifest is not None:
        desired_image_reference = _artifact_image_reference_from_manifest(artifact_manifest)

    current_image_reference = env_map.get(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, "")
    if current_image_reference == desired_image_reference:
        return

    if desired_image_reference:
        env_map[ARTIFACT_IMAGE_REFERENCE_ENV_KEY] = desired_image_reference
    else:
        env_map.pop(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, None)

    control_plane_dokploy.update_dokploy_target_env(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        target_payload=target_payload,
        env_text=control_plane_dokploy.serialize_dokploy_env_text(env_map),
    )


def _write_environment_inventory(
    *,
    record_store: FilesystemRecordStore,
    deployment_record: DeploymentRecord,
    promotion_record_id: str = "",
    promoted_from_instance: str = "",
) -> Path:
    inventory_record = build_environment_inventory(
        deployment_record=deployment_record,
        updated_at=utc_now_timestamp(),
        promotion_record_id=promotion_record_id,
        promoted_from_instance=promoted_from_instance,
    )
    return record_store.write_environment_inventory(inventory_record)


def _write_environment_inventory_from_promotion(
    *,
    record_store: FilesystemRecordStore,
    promotion_record: PromotionRecord,
) -> Path:
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
    record_store: FilesystemRecordStore,
    deployment_record: DeploymentRecord,
    artifact_manifest: ArtifactIdentityManifest,
) -> Path | None:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(deployment_record.instance):
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
    record_store: FilesystemRecordStore,
    request: PromotionRequest,
) -> ReleaseTupleRecord | None:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(request.from_instance):
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
    record_store: FilesystemRecordStore,
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
            "Promotion requires a current source release tuple before Harbor can mint the destination tuple. "
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
    record_store: FilesystemRecordStore,
    source_tuple: ReleaseTupleRecord | None,
    deployment_record: DeploymentRecord,
    promotion_record: PromotionRecord,
) -> Path | None:
    if source_tuple is None:
        return None
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(promotion_record.to_instance):
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
    record_store: FilesystemRecordStore,
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
    record_store: FilesystemRecordStore,
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


@click.group()
def main() -> None:
    """Control-plane CLI."""


@main.group()
def artifacts() -> None:
    """Artifact manifest commands."""


@main.group()
def service() -> None:
    """Harbor service commands."""


@service.command("serve")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--policy-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=8080, show_default=True)
@click.option(
    "--audience",
    default="harbor.shinycomputers.com",
    show_default=True,
    help="Expected GitHub OIDC audience for Harbor service tokens.",
)
def service_serve(state_dir: Path, policy_file: Path, host: str, port: int, audience: str) -> None:
    serve_harbor_service(
        state_dir=state_dir,
        policy_file=policy_file,
        host=host,
        port=port,
        audience=audience,
    )


@artifacts.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_write(state_dir: Path, input_file: Path) -> None:
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


@artifacts.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--artifact-id", required=True)
def artifacts_show(state_dir: Path, artifact_id: str) -> None:
    manifest = _store(state_dir).read_artifact_manifest(artifact_id)
    click.echo(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))


@artifacts.command("ingest")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_ingest(state_dir: Path, input_file: Path) -> None:
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


@main.group("release-tuples")
def release_tuples() -> None:
    """Release tuple state commands."""


@release_tuples.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
def release_tuples_list(state_dir: Path) -> None:
    records = _store(state_dir).list_release_tuple_records()
    click.echo(
        json.dumps([record.model_dump(mode="json") for record in records], indent=2, sort_keys=True)
    )


@release_tuples.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--channel", "channel_name", required=True)
def release_tuples_show(state_dir: Path, context_name: str, channel_name: str) -> None:
    record = _store(state_dir).read_release_tuple_record(
        context_name=context_name,
        channel_name=channel_name,
    )
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@release_tuples.command("export-catalog")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--output-file", type=click.Path(path_type=Path), default=None)
def release_tuples_export_catalog(state_dir: Path, output_file: Path | None) -> None:
    records = _store(state_dir).list_release_tuple_records()
    if not records:
        raise click.ClickException("No release tuple records found to export.")
    rendered_catalog = control_plane_release_tuples.render_release_tuple_catalog_toml(records)
    if output_file is None:
        click.echo(rendered_catalog, nl=False)
        return
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(rendered_catalog, encoding="utf-8")
    click.echo(output_file)


@release_tuples.command("write-from-promotion")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def release_tuples_write_from_promotion(state_dir: Path, record_id: str) -> None:
    record_store = _store(state_dir)
    promotion_record = record_store.read_promotion_record(record_id)
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        promotion_record.from_instance
    ) or not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        promotion_record.to_instance
    ):
        raise click.ClickException(
            "Release tuple promotion only supports stable remote channels testing, prod."
        )
    deployment_record_id = promotion_record.deployment_record_id.strip()
    if not deployment_record_id:
        raise click.ClickException(
            "Promotion record is missing deployment_record_id. "
            "Write a promotion record with explicit deployment linkage before minting a promoted release tuple."
        )
    deployment_record = record_store.read_deployment_record(deployment_record_id)
    source_tuple = _read_source_release_tuple_for_promotion_record(
        record_store=record_store,
        promotion_record=promotion_record,
    )
    tuple_path = _write_promoted_release_tuple(
        record_store=record_store,
        source_tuple=source_tuple,
        deployment_record=deployment_record,
        promotion_record=promotion_record,
    )
    if tuple_path is None:
        raise click.ClickException(
            "Promotion record did not produce a stable release tuple destination."
        )
    click.echo(tuple_path)


@main.group("backup-gates")
def backup_gates() -> None:
    """Backup gate record commands."""


@backup_gates.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def backup_gates_write(state_dir: Path, input_file: Path) -> None:
    record = BackupGateRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_backup_gate_record(record)
    click.echo(record_path)


@backup_gates.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def backup_gates_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_backup_gate_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@backup_gates.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def backup_gates_list(state_dir: Path, context_name: str, instance_name: str, limit: int) -> None:
    records = _store(state_dir).list_backup_gate_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_summarize_backup_gate_record(record) for record in records], indent=2, sort_keys=True
        )
    )


@main.group()
def promotions() -> None:
    """Promotion record commands."""


@promotions.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def promotions_write(state_dir: Path, input_file: Path) -> None:
    record = PromotionRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promotions.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def promotions_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_promotion_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@promotions.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--from-instance", "from_instance_name", default="")
@click.option("--to-instance", "to_instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def promotions_list(
    state_dir: Path,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    limit: int,
) -> None:
    records = _store(state_dir).list_promotion_records(
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_summarize_promotion_record(record) for record in records], indent=2, sort_keys=True
        )
    )


@main.group()
def deployments() -> None:
    """Deployment record commands."""


@deployments.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def deployments_write(state_dir: Path, input_file: Path) -> None:
    record = DeploymentRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_deployment_record(record)
    click.echo(record_path)


@deployments.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def deployments_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_deployment_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@deployments.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def deployments_list(state_dir: Path, context_name: str, instance_name: str, limit: int) -> None:
    records = _store(state_dir).list_deployment_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_summarize_deployment_record(record) for record in records], indent=2, sort_keys=True
        )
    )


@main.group()
def inventory() -> None:
    """Environment inventory commands."""


@inventory.command("write-from-deployment")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def inventory_write_from_deployment(state_dir: Path, record_id: str) -> None:
    record_store = _store(state_dir)
    deployment_record = record_store.read_deployment_record(record_id)
    inventory_path = _write_environment_inventory(
        record_store=record_store,
        deployment_record=deployment_record,
    )
    click.echo(inventory_path)


@inventory.command("write-from-promotion")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def inventory_write_from_promotion(state_dir: Path, record_id: str) -> None:
    record_store = _store(state_dir)
    promotion_record = record_store.read_promotion_record(record_id)
    inventory_path = _write_environment_inventory_from_promotion(
        record_store=record_store,
        promotion_record=promotion_record,
    )
    click.echo(inventory_path)


@inventory.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_show(state_dir: Path, context_name: str, instance_name: str) -> None:
    record = _store(state_dir).read_environment_inventory(
        context_name=context_name, instance_name=instance_name
    )
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@inventory.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
def inventory_list(state_dir: Path) -> None:
    records = _store(state_dir).list_environment_inventory()
    click.echo(
        json.dumps([record.model_dump(mode="json") for record in records], indent=2, sort_keys=True)
    )


@inventory.command("overview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
def inventory_overview(state_dir: Path, context_name: str) -> None:
    payload = _build_environment_overview_payloads(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@inventory.command("status")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_status(state_dir: Path, context_name: str, instance_name: str) -> None:
    payload = _build_environment_status_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        instance_name=instance_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@main.group("harbor-previews")
def harbor_previews() -> None:
    """Harbor preview record and read-model commands."""


@harbor_previews.command("write-preview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_write_preview(state_dir: Path, input_file: Path) -> None:
    request = PreviewMutationRequest.model_validate(_load_json_file(input_file))
    record = build_preview_record_from_request(
        control_plane_root=_control_plane_root(),
        record_store=_store(state_dir),
        request=request,
    )
    record_path = _store(state_dir).write_preview_record(record)
    click.echo(record_path)


@harbor_previews.command("write-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_write_generation(state_dir: Path, input_file: Path) -> None:
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    record = build_preview_generation_record_from_request(
        record_store=_store(state_dir),
        request=request,
    )
    record_path = _store(state_dir).write_preview_generation_record(record)
    click.echo(record_path)


@harbor_previews.command("write-enablement")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_write_enablement(state_dir: Path, input_file: Path) -> None:
    record = PreviewEnablementRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_preview_enablement_record(record)
    click.echo(record_path)


@harbor_previews.command("request-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--preview-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--generation-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
def harbor_previews_request_generation(
    state_dir: Path,
    preview_input_file: Path,
    generation_input_file: Path,
) -> None:
    record_store = _store(state_dir)
    preview_request = PreviewMutationRequest.model_validate(_load_json_file(preview_input_file))
    generation_request = PreviewGenerationMutationRequest.model_validate(
        _load_json_file(generation_input_file)
    )
    result_payload = _apply_harbor_request_generation(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@harbor_previews.command("write-from-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--preview-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--generation-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
def harbor_previews_write_from_generation(
    state_dir: Path,
    preview_input_file: Path,
    generation_input_file: Path,
) -> None:
    record_store = _store(state_dir)
    preview_request = PreviewMutationRequest.model_validate(_load_json_file(preview_input_file))
    generation_request = PreviewGenerationMutationRequest.model_validate(
        _load_json_file(generation_input_file)
    )
    result_payload = _apply_harbor_generation_evidence(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@harbor_previews.command("write-destroyed")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_write_destroyed(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewDestroyMutationRequest.model_validate(_load_json_file(input_file))
    result_payload = _apply_harbor_destroy_preview(
        record_store=record_store,
        request=request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@harbor_previews.command("mark-generation-ready")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_mark_generation_ready(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    if not request.generation_id.strip():
        raise click.ClickException("Ready-generation transition requires generation_id.")
    preview_record = _read_harbor_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    _read_harbor_generation_or_fail(
        record_store=record_store,
        preview_id=preview_record.preview_id,
        generation_id=request.generation_id,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    transitioned_preview = apply_generation_ready_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    click.echo(
        json.dumps(
            {
                "generation_id": generation_record.generation_id,
                "generation_path": str(generation_path),
                "preview_id": transitioned_preview.preview_id,
                "preview_path": str(preview_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


@harbor_previews.command("mark-generation-failed")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_mark_generation_failed(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    if not request.generation_id.strip():
        raise click.ClickException("Failed-generation transition requires generation_id.")
    preview_record = _read_harbor_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    _read_harbor_generation_or_fail(
        record_store=record_store,
        preview_id=preview_record.preview_id,
        generation_id=request.generation_id,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    transitioned_preview = apply_generation_failed_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    click.echo(
        json.dumps(
            {
                "generation_id": generation_record.generation_id,
                "generation_path": str(generation_path),
                "preview_id": transitioned_preview.preview_id,
                "preview_path": str(preview_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


@harbor_previews.command("destroy-preview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_destroy_preview(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewDestroyMutationRequest.model_validate(_load_json_file(input_file))
    result_payload = _apply_harbor_destroy_preview(
        record_store=record_store,
        request=request,
    )
    click.echo(result_payload["preview_path"])


@harbor_previews.command("ingest-pr-event")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def harbor_previews_ingest_pr_event(
    state_dir: Path, input_file: Path, apply_intent: bool, deliver_feedback: bool
) -> None:
    event = GitHubPullRequestEvent.model_validate(_load_json_file(input_file))
    payload = _ingest_harbor_pr_event_payload(
        state_dir=state_dir,
        event=event,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("ingest-github-webhook")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--event-name", default="pull_request", show_default=True)
@click.option("--delivery-id", default="", help="Optional GitHub delivery id for traceability.")
@click.option("--signature-256", default="", help="Raw X-Hub-Signature-256 header value.")
@click.option("--allow-unsigned", is_flag=True, help="Explicit local/manual bypass for signature verification.")
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def harbor_previews_ingest_github_webhook(
    state_dir: Path,
    input_file: Path,
    event_name: str,
    delivery_id: str,
    signature_256: str,
    allow_unsigned: bool,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    raw_payload_bytes, webhook_payload = _load_github_webhook_json_file(input_file)
    payload = _ingest_harbor_github_webhook_payload(
        state_dir=state_dir,
        event_name=event_name,
        raw_payload_bytes=raw_payload_bytes,
        webhook_payload=webhook_payload,
        delivery_id=delivery_id,
        delivery_source="github-webhook",
        signature_256=signature_256,
        allow_unsigned=allow_unsigned,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _load_json_object_file(input_file: Path, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(input_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise click.ClickException(f"{description} must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{description} must decode to a JSON object.")
    return payload


def _load_github_webhook_capture_headers(input_file: Path) -> dict[str, str]:
    payload = _load_json_object_file(input_file, description="GitHub webhook headers file")
    headers: dict[str, str] = {}
    for header_name, header_value in payload.items():
        if not isinstance(header_value, str):
            raise click.ClickException(
                "GitHub webhook headers file must map header names to string values."
            )
        headers[str(header_name)] = header_value
    return headers


def _github_webhook_capture_header_value(headers: dict[str, str], name: str) -> str:
    normalized_name = name.strip().lower()
    if not normalized_name:
        return ""
    for header_name, header_value in headers.items():
        if header_name.strip().lower() == normalized_name:
            return header_value.strip()
    return ""


def _github_webhook_capture_metadata_headers(headers: dict[str, str]) -> dict[str, str]:
    exact_redacted_header_names = {
        "authorization",
        "baggage",
        "cookie",
        "cf-connecting-ip",
        "proxy-authorization",
        "true-client-ip",
        "x-real-ip",
    }
    metadata_headers: dict[str, str] = {}
    for header_name, header_value in headers.items():
        normalized_header_name = header_name.strip().lower()
        if normalized_header_name in exact_redacted_header_names or normalized_header_name == "forwarded":
            metadata_headers[header_name] = "[redacted]"
            continue
        if normalized_header_name.startswith("x-forwarded-"):
            metadata_headers[header_name] = "[redacted]"
            continue
        metadata_headers[header_name] = header_value
    return metadata_headers


def _split_http_capture_text(http_capture_text: str) -> tuple[str, str]:
    for delimiter in ("\r\n\r\n", "\n\n"):
        if delimiter in http_capture_text:
            header_text, body_text = http_capture_text.split(delimiter, 1)
            return header_text, body_text
    raise click.ClickException(
        "GitHub webhook HTTP capture must contain headers, a blank line, and a JSON body."
    )


def _parse_github_webhook_http_capture(input_file: Path) -> tuple[str, dict[str, str], bytes]:
    try:
        http_capture_text = input_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(f"GitHub webhook HTTP capture must be valid UTF-8 text: {exc}") from exc
    header_text, body_text = _split_http_capture_text(http_capture_text)
    header_lines = header_text.splitlines()
    if not header_lines:
        raise click.ClickException("GitHub webhook HTTP capture is missing the request line.")
    request_line = header_lines[0].strip()
    if not request_line.startswith("POST ") or "HTTP/" not in request_line:
        raise click.ClickException(
            "GitHub webhook HTTP capture must start with a POST request line such as 'POST /github-webhook HTTP/1.1'."
        )
    headers: dict[str, str] = {}
    for header_line in header_lines[1:]:
        if not header_line.strip():
            continue
        if ":" not in header_line:
            raise click.ClickException(
                "GitHub webhook HTTP capture headers must use the 'Name: value' format."
            )
        header_name, header_value = header_line.split(":", 1)
        normalized_name = header_name.strip()
        if not normalized_name:
            raise click.ClickException("GitHub webhook HTTP capture contains a blank header name.")
        headers[normalized_name] = header_value.strip()
    if not headers:
        raise click.ClickException("GitHub webhook HTTP capture must include at least one header.")
    for override_header_name in ("X-HTTP-Method-Override", "X-Method-Override"):
        override_method = _github_webhook_capture_header_value(headers, override_header_name)
        if override_method and override_method.upper() != "POST":
            raise click.ClickException(
                f"GitHub webhook HTTP capture {override_header_name} must not conflict with the captured POST request."
            )
    declared_transfer_encoding = _github_webhook_capture_header_value(headers, "Transfer-Encoding")
    if declared_transfer_encoding and declared_transfer_encoding.lower() != "identity":
        raise click.ClickException(
            "GitHub webhook HTTP capture Transfer-Encoding is unsupported for the saved-capture replay path."
        )
    declared_content_encoding = _github_webhook_capture_header_value(headers, "Content-Encoding")
    if declared_content_encoding and declared_content_encoding.lower() != "identity":
        raise click.ClickException(
            "GitHub webhook HTTP capture Content-Encoding is unsupported for the saved-capture replay path."
        )
    declared_trailer = _github_webhook_capture_header_value(headers, "Trailer")
    if declared_trailer:
        raise click.ClickException(
            "GitHub webhook HTTP capture Trailer declarations are unsupported for the saved-capture replay path."
        )
    declared_expect = _github_webhook_capture_header_value(headers, "Expect")
    if declared_expect:
        raise click.ClickException(
            "GitHub webhook HTTP capture Expect declarations are unsupported for the saved-capture replay path."
        )
    declared_connection = _github_webhook_capture_header_value(headers, "Connection")
    if declared_connection:
        raise click.ClickException(
            "GitHub webhook HTTP capture Connection declarations are unsupported for the saved-capture replay path."
        )
    declared_pragma = _github_webhook_capture_header_value(headers, "Pragma")
    if declared_pragma:
        raise click.ClickException(
            "GitHub webhook HTTP capture Pragma declarations are unsupported for the saved-capture replay path."
        )
    declared_cache_control = _github_webhook_capture_header_value(headers, "Cache-Control")
    if declared_cache_control:
        raise click.ClickException(
            "GitHub webhook HTTP capture Cache-Control declarations are unsupported for the saved-capture replay path."
        )
    declared_upgrade = _github_webhook_capture_header_value(headers, "Upgrade")
    if declared_upgrade:
        raise click.ClickException(
            "GitHub webhook HTTP capture Upgrade declarations are unsupported for the saved-capture replay path."
        )
    declared_te = _github_webhook_capture_header_value(headers, "TE")
    if declared_te:
        raise click.ClickException(
            "GitHub webhook HTTP capture TE declarations are unsupported for the saved-capture replay path."
        )
    declared_keep_alive = _github_webhook_capture_header_value(headers, "Keep-Alive")
    if declared_keep_alive:
        raise click.ClickException(
            "GitHub webhook HTTP capture Keep-Alive declarations are unsupported for the saved-capture replay path."
        )
    declared_proxy_connection = _github_webhook_capture_header_value(headers, "Proxy-Connection")
    if declared_proxy_connection:
        raise click.ClickException(
            "GitHub webhook HTTP capture Proxy-Connection declarations are unsupported for the saved-capture replay path."
        )
    declared_content_type = _github_webhook_capture_header_value(headers, "Content-Type")
    if declared_content_type:
        normalized_media_type = declared_content_type.split(";", 1)[0].strip().lower()
        if normalized_media_type not in {"application/json"} and not normalized_media_type.endswith(
            "+json"
        ):
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Type must be JSON when present."
            )
    body_bytes = body_text.encode("utf-8")
    declared_content_length = _github_webhook_capture_header_value(headers, "Content-Length")
    if declared_content_length:
        try:
            expected_content_length = int(declared_content_length)
        except ValueError as exc:
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length header must be an integer when present."
            ) from exc
        if expected_content_length < 0:
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length header must not be negative."
            )
        if expected_content_length != len(body_bytes):
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length does not match the saved body bytes."
            )
    return request_line, headers, body_bytes


def _merge_github_webhook_capture_evidence(
    *,
    evidence_payload: dict[str, object] | None,
    request_line: str,
) -> dict[str, object] | None:
    if not request_line.strip():
        return evidence_payload

    merged_evidence = dict(evidence_payload) if evidence_payload is not None else {}
    http_request_payload = merged_evidence.get("http_request")
    if http_request_payload is None:
        merged_evidence["http_request"] = {"request_line": request_line}
        return merged_evidence
    if not isinstance(http_request_payload, dict):
        raise click.ClickException(
            "GitHub webhook evidence file field 'http_request' must be a JSON object when provided."
        )

    request_line_payload = http_request_payload.get("request_line")
    if request_line_payload is not None:
        if not isinstance(request_line_payload, str):
            raise click.ClickException(
                "GitHub webhook evidence file field 'http_request.request_line' must be a string when provided."
            )
        if request_line_payload != request_line:
            raise click.ClickException(
                "GitHub webhook evidence file request_line conflicts with the saved HTTP capture request line."
            )
        return merged_evidence

    merged_http_request_payload = dict(http_request_payload)
    merged_http_request_payload["request_line"] = request_line
    merged_evidence["http_request"] = merged_http_request_payload
    return merged_evidence


@harbor_previews.command("build-github-webhook-replay-envelope")
@click.option("--payload-file", type=click.Path(exists=True, path_type=Path))
@click.option("--http-capture-file", type=click.Path(exists=True, path_type=Path))
@click.option("--headers-file", type=click.Path(exists=True, path_type=Path))
@click.option("--event-name", default="", help="Optional GitHub event name override.")
@click.option("--signature-256", default="", help="Optional X-Hub-Signature-256 override.")
@click.option("--delivery-id", default="", help="Optional GitHub delivery id override.")
@click.option("--delivery-source", default="", help="Optional top-level delivery source override.")
@click.option("--allow-unsigned", is_flag=True, help="Emit an explicit unsigned replay envelope.")
@click.option("--recorded-at", default="", help="Optional capture timestamp for traceability.")
@click.option("--capture-source", default="", help="Optional capture source label for replay metadata.")
@click.option("--evidence-file", type=click.Path(exists=True, path_type=Path))
@click.option("--output-file", type=click.Path(path_type=Path))
def harbor_previews_build_github_webhook_replay_envelope(
    payload_file: Path | None,
    http_capture_file: Path | None,
    headers_file: Path | None,
    event_name: str,
    signature_256: str,
    delivery_id: str,
    delivery_source: str,
    allow_unsigned: bool,
    recorded_at: str,
    capture_source: str,
    evidence_file: Path | None,
    output_file: Path | None,
) -> None:
    if (payload_file is None) == (http_capture_file is None):
        raise click.ClickException(
            "Provide exactly one of --payload-file or --http-capture-file when building a GitHub replay envelope."
        )
    if http_capture_file is not None and headers_file is not None:
        raise click.ClickException(
            "--headers-file cannot be combined with --http-capture-file because the HTTP capture already carries headers."
        )

    capture_headers: dict[str, str] = {}
    captured_request_line = ""
    if http_capture_file is not None:
        captured_request_line, capture_headers, raw_payload_bytes = _parse_github_webhook_http_capture(
            http_capture_file
        )
        _load_github_webhook_json_bytes(raw_payload_bytes, description="GitHub webhook HTTP capture body")
    else:
        assert payload_file is not None
        raw_payload_bytes, _ = _load_github_webhook_json_file(payload_file)
        capture_headers = (
            _load_github_webhook_capture_headers(headers_file) if headers_file is not None else {}
        )
    capture_event_name = _github_webhook_capture_header_value(capture_headers, "X-GitHub-Event")
    evidence_payload = (
        _load_json_object_file(evidence_file, description="GitHub webhook evidence file")
        if evidence_file is not None
        else None
    )
    evidence_payload = _merge_github_webhook_capture_evidence(
        evidence_payload=evidence_payload,
        request_line=captured_request_line,
    )
    raw_payload_text = raw_payload_bytes.decode("utf-8")

    capture_payload: dict[str, object] | None = None
    if capture_headers or recorded_at.strip() or capture_source.strip() or evidence_payload is not None:
        capture_payload = {}
        if recorded_at.strip():
            capture_payload["recorded_at"] = recorded_at.strip()
        if capture_source.strip():
            capture_payload["source"] = capture_source.strip()
        capture_metadata_headers = _github_webhook_capture_metadata_headers(capture_headers)
        if capture_metadata_headers:
            capture_payload["headers"] = capture_metadata_headers
        if evidence_payload is not None:
            capture_payload["evidence"] = evidence_payload

    resolved_event_name = event_name.strip() or capture_event_name or "pull_request"
    top_level_event_name = event_name.strip() or ("" if capture_event_name else resolved_event_name)
    top_level_signature_256 = signature_256.strip()
    top_level_delivery_id = delivery_id.strip()
    envelope = GitHubWebhookReplayEnvelope.model_validate(
        {
            "schema_version": 1,
            "adapter": "github_webhook",
            "event_name": top_level_event_name,
            "signature_256": top_level_signature_256,
            "allow_unsigned": allow_unsigned,
            "delivery_id": top_level_delivery_id,
            "delivery_source": delivery_source.strip(),
            "payload_text": raw_payload_text,
            "capture": capture_payload,
        }
    )
    envelope_json = json.dumps(
        envelope.model_dump(mode="json", exclude_none=True),
        indent=2,
        sort_keys=True,
    )
    if output_file is not None:
        output_file.write_text(f"{envelope_json}\n", encoding="utf-8")
    click.echo(envelope_json)


@harbor_previews.command("replay-github-webhook")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def harbor_previews_replay_github_webhook(
    state_dir: Path,
    input_file: Path,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    try:
        envelope = GitHubWebhookReplayEnvelope.model_validate(_load_json_file(input_file))
    except ValidationError as exc:
        raise click.ClickException(f"Invalid GitHub webhook replay envelope: {exc}") from exc
    resolved_event_name = envelope.resolved_event_name()
    resolved_delivery_id = envelope.resolved_delivery_id()
    resolved_delivery_source = envelope.resolved_delivery_source()
    raw_payload_bytes, webhook_payload = _load_github_webhook_replay_envelope(envelope)
    payload = _ingest_harbor_github_webhook_payload(
        state_dir=state_dir,
        event_name=resolved_event_name,
        raw_payload_bytes=raw_payload_bytes,
        webhook_payload=webhook_payload,
        delivery_id=resolved_delivery_id,
        delivery_source=resolved_delivery_source,
        signature_256=envelope.resolved_signature_256(),
        allow_unsigned=envelope.allow_unsigned,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    payload["webhook_replay"] = {
        "adapter": envelope.adapter,
        "event_name": resolved_event_name,
        "delivery_id": resolved_delivery_id,
        "delivery_source": resolved_delivery_source,
    }
    replay_capture_payload = envelope.replay_capture_payload()
    if replay_capture_payload is not None:
        payload["webhook_replay"]["capture"] = replay_capture_payload
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _ingest_harbor_pr_event_payload(
    *,
    state_dir: Path,
    event: GitHubPullRequestEvent,
    apply_intent: bool,
    deliver_feedback: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    record_store = _store(state_dir)
    payload = build_pull_request_event_action_payload(
        control_plane_root=control_plane_root,
        record_store=record_store,
        event=event,
    )
    decision_payload = payload.get("decision")
    resolved_context = (
        str(decision_payload.get("resolved_context", "")).strip() if isinstance(decision_payload, dict) else ""
    )
    preview_payload = payload.get("preview")
    if not resolved_context and isinstance(preview_payload, dict):
        resolved_context = str(preview_payload.get("context", "")).strip()
    request_metadata_payload = payload.get("request_metadata")
    request_metadata = (
        HarborPreviewRequestParseResult.model_validate(request_metadata_payload)
        if isinstance(request_metadata_payload, dict)
        else HarborPreviewRequestParseResult(status="missing")
    )
    resolved_manifest_payload = payload.get("manifest")
    resolved_manifest = (
        HarborResolvedPreviewManifest.model_validate(resolved_manifest_payload)
        if isinstance(resolved_manifest_payload, dict)
        else None
    )
    enablement_record = _build_harbor_preview_enablement_record(
        context_name=resolved_context,
        event=event,
        request_metadata=request_metadata,
        resolved_manifest=resolved_manifest,
    )
    if enablement_record is not None:
        record_store.write_preview_enablement_record(enablement_record)
    payload["enablement_record"] = (
        enablement_record.model_dump(mode="json") if enablement_record is not None else None
    )
    if apply_intent:
        payload["apply"] = _apply_harbor_pr_event_intent(
            control_plane_root=control_plane_root,
            record_store=record_store,
            payload=payload,
        )
        request_metadata_payload = payload.get("request_metadata")
        action = decision_payload.get("action", "") if isinstance(decision_payload, dict) else ""
        payload["feedback"] = build_pull_request_feedback_payload(
            record_store=record_store,
            event=event,
            action=action if isinstance(action, str) else "",
            preview=None,
            request_metadata=HarborPreviewRequestParseResult.model_validate(request_metadata_payload),
            resolved_manifest=(
                HarborResolvedPreviewManifest.model_validate(payload["manifest"])
                if isinstance(payload.get("manifest"), dict)
                else None
            ),
            apply_result=payload["apply"],
        )
    if deliver_feedback:
        resolved_context = (
            decision_payload.get("resolved_context", "") if isinstance(decision_payload, dict) else ""
        )
        feedback_payload = payload.get("feedback")
        if not isinstance(feedback_payload, dict):
            raise click.ClickException("Harbor feedback payload is missing before delivery.")
        payload["feedback_delivery"] = deliver_pull_request_feedback(
            control_plane_root=control_plane_root,
            record_store=record_store,
            event=event,
            resolved_context=resolved_context if isinstance(resolved_context, str) else "",
            feedback_payload=feedback_payload,
        )
    return payload


def _ingest_harbor_github_webhook_payload(
    *,
    state_dir: Path,
    event_name: str,
    raw_payload_bytes: bytes,
    webhook_payload: dict[str, object],
    delivery_id: str,
    delivery_source: str,
    signature_256: str,
    allow_unsigned: bool,
    apply_intent: bool,
    deliver_feedback: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    signature_verification = _verify_harbor_github_webhook_signature(
        control_plane_root=control_plane_root,
        event_name=event_name,
        webhook_payload=webhook_payload,
        raw_payload_bytes=raw_payload_bytes,
        signature_256=signature_256,
        allow_unsigned=allow_unsigned,
    )
    event = adapt_github_webhook_pull_request_event(
        event_name=event_name,
        webhook_payload=webhook_payload,
    )
    payload = _ingest_harbor_pr_event_payload(
        state_dir=state_dir,
        event=event,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    payload["webhook"] = {
        "event_name": event_name,
        "adapter": "github_pull_request",
        "delivery": {
            "delivery_id": delivery_id.strip(),
            "delivery_source": delivery_source.strip() or "github-webhook",
        },
        "signature_verification": signature_verification,
    }
    return payload


def _load_github_webhook_json_file(input_file: Path) -> tuple[bytes, dict[str, object]]:
    raw_payload_bytes = input_file.read_bytes()
    webhook_payload = _load_github_webhook_json_bytes(
        raw_payload_bytes,
        description="GitHub webhook input file",
    )
    return raw_payload_bytes, webhook_payload


def _load_github_webhook_replay_envelope(
    envelope: GitHubWebhookReplayEnvelope,
) -> tuple[bytes, dict[str, object]]:
    if envelope.payload_text.strip():
        raw_payload_bytes = envelope.payload_text.encode("utf-8")
        try:
            webhook_payload = json.loads(envelope.payload_text)
        except JSONDecodeError as exc:
            raise click.ClickException(
                f"GitHub webhook replay envelope payload_text must be valid JSON: {exc}"
            ) from exc
        if not isinstance(webhook_payload, dict):
            raise click.ClickException(
                "GitHub webhook replay envelope payload_text must decode to a JSON object."
            )
        return raw_payload_bytes, webhook_payload
    if envelope.payload is None:
        raise click.ClickException("GitHub webhook replay envelope is missing payload content.")
    return json.dumps(envelope.payload).encode("utf-8"), envelope.payload


def _verify_harbor_github_webhook_signature(
    *,
    control_plane_root: Path,
    event_name: str,
    webhook_payload: dict[str, object],
    raw_payload_bytes: bytes,
    signature_256: str,
    allow_unsigned: bool,
) -> dict[str, object]:
    if allow_unsigned:
        return {
            "mode": "bypass",
            "verified": False,
            "reason": "allow_unsigned",
        }

    context_name = _resolve_harbor_github_webhook_context(
        event_name=event_name,
        webhook_payload=webhook_payload,
    )
    if not context_name:
        raise click.ClickException(
            "GitHub webhook signature verification could not resolve a Harbor context from the raw payload."
        )
    secret = resolve_harbor_github_webhook_secret(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    if not secret:
        raise click.ClickException(
            f"Runtime environments file is missing GITHUB_WEBHOOK_SECRET for Harbor context {context_name!r}."
        )
    verify_github_webhook_signature(
        payload_bytes=raw_payload_bytes,
        signature_header=signature_256,
        secret=secret,
    )
    return {
        "mode": "verified",
        "verified": True,
        "context": context_name,
    }


def _resolve_harbor_github_webhook_context(*, event_name: str, webhook_payload: dict[str, object]) -> str:
    if event_name.strip() != "pull_request":
        return ""
    repository_payload = webhook_payload.get("repository")
    if not isinstance(repository_payload, dict):
        return ""
    repo_name = repository_payload.get("name")
    if not isinstance(repo_name, str) or not repo_name.strip():
        return ""
    return harbor_anchor_repo_context(repo=repo_name)


@harbor_previews.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
def harbor_previews_list(state_dir: Path, context_name: str) -> None:
    payload = build_preview_inventory_payload(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("show-tenant")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--anchor-repo", default="")
@click.option(
    "--release-tuples-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Use an explicit release tuple catalog while resolving tenant preview recipes.",
)
def harbor_previews_show_tenant(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    release_tuples_file: Path | None,
) -> None:
    with _harbor_release_tuples_file_override(release_tuples_file):
        payload = _build_harbor_tenant_payload(
            control_plane_root=_control_plane_root(),
            record_store=_store(state_dir),
            context_name=context_name,
            anchor_repo=anchor_repo,
        )
    if payload is None:
        raise click.ClickException("No Harbor tenant environment evidence found for the requested scope.")
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("render-index-page")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--output-file", type=click.Path(path_type=Path))
@click.option(
    "--release-tuples-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Use an explicit release tuple catalog while resolving tenant preview recipes.",
)
def harbor_previews_render_index_page(
    state_dir: Path,
    context_name: str,
    output_file: Path | None,
    release_tuples_file: Path | None,
) -> None:
    record_store = _store(state_dir)
    payload = build_preview_inventory_payload(
        record_store=record_store,
        context_name=context_name,
    )
    with _harbor_release_tuples_file_override(release_tuples_file):
        tenant_payload = _build_harbor_tenant_payload(
            control_plane_root=_control_plane_root(),
            record_store=record_store,
            context_name=context_name,
        )
    html_output = _render_harbor_preview_index_page_html(payload, tenant_payload=tenant_payload)
    if output_file is not None:
        output_file.write_text(html_output, encoding="utf-8")
        return
    click.echo(html_output)


@harbor_previews.command("render-policy-page")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--output-file", type=click.Path(path_type=Path))
def harbor_previews_render_policy_page(
    state_dir: Path,
    context_name: str,
    output_file: Path | None,
) -> None:
    payload = build_preview_inventory_payload(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    html_output = _render_harbor_preview_policy_page_html(payload)
    if output_file is not None:
        output_file.write_text(html_output, encoding="utf-8")
        return
    click.echo(html_output)


@harbor_previews.command("render-site")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option(
    "--release-tuples-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Use an explicit release tuple catalog while resolving tenant preview recipes.",
)
def harbor_previews_render_site(
    state_dir: Path,
    context_name: str,
    output_dir: Path,
    release_tuples_file: Path | None,
) -> None:
    _write_harbor_site_bundle(
        state_dir=state_dir,
        output_dir=output_dir,
        context_name=context_name,
        release_tuples_file=release_tuples_file,
    )


@harbor_previews.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
def harbor_previews_show(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> None:
    payload = _require_harbor_preview_status_payload(
        state_dir=state_dir,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("render-status-page")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
@click.option("--output-file", type=click.Path(path_type=Path))
def harbor_previews_render_status_page(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
    output_file: Path | None,
) -> None:
    payload = _require_harbor_preview_status_payload(
        state_dir=state_dir,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    html_output = _render_harbor_preview_status_page_html(payload)
    if output_file is not None:
        output_file.write_text(html_output, encoding="utf-8")
        return
    click.echo(html_output)


@harbor_previews.command("history")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
def harbor_previews_history(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> None:
    payload = build_preview_history_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if payload is None:
        raise click.ClickException(
            f"No Harbor preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _read_harbor_preview_or_fail(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
):
    preview_record = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview_record is None:
        raise click.ClickException(
            f"No Harbor preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    return preview_record


def _read_harbor_generation_or_fail(
    *,
    record_store: FilesystemRecordStore,
    preview_id: str,
    generation_id: str,
):
    generations = record_store.list_preview_generation_records(preview_id=preview_id)
    for generation_record in generations:
        if generation_record.generation_id == generation_id:
            return generation_record
    raise click.ClickException(
        f"No Harbor preview generation found for {preview_id} generation {generation_id}."
    )


def _apply_harbor_request_generation(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    preview_record = _upsert_harbor_preview_from_request(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=preview_request,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=generation_request,
    )
    transitioned_preview = apply_generation_requested_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "generation_id": generation_record.generation_id,
        "generation_path": str(generation_path),
        "preview_id": transitioned_preview.preview_id,
        "preview_path": str(preview_path),
    }


def _upsert_harbor_preview_from_request(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: PreviewMutationRequest,
) -> PreviewRecord:
    return shared_upsert_harbor_preview_from_request(
        control_plane_root_path=control_plane_root,
        record_store=record_store,
        request=request,
    )


def _apply_harbor_generation_evidence(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    return shared_apply_harbor_generation_evidence(
        control_plane_root_path=control_plane_root,
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )


def _apply_harbor_destroy_preview(
    *,
    record_store: FilesystemRecordStore,
    request: PreviewDestroyMutationRequest,
) -> dict[str, object]:
    return shared_apply_harbor_destroy_preview(
        record_store=record_store,
        request=request,
    )


def _apply_harbor_pr_event_intent(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    payload: dict[str, object],
) -> dict[str, object]:
    mutation_payload = payload.get("mutation")
    if not isinstance(mutation_payload, dict):
        return {
            "applied": False,
            "reason": "no_mutation_intent",
        }
    intent = HarborPullRequestMutationIntent.model_validate(mutation_payload)
    if intent.command == "request-generation":
        if intent.preview_request is None:
            raise click.ClickException("Resolved Harbor PR-event request-generation intent is missing preview_request.")
        if intent.generation_request is None:
            return {
                "applied": False,
                "reason": "manifest_resolution_required",
            }
        result_payload = _apply_harbor_request_generation(
            control_plane_root=control_plane_root,
            record_store=record_store,
            preview_request=intent.preview_request,
            generation_request=intent.generation_request,
        )
        return {
            "applied": True,
            "command": intent.command,
            "result": result_payload,
        }
    if intent.destroy_request is None:
        raise click.ClickException("Resolved Harbor PR-event destroy intent is missing destroy_request.")
    result_payload = _apply_harbor_destroy_preview(
        record_store=record_store,
        request=intent.destroy_request,
    )
    return {
        "applied": True,
        "command": intent.command,
        "result": result_payload,
    }


@main.group()
def environments() -> None:
    """Runtime environment contract commands."""


@environments.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", default="local", show_default=True)
@click.option("--json-output", is_flag=True, default=False)
def environments_resolve(context_name: str, instance_name: str, json_output: bool) -> None:
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=_control_plane_root(),
        context_name=context_name,
        instance_name=instance_name,
    )
    if json_output:
        click.echo(
            json.dumps(
                {
                    "context": context_name,
                    "instance": instance_name,
                    "environment": environment_values,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    for environment_key in sorted(environment_values):
        click.echo(f"{environment_key}={environment_values[environment_key]}")


@environments.command("show-live-target")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def environments_show_live_target(context_name: str, instance_name: str) -> None:
    payload = _build_live_target_runtime_contract_payload(
        context_name=context_name,
        instance_name=instance_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@environments.command("sync-live-target")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--apply", "apply_changes", is_flag=True, default=False)
def environments_sync_live_target(context_name: str, instance_name: str, apply_changes: bool) -> None:
    payload = _sync_live_target_from_tracked_contract(
        context_name=context_name,
        instance_name=instance_name,
        apply_changes=apply_changes,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@main.group()
def promote() -> None:
    """Promotion workflow commands."""


@promote.command("record")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
@click.option("--artifact-id", required=True)
@click.option("--backup-record-id", default="", show_default=False)
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--target-name", required=True)
@click.option("--target-type", type=click.Choice(["compose", "application"]), required=True)
@click.option("--deploy-mode", required=True)
@click.option("--deployment-id", default="", show_default=False)
def promote_record(
    state_dir: Path,
    record_id: str,
    artifact_id: str,
    backup_record_id: str,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    target_name: str,
    target_type: str,
    deploy_mode: str,
    deployment_id: str,
) -> None:
    record = build_promotion_record(
        record_id=record_id,
        artifact_id=artifact_id,
        backup_record_id=backup_record_id,
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        target_name=target_name,
        target_type=target_type,
        deploy_mode=deploy_mode,
        deployment_id=deployment_id,
    )
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promote.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--artifact-id", required=True)
@click.option("--backup-record-id", required=True)
@click.option("--source-ref", "source_git_ref", default="")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", "timeout_override_seconds", type=int, default=None)
@click.option("--verify-health/--no-verify-health", default=True)
@click.option("--health-timeout", "health_timeout_override_seconds", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-cache", is_flag=True, default=False)
@click.option("--allow-dirty", is_flag=True, default=False)
def promote_resolve(
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
) -> None:
    request = _resolve_native_promotion_request(
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
    )
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@promote.command("execute")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def promote_execute(
    state_dir: Path,
    input_file: Path,
    env_file: Path | None,
) -> None:
    request = PromotionRequest.model_validate(_load_json_file(input_file))
    record_store = _store(state_dir)
    resolved_artifact_id = _require_artifact_id(requested_artifact_id=request.artifact_id)
    _read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    normalized_request = request.model_copy(update={"artifact_id": resolved_artifact_id})
    resolved_request, _backup_gate_record = _resolve_backup_gate_for_promotion(
        request=normalized_request,
        record_store=record_store,
    )
    source_release_tuple = _read_source_release_tuple_for_promotion(
        record_store=record_store,
        request=resolved_request,
    )
    record_id = generate_promotion_record_id(
        context_name=resolved_request.context,
        from_instance_name=resolved_request.from_instance,
        to_instance_name=resolved_request.to_instance,
    )
    if resolved_request.dry_run:
        _resolve_ship_request_for_promotion(request=resolved_request)
        click.echo(
            json.dumps(
                build_executed_promotion_record(
                    request=resolved_request,
                    record_id=record_id,
                    deployment_id="",
                    deployment_status="pending",
                ).model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
        )
        return

    pending_record = build_executed_promotion_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="",
        deployment_status="pending",
    )
    record_path = record_store.write_promotion_record(pending_record)

    try:
        ship_request = _resolve_ship_request_for_promotion(request=resolved_request)
        _record_path, deployment_record = _execute_ship(
            state_dir=state_dir,
            env_file=env_file,
            request=ship_request,
            mint_release_tuple=False,
        )
        if not isinstance(deployment_record, DeploymentRecord):
            raise click.ClickException(
                "Ship execution returned an unexpected non-record payload during promotion."
            )
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_record_id=deployment_record.record_id,
            deployment_id=deployment_record.deploy.deployment_id,
            deployment_status=deployment_record.deploy.status,
        )
    except (subprocess.CalledProcessError, click.ClickException, json.JSONDecodeError):
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
        )
        record_store.write_promotion_record(final_record)
        raise

    record_store.write_promotion_record(final_record)
    if deployment_record.wait_for_completion and deployment_record.deploy.status == "pass":
        _write_environment_inventory(
            record_store=record_store,
            deployment_record=deployment_record,
            promotion_record_id=final_record.record_id,
            promoted_from_instance=final_record.from_instance,
        )
        _write_promoted_release_tuple(
            record_store=record_store,
            source_tuple=source_release_tuple,
            deployment_record=deployment_record,
            promotion_record=final_record,
        )
    click.echo(record_path)


@main.group()
def ship() -> None:
    """Ship workflow commands."""


@ship.command("plan")
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def ship_plan(input_file: Path) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--artifact-id", required=True)
@click.option("--source-ref", "source_git_ref", default="")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", "timeout_override_seconds", type=int, default=None)
@click.option("--verify-health/--no-verify-health", default=True)
@click.option("--health-timeout", "health_timeout_override_seconds", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-cache", is_flag=True, default=False)
@click.option("--allow-dirty", is_flag=True, default=False)
def ship_resolve(
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
) -> None:
    request = _resolve_native_ship_request(
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
    )
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("execute")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def ship_execute(
    state_dir: Path,
    input_file: Path,
    env_file: Path | None,
) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    record_path, _record = _execute_ship(
        state_dir=state_dir,
        env_file=env_file,
        request=request,
    )
    if record_path is not None:
        click.echo(record_path)
