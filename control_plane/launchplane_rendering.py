import json
import os
from html import escape
from pathlib import Path

from control_plane.contracts.preview_request_metadata import (
    LAUNCHPLANE_ALLOWED_COMPANION_REPOS,
    LAUNCHPLANE_PREVIEW_REQUEST_BLOCK_INFO_STRING,
)
from control_plane.workflows.launchplane import (
    DEFAULT_LAUNCHPLANE_BASELINE_CHANNEL,
    LAUNCHPLANE_PREVIEW_ENABLE_LABEL,
)


def status_tone(value: str) -> str:
    normalized_value = value.strip().lower()
    if normalized_value in {"pass", "ready", "healthy", "serving"}:
        return "good"
    if normalized_value in {"fail", "failed", "destroyed"}:
        return "bad"
    if normalized_value in {
        "pending",
        "building",
        "deploying",
        "verifying",
        "requested",
        "paused",
        "unavailable",
    }:
        return "warn"
    return "neutral"


def json_object_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def json_object(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def int_from_json_value(value: object, *, default: int = 0) -> int:
    if value is None:
        return default
    if not isinstance(value, (str, int, float, bool)):
        return default
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def status_label(value: str) -> str:
    normalized_value = value.strip().replace("_", " ")
    return normalized_value or "unknown"


def generation_in_progress(value: str) -> bool:
    return value.strip().lower() in {"resolving", "building", "deploying", "verifying"}


def launchplane_action_slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "launchplane-preview"


def render_launchplane_action_recipe(
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


def build_launchplane_action_script(
    *,
    command_name: str,
    file_payloads: tuple[tuple[str, str, dict[str, object]], ...],
    command_args: tuple[str, ...],
) -> str:
    lines = ['STATE_DIR="/path/to/state"']
    for variable_name, file_path, payload in file_payloads:
        lines.append(f'{variable_name}="{file_path}"')
        lines.append(f"cat >\"${variable_name}\" <<'JSON'")
        lines.append(json.dumps(payload, indent=2, sort_keys=True))
        lines.append("JSON")
    command_parts = [
        "uv",
        "run",
        "launchplane",
        "launchplane-previews",
        command_name,
        "--state-dir",
        '"$STATE_DIR"',
    ]
    command_parts.extend(command_args)
    lines.append(" ".join(command_parts))
    return "\n".join(lines)


def render_launchplane_shell_document(
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
        render_launchplane_shell_nav_item(
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
        <div class=\"shell-brand-mark\">Launchplane control plane</div>
        <h1>Tenant environments and PR previews</h1>
        <p>Launchplane links testing, prod, and PR preview lanes for {context_html}. GitHub remains the review source; Launchplane carries environment state, routing, and promotion evidence.</p>
      </div>
      <nav class=\"shell-nav\" aria-label=\"Launchplane sections\">{nav_html}</nav>
    </header>
    <main class=\"page-body {body_class}\">{body_html}</main>
  </div>
</body>
</html>
"""


def render_launchplane_shell_nav_item(*, key: str, label: str, active_nav: str, href: str) -> str:
    class_name = "shell-nav-item active" if key == active_nav else "shell-nav-item"
    if href:
        return f'<a class="{class_name}" href="{escape(href)}">{escape(label)}</a>'
    return f'<span class="{class_name}">{escape(label)}</span>'


def launchplane_preview_bundle_relative_path(
    *,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> Path:
    return Path("previews") / context_name / anchor_repo / f"pr-{anchor_pr_number}.html"


def launchplane_environment_bundle_relative_path(*, context_name: str, instance_name: str) -> Path:
    return Path("environments") / context_name / f"{instance_name}.html"


def launchplane_promotion_bundle_relative_path(*, context_name: str) -> Path:
    return Path("promotions") / context_name / "testing-to-prod.html"


def relative_href(*, from_file: Path, to_file: Path) -> str:
    return os.path.relpath(to_file, start=from_file.parent)


def launchplane_inventory_bucket(row: dict[str, object]) -> str:
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


def launchplane_preview_enablement_record_id(
    *, context_name: str, anchor_repo: str, anchor_pr_number: int
) -> str:
    return f"{context_name}-{anchor_repo}-pr-{anchor_pr_number}"


def build_launchplane_promotion_resolve_recipe_script(
    *,
    context_name: str,
    artifact_id: str,
    backup_record_id: str,
) -> str:
    resolved_backup_record_id = backup_record_id.strip() or "backup-prod-pass-record-id"
    return "\n".join(
        (
            'PROMOTION_REQUEST_FILE="/tmp/launchplane-promotion-request.json"',
            f'uv run launchplane promote resolve --context "{context_name}" --from-instance testing --to-instance prod --artifact-id "{artifact_id}" --backup-record-id "{resolved_backup_record_id}" >"$PROMOTION_REQUEST_FILE"',
            'cat "$PROMOTION_REQUEST_FILE"',
        )
    )


def build_launchplane_backup_gate_write_recipe_script(
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
        'BACKUP_GATE_FILE="/tmp/launchplane-backup-gate.json"',
        "cat >\"$BACKUP_GATE_FILE\" <<'JSON'",
        json.dumps(payload, indent=2, sort_keys=True),
        "JSON",
        'uv run launchplane backup-gates write --state-dir "$STATE_DIR" --input-file "$BACKUP_GATE_FILE"',
    ]
    return "\n".join(lines)


def build_launchplane_promotion_execute_recipe_script(*, state_dir: str) -> str:
    return "\n".join(
        (
            f'STATE_DIR="{state_dir or "/path/to/state"}"',
            'PROMOTION_REQUEST_FILE="/tmp/launchplane-promotion-request.json"',
            'uv run launchplane promote execute --state-dir "$STATE_DIR" --input-file "$PROMOTION_REQUEST_FILE"',
        )
    )


def build_launchplane_environment_ship_recipe_script(
    *,
    context_name: str,
    instance_name: str,
    artifact_id: str,
    source_git_ref: str,
) -> str:
    request_file = f"/tmp/launchplane-{context_name}-{instance_name}-ship-request.json"
    return "\n".join(
        (
            'STATE_DIR="/path/to/state"',
            f'SHIP_REQUEST_FILE="{request_file}"',
            f'uv run launchplane ship resolve --context "{context_name}" --instance "{instance_name}" --artifact-id "{artifact_id}" --source-ref "{source_git_ref}" >"$SHIP_REQUEST_FILE"',
            'cat "$SHIP_REQUEST_FILE"',
            'uv run launchplane ship execute --state-dir "$STATE_DIR" --input-file "$SHIP_REQUEST_FILE"',
        )
    )


def render_launchplane_preview_policy_page_html(
    payload: dict[str, object],
    *,
    eligible_contexts: tuple[tuple[str, str], ...] = (),
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", ""))
    preview_rows = json_object_items(payload.get("previews"))
    active_preview_count = sum(
        1 for row in preview_rows if str(row.get("state", "")).strip().lower() != "destroyed"
    )
    retained_preview_count = sum(
        1 for row in preview_rows if str(row.get("state", "")).strip().lower() == "destroyed"
    )
    overview_href = escape((nav_links or {}).get("overview", "index.html") or "index.html")
    context_distribution_rows = []
    for context_value in sorted(
        {
            str(row.get("context", "")).strip()
            for row in preview_rows
            if str(row.get("context", "")).strip()
        }
    ):
        matching_rows = [
            row for row in preview_rows if str(row.get("context", "")).strip() == context_value
        ]
        active_count = sum(
            1 for row in matching_rows if str(row.get("state", "")).strip().lower() != "destroyed"
        )
        retained_count = sum(
            1 for row in matching_rows if str(row.get("state", "")).strip().lower() == "destroyed"
        )
        context_link = f"{overview_href}#scope=context:{escape(context_value)}"
        context_distribution_rows.append(
            "<tr>"
            f'<td><a href="{context_link}">{escape(context_value)}</a></td>'
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
      <p>When Launchplane is showing more than one tenant context, this page should still reveal how the current preview fleet is distributed across those contexts.</p>
      <table>
        <thead><tr><th>Context</th><th>Total</th><th>Active</th><th>Retained</th></tr></thead>
        <tbody>{"".join(context_distribution_rows)}</tbody>
      </table>
    </section>
    """
    eligible_context_rows = "".join(
        f"<tr><td>{escape(repo)}</td><td>{escape(context)}</td></tr>"
        for repo, context in sorted(eligible_contexts)
    )
    companion_items = "".join(
        f"<li><code>{escape(repo)}</code></li>" for repo in LAUNCHPLANE_ALLOWED_COMPANION_REPOS
    )
    preview_label_example = escape("<context>/<anchor-repo>/pr-<number>")
    preview_route_example = escape("/previews/<context>/<anchor-repo>/pr-<number>")

    body_html = f"""
    <section class=\"policy-mast\">
      <div class=\"section-label\">Read-only policy</div>
      <h2>How Launchplane decides what becomes a preview</h2>
      <p>This page exposes the current preview contract as operator evidence. GitHub supplies PR events and identity; Launchplane decides eligibility, route shape, baseline input defaults, and preview retention behavior.</p>
    </section>

    <section class=\"policy-grid\">
      <article class=\"policy-card\">
        <div class=\"section-label\">Current queue</div>
        <h3>Observed state</h3>
        <dl class=\"policy-stats\">
          <div><dt>Context</dt><dd>{escape(context_name) or "all contexts"}</dd></div>
          <div><dt>Active previews</dt><dd>{active_preview_count}</dd></div>
          <div><dt>Retained evidence</dt><dd>{retained_preview_count}</dd></div>
          <div><dt>Total records</dt><dd>{len(preview_rows)}</dd></div>
        </dl>
      </article>
      <article class=\"policy-card\">
        <div class=\"section-label\">Enablement</div>
        <h3>Preview request gate</h3>
        <p>Launchplane can enable a PR preview from the anchor PR label <code>{escape(LAUNCHPLANE_PREVIEW_ENABLE_LABEL)}</code> or from an explicit Launchplane-side request. Once requested, manifest-changing PR events can refresh the same preview identity.</p>
      </article>
    </section>

    {context_distribution_html}

    <section class=\"policy-section\">
      <div class=\"section-label\">Anchor policy</div>
      <h2>Eligible anchor repositories</h2>
      <p>Launchplane only anchors preview identities from tenant repositories that resolve to a known control-plane context.</p>
      <table>
        <thead><tr><th>Anchor repo</th><th>Context</th></tr></thead>
        <tbody>{eligible_context_rows}</tbody>
      </table>
    </section>

    <section class=\"policy-grid\">
      <article class=\"policy-card\">
        <div class=\"section-label\">Preview metadata</div>
        <h3>PR body contract</h3>
        <p>Launchplane reads one fenced metadata block from the anchor PR body using info string <code>{escape(LAUNCHPLANE_PREVIEW_REQUEST_BLOCK_INFO_STRING)}</code>. The default baseline channel is <code>{escape(DEFAULT_LAUNCHPLANE_BASELINE_CHANNEL)}</code>.</p>
      </article>
      <article class=\"policy-card\">
        <div class=\"section-label\">Companions</div>
        <h3>Allowlisted companion repos</h3>
        <p>Companion refs are explicit, PR-based, and allowlisted. Launchplane does not accept raw branch-name or SHA overrides here.</p>
        <ul class=\"policy-list\">{companion_items or "<li>None</li>"}</ul>
      </article>
    </section>

    <section class=\"policy-grid\">
      <article class=\"policy-card\">
        <div class=\"section-label\">Identity</div>
        <h3>Preview naming</h3>
        <p>Human-readable preview labels follow <code>{preview_label_example}</code>. Launchplane keeps one stable preview identity per anchor PR and rotates generations behind it.</p>
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
        <li>Launchplane treats preview records and generation records as canonical control-plane evidence, not transient UI state.</li>
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

    return render_launchplane_shell_document(
        page_title=f"Launchplane preview policy{' · ' + context_name if context_name else ''}",
        context_name=context_name,
        active_nav="policy",
        body_class="index-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def render_launchplane_promotion_status_page_html(
    payload: dict[str, object],
    *,
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", "")).strip()
    path_label = str(payload.get("path_label", "")).strip() or f"{context_name}/testing-to-prod"
    tone = str(payload.get("tone", "neutral")).strip() or "neutral"
    headline = escape(
        str(payload.get("headline", "Launchplane cannot describe the promotion path yet."))
    )
    summary = escape(str(payload.get("summary", "No promotion summary recorded.")))
    next_action = escape(str(payload.get("next_action", "No next action recorded.")))
    retained_evidence = escape(
        str(payload.get("retained_evidence", "No retained evidence summary recorded."))
    )
    candidate_artifact_id = escape(str(payload.get("candidate_artifact_id", "")) or "Unavailable")
    current_prod_artifact_id = escape(
        str(payload.get("current_prod_artifact_id", "")) or "Unavailable"
    )
    source_git_ref = escape(str(payload.get("source_git_ref", "")) or "Unavailable")
    status_label_value = escape(str(payload.get("status", "unknown")).replace("_", " "))
    evidence_checks = json_object_items(payload.get("evidence_checks"))
    latest_backup_gate = json_object(payload.get("latest_backup_gate"))
    latest_promotion = json_object(payload.get("latest_promotion"))
    recent_backup_gates = json_object_items(payload.get("recent_backup_gates"))
    recent_promotions = json_object_items(payload.get("recent_promotions"))
    testing_live = json_object(payload.get("testing_live"))
    prod_live = json_object(payload.get("prod_live"))

    evidence_cards: list[str] = []
    for check in evidence_checks:
        check_status = str(check.get("status", "pending"))
        check_tone = status_tone(check_status)
        evidence_cards.append(
            f"""
        <article class=\"promotion-detail-check promotion-detail-check-{check_tone}\">
          <div class=\"promotion-detail-check-head\">
            <h4>{escape(str(check.get("label", "Evidence")))}</h4>
            <span class=\"signal-chip signal-{check_tone}\">{escape(status_label(check_status))}</span>
          </div>
          <p>{escape(str(check.get("detail", "No evidence detail recorded.")))}</p>
        </article>
        """
        )
    evidence_html = (
        "".join(evidence_cards)
        or '<p class="table-empty">No promotion evidence checks recorded yet.</p>'
    )

    recipe_cards: list[str] = []
    backup_gate_recipe = str(payload.get("backup_gate_recipe", "")).strip()
    if backup_gate_recipe:
        recipe_cards.append(
            render_launchplane_action_recipe(
                title="Record prod backup gate",
                summary="Persist the exact backup authorization Launchplane expects before trying to promote into prod.",
                tone="warn",
                script=backup_gate_recipe,
                command_label="backup-gates write",
                recipe_id=f"promotion-detail-{escape(context_name)}-backup-gate",
            )
        )
    resolve_recipe = str(payload.get("resolve_recipe", "")).strip()
    if resolve_recipe:
        recipe_cards.append(
            render_launchplane_action_recipe(
                title="Plan promotion request",
                summary="Resolve Launchplane's typed promotion request from the current tenant evidence before execution.",
                tone=tone,
                script=resolve_recipe,
                command_label="promote resolve",
                recipe_id=f"promotion-detail-{escape(context_name)}-resolve",
            )
        )
    execute_recipe = str(payload.get("execute_recipe", "")).strip()
    if execute_recipe:
        recipe_cards.append(
            render_launchplane_action_recipe(
                title="Execute promotion",
                summary="Run the resolved promotion request once the typed payload looks correct.",
                tone=tone,
                script=execute_recipe,
                command_label="promote execute",
                recipe_id=f"promotion-detail-{escape(context_name)}-execute",
            )
        )
    recipe_html = "".join(recipe_cards) or (
        '<p class="table-empty">Launchplane is not exposing a promotion recipe for the current tenant state yet.</p>'
    )

    def render_live_lane_card(title: str, lane_payload: dict[str, object] | None) -> str:
        if lane_payload is None:
            return f"""
            <article class=\"promotion-lane-card promotion-lane-card-empty\">
              <div class=\"section-label\">{escape(title)}</div>
              <h3>No lane evidence</h3>
              <p>Launchplane has not recorded current live inventory for this lane yet.</p>
            </article>
            """
        return f"""
        <article class=\"promotion-lane-card\">
          <div class=\"section-label\">{escape(title)}</div>
          <h3><code>{escape(str(lane_payload.get("artifact_id", "")) or "Unavailable")}</code></h3>
          <p>{escape(str(lane_payload.get("source_git_ref", "")) or "No source ref recorded.")}</p>
          <dl class=\"promotion-lane-meta\">
            <div><dt>Updated</dt><dd>{escape(str(lane_payload.get("updated_at", "")) or "Unavailable")}</dd></div>
            <div><dt>Deploy</dt><dd>{escape(str(lane_payload.get("deploy_status", "")) or "Unavailable")}</dd></div>
            <div><dt>Health</dt><dd>{escape(str(lane_payload.get("destination_health_status", "")) or "Unavailable")}</dd></div>
            <div><dt>Record</dt><dd><code>{escape(str(lane_payload.get("deployment_record_id", "")) or "Unavailable")}</code></dd></div>
          </dl>
        </article>
        """

    recent_promotions_html = '<p class="table-empty">No promotion history recorded yet.</p>'
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

    recent_backup_gates_html = (
        '<p class="table-empty">No prod backup-gate history recorded yet.</p>'
    )
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
        <span class=\"tone-pill tone-{escape(tone)}\">{status_label_value}</span>
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
          <div><dt>Launchplane retains</dt><dd>{retained_evidence}</dd></div>
        </dl>
      </article>
      <div class=\"promotion-lane-grid\">
        {render_live_lane_card("Testing lane", testing_live)}
        {render_live_lane_card("Prod lane", prod_live)}
      </div>
    </section>

    <section class=\"promotion-detail-section\">
      <div class=\"section-label\">Evidence checks</div>
      <h3>What Launchplane is using to gate promotion</h3>
      <div class=\"promotion-detail-check-grid\">{evidence_html}</div>
    </section>

    <section class=\"promotion-detail-section\">
      <div class=\"section-label\">Typed actions</div>
      <h3>What Launchplane can do next</h3>
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

    return render_launchplane_shell_document(
        page_title=f"Launchplane promotion detail · {path_label}",
        context_name=context_name,
        active_nav="detail",
        body_class="detail-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def render_launchplane_environment_status_page_html(
    payload: dict[str, object],
    *,
    action_payload: dict[str, object] | None = None,
    nav_links: dict[str, str] | None = None,
) -> str:
    context_name = str(payload.get("context", "")).strip()
    instance_name = str(payload.get("instance", "")).strip() or "environment"
    live_payload = json_object(payload.get("live")) or {}
    live_promotion = json_object(payload.get("live_promotion"))
    authorized_backup_gate = json_object(payload.get("authorized_backup_gate"))
    latest_promotion = json_object(payload.get("latest_promotion"))
    latest_deployment = json_object(payload.get("latest_deployment"))
    recent_promotions = json_object_items(payload.get("recent_promotions"))
    recent_deployments = json_object_items(payload.get("recent_deployments"))

    lane_title = f"{context_name}/{instance_name}" if context_name else instance_name
    role_summary = (
        "Testing carries the integration artifact Launchplane would promote next."
        if instance_name == "testing"
        else "Prod is the customer-facing lane Launchplane protects and promotes into deliberately."
    )
    live_tone = status_tone(
        str(live_payload.get("destination_health_status", "pending") or "pending")
    )
    deploy_status = str(live_payload.get("deploy_status", "pending") or "pending")
    health_status = str(live_payload.get("destination_health_status", "pending") or "pending")
    action_status = (
        str(action_payload.get("status", "")) if isinstance(action_payload, dict) else ""
    )

    live_promotion_html = ""
    if live_promotion is not None:
        live_promotion_html = f"""
        <article class=\"detail-note\">
          <div class=\"section-label\">Attached promotion</div>
          <h3>Current lane inventory is backed by a promotion record.</h3>
          <dl class=\"detail-meta\">
            <div><dt>Promotion record</dt><dd><code>{escape(str(live_promotion.get("record_id", "")) or "Unavailable")}</code></dd></div>
            <div><dt>Artifact</dt><dd><code>{escape(str(live_promotion.get("artifact_id", "")) or "Unavailable")}</code></dd></div>
            <div><dt>Backup gate</dt><dd><code>{escape(str(live_promotion.get("backup_record_id", "")) or "Unavailable")}</code></dd></div>
            <div><dt>Finished</dt><dd>{escape(str(live_promotion.get("finished_at", "")) or "Unavailable")}</dd></div>
          </dl>
        </article>
        """
    else:
        live_promotion_html = """
        <article class=\"detail-note detail-note-muted\">
          <div class=\"section-label\">Attached promotion</div>
          <h3>No live promotion record is attached to this lane inventory.</h3>
          <p>Launchplane can still show recent promotion history below, but the current environment inventory does not point at one canonical promotion record yet.</p>
        </article>
        """

    backup_gate_html = ""
    if authorized_backup_gate is not None:
        backup_gate_evidence = json_object(authorized_backup_gate.get("evidence")) or {}
        evidence_entries = "".join(
            f"<li><code>{escape(str(key))}</code> {escape(str(value))}</li>"
            for key, value in sorted(backup_gate_evidence.items())
        )
        backup_gate_html = f"""
        <article class=\"detail-note\">
          <div class=\"section-label\">Authorized backup gate</div>
          <h3>Launchplane has a recorded backup gate for this lane.</h3>
          <dl class=\"detail-meta\">
            <div><dt>Record</dt><dd><code>{escape(str(authorized_backup_gate.get("record_id", "")) or "Unavailable")}</code></dd></div>
            <div><dt>Status</dt><dd>{escape(str(authorized_backup_gate.get("status", "unknown")) or "unknown")}</dd></div>
            <div><dt>Source</dt><dd>{escape(str(authorized_backup_gate.get("source", "")) or "Unavailable")}</dd></div>
            <div><dt>Created</dt><dd>{escape(str(authorized_backup_gate.get("created_at", "")) or "Unavailable")}</dd></div>
          </dl>
          <ul class=\"detail-list\">{evidence_entries or "<li>No backup evidence fields recorded.</li>"}</ul>
        </article>
        """
    else:
        backup_gate_html = """
        <article class=\"detail-note detail-note-muted\">
          <div class=\"section-label\">Authorized backup gate</div>
          <h3>No authorized backup gate is attached to this lane yet.</h3>
          <p>This is normal for `testing` and is still a useful warning for `prod` when Launchplane cannot prove the current lane from attached backup evidence alone.</p>
        </article>
        """

    action_html = """
    <article class=\"detail-note detail-note-muted\">
      <div class=\"section-label\">Lane action</div>
      <h3>No typed lane action is available.</h3>
      <p>Launchplane does not have enough environment evidence to build a re-ship recipe for this lane yet.</p>
    </article>
    """
    if isinstance(action_payload, dict):
        if action_status == "actionable":
            action_html = render_launchplane_action_recipe(
                title=str(
                    action_payload.get("headline", f"Re-ship current {instance_name} artifact")
                ),
                summary=str(action_payload.get("summary", "")),
                tone=str(action_payload.get("tone", "neutral")),
                script=str(action_payload.get("recipe", "")),
                command_label="ship resolve -> ship execute",
                recipe_id=f"environment-detail-{launchplane_action_slug(lane_title)}-ship",
            )
        else:
            action_html = f"""
            <article class=\"detail-note detail-note-muted\">
              <div class=\"section-label\">Lane action</div>
              <h3>{escape(str(action_payload.get("headline", "No typed lane action is available.")))}</h3>
              <p>{escape(str(action_payload.get("summary", "Launchplane does not have enough environment evidence to build a re-ship recipe for this lane yet.")))}</p>
            </article>
            """

    def render_activity_table(rows: list[dict[str, object]], *, table_kind: str) -> str:
        if table_kind == "deployments":
            if not rows:
                return (
                    '<p class="table-empty">No deployment history recorded for this lane yet.</p>'
                )
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
            return '<p class="table-empty">No promotion history recorded into this lane yet.</p>'
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
    latest_promotion_summary = "Launchplane has not recorded a recent promotion into this lane yet."
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
        <span class=\"tone-pill tone-{live_tone}\">Deploy {status_label(deploy_status)}</span>
        <span class=\"tone-pill tone-{status_tone(health_status)}\">Health {status_label(health_status)}</span>
        <p>{latest_deployment_summary}</p>
      </aside>
    </section>

    <section class=\"environment-detail-grid\">
      <article class=\"detail-card detail-card-primary\">
        <div class=\"section-label\">Live lane snapshot</div>
        <h3>Current environment evidence</h3>
        <dl class=\"detail-meta\">
          <div><dt>Artifact</dt><dd><code>{escape(str(live_payload.get("artifact_id", "")) or "Unavailable")}</code></dd></div>
          <div><dt>Source ref</dt><dd><code>{escape(str(live_payload.get("source_git_ref", "")) or "Unavailable")}</code></dd></div>
          <div><dt>Updated</dt><dd>{escape(str(live_payload.get("updated_at", "")) or "Unavailable")}</dd></div>
          <div><dt>Deploy record</dt><dd><code>{escape(str(live_payload.get("deployment_record_id", "")) or "Unavailable")}</code></dd></div>
          <div><dt>Deploy status</dt><dd>{escape(status_label(deploy_status))}</dd></div>
          <div><dt>Health status</dt><dd>{escape(status_label(health_status))}</dd></div>
          <div><dt>Promoted from</dt><dd>{escape(str(live_payload.get("promoted_from_instance", "")) or "Unavailable")}</dd></div>
          <div><dt>Promotion record</dt><dd><code>{escape(str(live_payload.get("promotion_record_id", "")) or "Unavailable")}</code></dd></div>
        </dl>
      </article>
      <article class=\"detail-card\">
        <div class=\"section-label\">Recent changes</div>
        <h3>What Launchplane saw last</h3>
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
      {render_activity_table([row for row in recent_deployments if isinstance(row, dict)], table_kind="deployments")}
    </section>

    <section class=\"environment-history\">
      <div class=\"section-label\">Promotion history</div>
      <h3>Recent promotions into this lane</h3>
      {render_activity_table([row for row in recent_promotions if isinstance(row, dict)], table_kind="promotions")}
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

    return render_launchplane_shell_document(
        page_title=f"Launchplane environment detail · {lane_title}",
        context_name=context_name,
        active_nav="detail",
        body_class="detail-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )


def render_launchplane_preview_status_page_html(
    payload: dict[str, object],
    *,
    nav_links: dict[str, str] | None = None,
) -> str:
    preview = json_object(payload.get("preview")) or {}
    trust_summary = json_object(payload.get("trust_summary")) or {}
    health_summary = json_object(payload.get("health_summary")) or {}
    input_summary = json_object(payload.get("input_summary")) or {}
    lifecycle_summary = json_object(payload.get("lifecycle_summary")) or {}
    links = json_object(payload.get("links")) or {}
    recent_generations = json_object_items(payload.get("recent_generations"))
    source_map = json_object_items(input_summary.get("source_map"))
    companions = json_object_items(input_summary.get("companions"))
    serving_generation = json_object(payload.get("serving_generation")) or {}
    latest_generation = json_object(payload.get("latest_generation")) or {}

    preview_label = escape(str(preview.get("preview_label", "Launchplane preview")))
    context_name = escape(str(preview.get("context", "")))
    anchor_repo_name = escape(str(preview.get("anchor_repo", "")))
    anchor_pr_number = escape(str(preview.get("anchor_pr_number", "")))
    canonical_url = escape(str(links.get("canonical_url", preview.get("canonical_url", ""))))
    anchor_pr_url = escape(str(links.get("anchor_pr_url", "")))
    preview_state = str(preview.get("state", "unknown"))
    status_summary = escape(
        str(health_summary.get("status_summary", "No Launchplane preview summary available."))
    )
    next_action = escape(str(lifecycle_summary.get("next_action", "")))
    artifact_id = escape(str(trust_summary.get("artifact_id", "")))
    manifest_fingerprint = escape(str(trust_summary.get("manifest_fingerprint", "")))
    destroy_after = escape(str(lifecycle_summary.get("destroy_after", "")))
    active_generation_id = escape(
        str(trust_summary.get("active_generation_id", preview.get("active_generation_id", "")))
    )
    paused_at = escape(str(preview.get("paused_at", "")))
    destroyed_at = escape(
        str(lifecycle_summary.get("destroyed_at", preview.get("destroyed_at", "")))
    )
    destroy_reason = escape(
        str(lifecycle_summary.get("destroy_reason", preview.get("destroy_reason", "")))
    )
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

    banner_label = f"{status_label(preview_state).upper()}"
    banner_note = f"Health {status_label(display_health_status)}"
    banner_tone = status_tone(display_health_status)
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
    elif generation_in_progress(latest_generation_state):
        banner_label = (
            "REPLACEMENT IN FLIGHT" if serving_generation_id else "FIRST GENERATION IN FLIGHT"
        )
        banner_note = (
            "Current preview still serving"
            if serving_generation_id
            else "Launchplane is preparing the first preview"
        )
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
        callout_title = "This preview has already been destroyed. Launchplane is retaining the record as evidence."
        callout_summary = status_summary
        callout_items = [
            ("Destroyed at", destroyed_at or "Unavailable"),
            ("Destroy reason", destroy_reason or "Unavailable"),
            ("Retained generation", f"<code>{latest_generation_id or 'Unavailable'}</code>"),
        ]
        callout_tone = "neutral"
    elif preview_state.strip().lower() == "paused":
        callout_eyebrow = "Paused state"
        callout_title = "This preview is intentionally paused. Launchplane is holding the current review evidence in place."
        callout_summary = status_summary
        callout_items = [
            ("Paused at", paused_at or "Unavailable"),
            (
                "Serving now",
                f"<code>{serving_generation_id or latest_generation_id or 'Unavailable'}</code>",
            ),
            ("Resume behavior", "Blocked until Launchplane resumes the preview."),
        ]
        callout_tone = "warn"
    elif preview_state.strip().lower() == "teardown_pending":
        callout_eyebrow = "Scheduled cleanup"
        callout_title = "This preview is queued for teardown. Launchplane is keeping the current runtime available until cleanup completes."
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
        callout_title = "Launchplane has created this preview record, but the first generation has not been requested yet."
        callout_summary = summary_text
        callout_items = [
            ("Preview route", canonical_url or "Unavailable"),
            ("Generation status", "Not created yet"),
            (
                "What happens next",
                "Launchplane needs the first generation request before this preview becomes live.",
            ),
        ]
        callout_tone = "neutral"
    elif generation_in_progress(latest_generation_state):
        callout_eyebrow = "Replacement in flight"
        callout_title = (
            "A replacement generation is in progress. Launchplane is still serving the current preview."
            if serving_generation_id
            else "The first preview generation is in progress. Launchplane is preparing this preview now."
        )
        callout_summary = (
            summary_text
            or "Launchplane is advancing the latest generation toward a reviewable preview."
        )
        callout_items = [
            ("Current stage", escape(status_label(latest_generation_state)) or "Unavailable"),
            (
                "Serving now",
                f"<code>{serving_generation_id or 'No serving preview yet'}</code>",
            ),
            ("Requested at", latest_requested_at or "Unavailable"),
        ]
        callout_tone = "warn"
    elif no_serving_preview:
        callout_eyebrow = "Availability gap"
        callout_title = (
            "Launchplane has generation evidence for this preview, but nothing is serving yet."
        )
        callout_summary = summary_text
        callout_items = [
            ("Latest generation", f"<code>{latest_generation_id or 'Unavailable'}</code>"),
            ("Current state", escape(status_label(latest_generation_state)) or "Unavailable"),
            ("Requested at", latest_requested_at or "Unavailable"),
        ]
        callout_tone = "bad"
    elif replacement_failed:
        callout_eyebrow = "Replacement status"
        callout_title = "Latest replacement failed. Launchplane is still serving the older preview."
        callout_summary = status_summary
        callout_items = [
            ("Serving now", f"<code>{serving_generation_id or 'Unavailable'}</code>"),
            ("Failed replacement", f"<code>{latest_generation_id or 'Unavailable'}</code>"),
            ("Failure stage", latest_failure_stage or "Unavailable"),
        ]
        callout_detail = (
            latest_failure_summary
            or "Launchplane recorded a failed replacement without an additional summary."
        )
        callout_tone = "bad"
    elif healthy_live_preview:
        callout_eyebrow = "Review is live"
        callout_title = "This preview is live at the stable Launchplane route and serving the latest requested generation."
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
    callout_detail_html = (
        f'<p class="callout-detail">{callout_detail}</p>' if callout_detail else ""
    )
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
        state_label = escape(status_label(generation_state_value)) or "Unavailable"
        requested_at_value = escape(str(item.get("requested_at", ""))) or "Unavailable"
        role_parts: list[str] = []
        if generation_id_value and generation_id_value == serving_generation_id:
            role_parts.append("serving")
        if generation_id_value and generation_id_value == latest_generation_id:
            role_parts.append("latest")
        if generation_id_value and generation_id_value == active_generation_id:
            role_parts.append("active")
        role_label = escape(" / ".join(role_parts) if role_parts else "historical")
        serving_marker = (
            "&bull; "
            if generation_id_value and generation_id_value == serving_generation_id
            else ""
        )
        state_class = f"state-{status_tone(generation_state_value)}"
        generation_rows.append(
            "<tr>"
            f'<td><code title="{generation_id_value}">{serving_marker}{generation_id_value or "Unavailable"}</code></td>'
            f"<td>{role_label}</td>"
            f'<td class="{state_class}">{state_label}</td>'
            f"<td>{requested_at_value}</td>"
            "</tr>"
        )
        failure_stage_value = escape(str(item.get("failure_stage", "")))
        if generation_state_value.strip().lower() == "failed" and failure_stage_value:
            generation_rows.append(
                '<tr class="row-note">'
                "<td></td>"
                f'<td colspan="3">Failure stage {failure_stage_value}.</td>'
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
            f'<div class="route-item"><span>Stable route</span><a href="{canonical_url}">{canonical_url}</a></div>'
        )
    if anchor_pr_url:
        route_line_items.append(
            f'<div class="route-item"><span>Anchor PR</span><a href="{anchor_pr_url}">{anchor_pr_url}</a></div>'
        )
    route_line = (
        "".join(route_line_items) or '<div class="route-item">No preview route recorded.</div>'
    )
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

    action_slug = launchplane_action_slug(preview_label)
    raw_anchor_head_sha = str(
        (json_object(input_summary.get("anchor")) or {}).get("head_sha", "")
    ).strip()
    raw_baseline_release_tuple_id = str(input_summary.get("baseline_release_tuple_id", "")).strip()
    raw_source_map = source_map
    raw_companions = companions
    raw_context_name = str(preview.get("context", "")).strip()
    raw_anchor_repo = str(preview.get("anchor_repo", "")).strip()
    raw_anchor_pr_number = int_from_json_value(preview.get("anchor_pr_number", 0))
    raw_anchor_pr_url = str(preview.get("anchor_pr_url", links.get("anchor_pr_url", ""))).strip()
    raw_latest_generation_id = str(latest_generation.get("generation_id", "")).strip()
    raw_latest_requested_reason = str(latest_generation.get("requested_reason", "")).strip()
    raw_latest_requested_at = str(latest_generation.get("requested_at", "")).strip()
    raw_latest_artifact_id = str(latest_generation.get("artifact_id", "")).strip()
    raw_latest_manifest_fingerprint = str(
        latest_generation.get("resolved_manifest_fingerprint", "")
    ).strip()
    operator_actions: list[str] = []
    if preview_state.strip().lower() != "destroyed":
        destroy_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": int_from_json_value(preview.get("anchor_pr_number", 0)),
            "destroyed_at": "<utc-timestamp>",
            "destroy_reason": "operator_requested",
        }
        destroy_script = build_launchplane_action_script(
            command_name="destroy-preview",
            file_payloads=(
                (
                    "ACTION_FILE",
                    f"/tmp/launchplane-{action_slug}-destroy-preview.json",
                    destroy_payload,
                ),
            ),
            command_args=("--input-file", '"$ACTION_FILE"'),
        )
        operator_actions.append(
            render_launchplane_action_recipe(
                title="Destroy preview",
                summary="Tear down this preview explicitly while retaining Launchplane evidence for the record.",
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
            "anchor_pr_number": raw_anchor_pr_number,
            "anchor_pr_url": raw_anchor_pr_url,
            "state": preview_state.strip().lower() or "pending",
            "updated_at": "<utc-timestamp>",
        }
        generation_request_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": raw_anchor_pr_number,
            "anchor_pr_url": raw_anchor_pr_url,
            "anchor_head_sha": raw_anchor_head_sha or "<anchor-head-sha>",
            "state": "resolving",
            "requested_reason": "operator_requested_refresh",
            "requested_at": "<utc-timestamp>",
            "resolved_manifest_fingerprint": raw_latest_manifest_fingerprint
            or "<manifest-fingerprint>",
            "artifact_id": raw_latest_artifact_id,
            "baseline_release_tuple_id": raw_baseline_release_tuple_id,
            "source_map": raw_source_map,
            "companion_summaries": raw_companions,
            "deploy_status": "pending",
            "verify_status": "pending",
            "overall_health_status": "pending",
        }
        request_script = build_launchplane_action_script(
            command_name="request-generation",
            file_payloads=(
                (
                    "PREVIEW_FILE",
                    f"/tmp/launchplane-{action_slug}-preview.json",
                    request_generation_payload,
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
        operator_actions.insert(
            0,
            render_launchplane_action_recipe(
                title="Request replacement generation",
                summary="Queue a fresh Launchplane generation for this preview using the current record as the starting template.",
                tone="warn",
                script=request_script,
                command_label="request-generation",
                recipe_id=f"action-{action_slug}-request-generation",
            ),
        )
    if raw_latest_generation_id and generation_in_progress(latest_generation_state):
        ready_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": raw_anchor_pr_number,
            "anchor_pr_url": raw_anchor_pr_url,
            "anchor_head_sha": raw_anchor_head_sha or "<anchor-head-sha>",
            "generation_id": raw_latest_generation_id,
            "state": "ready",
            "requested_reason": raw_latest_requested_reason or "operator_requested_refresh",
            "requested_at": raw_latest_requested_at or "<requested-at>",
            "ready_at": "<utc-timestamp>",
            "finished_at": "<utc-timestamp>",
            "resolved_manifest_fingerprint": raw_latest_manifest_fingerprint
            or "<manifest-fingerprint>",
            "artifact_id": raw_latest_artifact_id or "<artifact-id>",
            "baseline_release_tuple_id": raw_baseline_release_tuple_id,
            "source_map": raw_source_map,
            "companion_summaries": raw_companions,
            "deploy_status": "pass",
            "verify_status": "pass",
            "overall_health_status": "pass",
        }
        ready_script = build_launchplane_action_script(
            command_name="mark-generation-ready",
            file_payloads=(
                ("ACTION_FILE", f"/tmp/launchplane-{action_slug}-mark-ready.json", ready_payload),
            ),
            command_args=("--input-file", '"$ACTION_FILE"'),
        )
        failed_payload = {
            "schema_version": 1,
            "context": raw_context_name,
            "anchor_repo": raw_anchor_repo,
            "anchor_pr_number": raw_anchor_pr_number,
            "anchor_pr_url": raw_anchor_pr_url,
            "anchor_head_sha": raw_anchor_head_sha or "<anchor-head-sha>",
            "generation_id": raw_latest_generation_id,
            "state": "failed",
            "requested_reason": raw_latest_requested_reason or "operator_requested_refresh",
            "requested_at": raw_latest_requested_at or "<requested-at>",
            "failed_at": "<utc-timestamp>",
            "finished_at": "<utc-timestamp>",
            "resolved_manifest_fingerprint": raw_latest_manifest_fingerprint
            or "<manifest-fingerprint>",
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
        failed_script = build_launchplane_action_script(
            command_name="mark-generation-failed",
            file_payloads=(
                ("ACTION_FILE", f"/tmp/launchplane-{action_slug}-mark-failed.json", failed_payload),
            ),
            command_args=("--input-file", '"$ACTION_FILE"'),
        )
        operator_actions.insert(
            0,
            render_launchplane_action_recipe(
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
            render_launchplane_action_recipe(
                title="Mark latest generation ready",
                summary="Advance the current in-flight generation into Launchplane's ready/serving path once deploy and verify evidence are complete.",
                tone="good",
                script=ready_script,
                command_label="mark-generation-ready",
                recipe_id=f"action-{action_slug}-mark-ready",
            ),
        )
    operator_actions_html = "".join(operator_actions)
    if not operator_actions_html:
        operator_actions_html = '<p class="action-empty">No write-side recipe is exposed for this retained preview state.</p>'
    operator_actions_section_html = f"""
    <section class=\"preview-detail-section\" id=\"operator-actions\">
      <div class=\"section-label\">Operator actions</div>
      <h2>Write-side Launchplane recipes</h2>
      <p>Launchplane still renders as a static operator surface here, so each action is shown as the exact shell recipe for this preview identity.</p>
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
      <p>Launchplane keeps the exact repo-to-SHA map visible so reviewers can answer what code is running here without hidden branch assumptions.</p>
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

    return render_launchplane_shell_document(
        page_title=f"{preview_label} · Launchplane status",
        context_name=str(preview.get("context", "")),
        active_nav="detail",
        body_class="detail-layout",
        body_html=body_html,
        extra_css=extra_css,
        nav_links=nav_links,
    )
