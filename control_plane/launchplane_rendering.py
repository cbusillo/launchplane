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
