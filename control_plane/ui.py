from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape


def _string_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _mapping_value(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return value
    return {}


def _status_class(status: str) -> str:
    if status == "pass":
        return "status-pass"
    if status == "fail":
        return "status-fail"
    if status == "pending":
        return "status-pending"
    return "status-skipped"


def _render_status_badge(label: str, status: str) -> str:
    normalized_status = status or "skipped"
    return (
        f'<span class="status-badge {_status_class(normalized_status)}">'
        f"{escape(label)}: {escape(normalized_status)}"
        "</span>"
    )


def _render_value(label: str, value: str) -> str:
    return (
        '<div class="meta-item">'
        f'<span class="meta-label">{escape(label)}</span>'
        f'<span class="meta-value">{escape(value or "-")}</span>'
        "</div>"
    )


def _overall_status(live_payload: Mapping[str, object]) -> str:
    for key in ("destination_health_status", "post_deploy_update_status", "deploy_status"):
        status = _string_value(live_payload.get(key))
        if status and status != "skipped":
            return status
    return "skipped"


def _render_environment_card(payload: Mapping[str, object]) -> str:
    live_payload = _mapping_value(payload, "live")
    live_promotion_payload = _mapping_value(payload, "live_promotion")
    backup_gate_payload = _mapping_value(payload, "authorized_backup_gate")
    latest_promotion_payload = _mapping_value(payload, "latest_promotion")
    latest_deployment_payload = _mapping_value(payload, "latest_deployment")

    context_name = _string_value(payload.get("context"))
    instance_name = _string_value(payload.get("instance"))
    overall_status = _overall_status(live_payload)
    promoted_from_instance = _string_value(live_payload.get("promoted_from_instance"))

    return "".join(
        (
            f'<article class="environment-card" data-context="{escape(context_name)}" '
            f'data-instance="{escape(instance_name)}" data-artifact="{escape(_string_value(live_payload.get("artifact_id")))}" '
            f'data-source-ref="{escape(_string_value(live_payload.get("source_git_ref")))}">',
            '<div class="card-header">',
            '<div>',
            f'<p class="eyebrow">{escape(context_name)}</p>',
            f'<h2>{escape(instance_name)}</h2>',
            '</div>',
            _render_status_badge("overall", overall_status),
            '</div>',
            '<div class="badge-row">',
            _render_status_badge("deploy", _string_value(live_payload.get("deploy_status"))),
            _render_status_badge("update", _string_value(live_payload.get("post_deploy_update_status"))),
            _render_status_badge("health", _string_value(live_payload.get("destination_health_status"))),
            _render_status_badge("backup", _string_value(backup_gate_payload.get("status"))),
            '</div>',
            '<div class="meta-grid">',
            _render_value("artifact", _string_value(live_payload.get("artifact_id"))),
            _render_value("source ref", _string_value(live_payload.get("source_git_ref"))),
            _render_value("updated", _string_value(live_payload.get("updated_at"))),
            _render_value("promoted from", promoted_from_instance or "direct ship"),
            '</div>',
            '<div class="detail-grid">',
            '<section class="detail-panel">',
            '<h3>Live record</h3>',
            _render_value("deployment record", _string_value(live_payload.get("deployment_record_id"))),
            _render_value("promotion record", _string_value(live_payload.get("promotion_record_id"))),
            '</section>',
            '<section class="detail-panel">',
            '<h3>Latest deployment</h3>',
            _render_value("record", _string_value(latest_deployment_payload.get("record_id"))),
            _render_value("deployment", _string_value(latest_deployment_payload.get("deployment_id"))),
            '</section>',
            '<section class="detail-panel">',
            '<h3>Latest promotion</h3>',
            _render_value("record", _string_value(latest_promotion_payload.get("record_id"))),
            _render_value("backup", _string_value(live_promotion_payload.get("backup_record_id"))),
            '</section>',
            '</div>',
            '</article>',
        )
    )


def _render_detail_section(title: str, payload: Mapping[str, object], fields: Sequence[tuple[str, str]]) -> str:
    values_markup = "".join(_render_value(label, _string_value(payload.get(key))) for label, key in fields)
    return "".join(
        (
            '<section class="detail-panel detail-panel-tall">',
            f"<h3>{escape(title)}</h3>",
            values_markup,
            '</section>',
        )
    )


def _recommended_artifact_id(live_payload: Mapping[str, object], *, fallback: str) -> str:
    artifact_id = _string_value(live_payload.get("artifact_id"))
    if artifact_id:
        return artifact_id
    return fallback


def _derive_next_operator_step(payload: Mapping[str, object]) -> dict[str, object]:
    context_name = _string_value(payload.get("context"))
    instance_name = _string_value(payload.get("instance"))
    live_payload = _mapping_value(payload, "live")
    backup_gate_payload = _mapping_value(payload, "authorized_backup_gate")

    live_promotion_record_id = _string_value(live_payload.get("promotion_record_id"))
    backup_status = _string_value(backup_gate_payload.get("status"))

    checklist = [
        {
            "label": "Deployment passed",
            "status": _string_value(live_payload.get("deploy_status")) or "skipped",
            "detail": "Last control-plane deployment result for this environment.",
        },
        {
            "label": "Health verified",
            "status": _string_value(live_payload.get("destination_health_status")) or "skipped",
            "detail": "Whether the latest known runtime healthcheck passed.",
        },
        {
            "label": "Promotion linked",
            "status": "pass" if live_promotion_record_id else "skipped",
            "detail": "Whether live inventory points at a stored promotion record.",
        },
        {
            "label": "Backup gate authorized",
            "status": backup_status or "skipped",
            "detail": "Whether a stored backup-gate record is linked to the live promotion state.",
        },
    ]

    if instance_name == "prod":
        if backup_status == "pass" and live_promotion_record_id:
            return {
                "tone": "pass",
                "title": "Production is promotion-managed",
                "summary": (
                    "The current live state is already tied back to a stored promotion and backup gate. "
                    "For the next rollout, capture a fresh prod backup-gate record and promote a candidate "
                    "artifact instead of direct shipping."
                ),
                "commands": (
                    f"uv run control-plane backup-gates list --context {context_name} --instance prod",
                    (
                        "uv run control-plane promote resolve "
                        f"--context {context_name} --from-instance testing --to-instance prod "
                        "--artifact-id <candidate-artifact-id> --backup-record-id <fresh-backup-record-id> "
                        "> tmp/promotion-request.json"
                    ),
                    "uv run control-plane promote execute --input-file tmp/promotion-request.json",
                ),
                "checklist": checklist,
            }
        return {
            "tone": "pending",
            "title": "Production promotion needs stronger control-plane linkage",
            "summary": (
                "This environment is not fully described by a live promotion plus authorized backup gate. "
                "Before the next rollout, restore the promotion path: capture a fresh backup gate and use "
                "promote resolve/execute instead of direct shipping."
            ),
            "commands": (
                f"uv run control-plane inventory status --context {context_name} --instance prod",
                f"uv run control-plane backup-gates list --context {context_name} --instance prod",
                (
                    "uv run control-plane promote resolve "
                    f"--context {context_name} --from-instance testing --to-instance prod "
                    "--artifact-id <candidate-artifact-id> --backup-record-id <fresh-backup-record-id> "
                    "> tmp/promotion-request.json"
                ),
            ),
            "checklist": checklist,
        }

    if instance_name == "testing":
        return {
            "tone": "pass",
            "title": "Testing is the likely promotion candidate",
            "summary": (
                "Use this environment to validate the candidate artifact, then create or confirm a fresh prod "
                "backup gate before promoting it onward. The recommended commands assume the standard testing to prod path."
            ),
            "commands": (
                f"uv run control-plane ui environment-status --context {context_name} --instance prod",
                f"uv run control-plane backup-gates list --context {context_name} --instance prod",
                (
                    "uv run control-plane promote resolve "
                    f"--context {context_name} --from-instance testing --to-instance prod "
                    f"--artifact-id {_recommended_artifact_id(live_payload, fallback='<candidate-artifact-id>')} "
                    "--backup-record-id <fresh-backup-record-id> > tmp/promotion-request.json"
                ),
            ),
            "checklist": checklist,
        }

    return {
        "tone": "skipped",
        "title": "Review environment state before taking the next action",
        "summary": (
            "This environment is outside the default testing to prod promotion path. Review the linked deploy, promotion, "
            "and backup records here first, then choose the next control-plane command explicitly."
        ),
        "commands": (
            f"uv run control-plane inventory status --context {context_name} --instance {instance_name}",
            f"uv run control-plane inventory overview --context {context_name}",
        ),
        "checklist": checklist,
    }


def _render_checklist_item(item: Mapping[str, object]) -> str:
    status = _string_value(item.get("status")) or "skipped"
    label = _string_value(item.get("label"))
    detail = _string_value(item.get("detail"))
    return (
        f'<div class="check-item {_status_class(status)}">'
        f'<span class="check-label">{escape(label)}</span>'
        f'<span class="check-status">{escape(status)}</span>'
        f'<p class="check-detail">{escape(detail)}</p>'
        '</div>'
    )


def _render_command_list(commands: Sequence[str]) -> str:
    rendered_commands = "".join(
        f'<pre class="command-snippet"><code>{escape(command)}</code></pre>' for command in commands
    )
    return f'<div class="command-list">{rendered_commands}</div>'


def _render_evidence_pairs(evidence: object) -> str:
    if not isinstance(evidence, Mapping) or not evidence:
        return _render_value("evidence", "(none)")
    return "".join(
        _render_value(_string_value(key), _string_value(value)) for key, value in evidence.items()
    )


def _is_sensitive_environment_key(key_name: str) -> bool:
    normalized_key = key_name.upper()
    if normalized_key in {"GITHUB_TOKEN", "ODOO_KEY"}:
        return True
    for fragment in (
        "PASSWORD",
        "TOKEN",
        "SECRET",
        "WEBHOOK_KEY",
        "MASTER_PASSWORD",
        "PRIVATE_KEY",
        "ACCESS_KEY",
        "AUTH_KEY",
    ):
        if fragment in normalized_key:
            return True
    return False


def _redact_environment_value(key_name: str, value: str) -> tuple[str, bool]:
    if value == "":
        return "(empty)", False
    if not _is_sensitive_environment_key(key_name):
        return value, False
    if len(value) <= 4:
        return "[redacted]", True
    return f"[redacted ending {value[-4:]}]", True


def _render_environment_row(row: Mapping[str, object], *, row_class: str) -> str:
    key_name = _string_value(row.get("key"))
    source = _string_value(row.get("source"))
    overrides = row.get("overrides", ())
    if not isinstance(overrides, Sequence):
        overrides = ()
    display_value, was_redacted = _redact_environment_value(key_name, _string_value(row.get("value")))
    override_note = ""
    if overrides:
        prior_sources = ", ".join(_string_value(item) for item in overrides if _string_value(item))
        if prior_sources:
            override_note = f'<span class="source-note">Overrides {escape(prior_sources)}</span>'
    sensitivity_badge = (
        '<span class="value-badge value-badge-sensitive">redacted</span>' if was_redacted else ""
    )
    return "".join(
        (
            f'<tr class="{escape(row_class)}">',
            f'<td class="env-key">{escape(key_name)}</td>',
            '<td class="env-value-cell">',
            f'<code class="env-value">{escape(display_value)}</code>',
            sensitivity_badge,
            '</td>',
            '<td class="env-source-cell">',
            f'<span class="source-badge source-{escape(source)}">{escape(source)}</span>',
            override_note,
            '</td>',
            '</tr>',
        )
    )


def _render_environment_rows(rows: Sequence[Mapping[str, object]], *, row_class: str) -> str:
    if not rows:
        return (
            '<div class="empty-state compact-empty">'
            '<h3>No values in this layer</h3>'
            '<p>This section does not contribute any environment keys.</p>'
            '</div>'
        )
    table_rows = "".join(_render_environment_row(row, row_class=row_class) for row in rows)
    return "".join(
        (
            '<div class="table-shell">',
            '<table class="env-table">',
            '<thead><tr><th>Key</th><th>Value</th><th>Source</th></tr></thead>',
            f'<tbody>{table_rows}</tbody>',
            '</table>',
            '</div>',
        )
    )


def render_environment_contract_dashboard(payload: Mapping[str, object]) -> str:
    context_name = _string_value(payload.get("context"))
    instance_name = _string_value(payload.get("instance"))
    source_file = _string_value(payload.get("source_file"))
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")

    available_contexts = payload.get("available_contexts", ())
    if not isinstance(available_contexts, Sequence):
        available_contexts = ()
    available_instances = payload.get("available_instances", ())
    if not isinstance(available_instances, Sequence):
        available_instances = ()

    layer_summaries = payload.get("layer_summaries", ())
    if not isinstance(layer_summaries, Sequence):
        layer_summaries = ()

    resolved_rows = payload.get("resolved_rows", ())
    if not isinstance(resolved_rows, Sequence):
        resolved_rows = ()
    global_rows = payload.get("global_rows", ())
    if not isinstance(global_rows, Sequence):
        global_rows = ()
    context_rows = payload.get("context_rows", ())
    if not isinstance(context_rows, Sequence):
        context_rows = ()
    instance_rows = payload.get("instance_rows", ())
    if not isinstance(instance_rows, Sequence):
        instance_rows = ()

    layer_summary_markup = "".join(
        "".join(
            (
                '<article class="summary-tile">',
                f'<span class="eyebrow">{escape(_string_value(layer.get("label")))}</span>',
                f'<span class="summary-value">{escape(_string_value(layer.get("count")))}</span>',
                f'<p class="tile-note">{escape(_string_value(layer.get("note")))}</p>',
                '</article>',
            )
        )
        for layer in layer_summaries
        if isinstance(layer, Mapping)
    )

    context_markup = "".join(
        "".join(
            (
                '<article class="context-chip">',
                f'<span class="context-name">{escape(_string_value(context_payload.get("context")))}</span>',
                f'<span class="context-meta">{escape(_string_value(context_payload.get("instance_count")))} instances</span>',
                '</article>',
            )
        )
        for context_payload in available_contexts
        if isinstance(context_payload, Mapping)
    )

    instance_markup = "".join(
        (
            f'<span class="instance-chip{" instance-chip-active" if _string_value(instance) == instance_name else ""}">'
            f'{escape(_string_value(instance))}'
            '</span>'
        )
        for instance in available_instances
    )

    resolved_count = len(resolved_rows)

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Control Plane Environment Contract</title>
    <style>
      :root {{
        --canvas: #f3ecdf;
        --ink: #1b1612;
        --muted: #5b5248;
        --line: rgba(27, 22, 18, 0.12);
        --panel: rgba(255, 251, 245, 0.92);
        --panel-strong: rgba(249, 242, 231, 0.96);
        --shadow: 0 22px 54px rgba(27, 22, 18, 0.14);
        --accent: #0f766e;
        --accent-soft: rgba(15, 118, 110, 0.14);
        --global: #7c3aed;
        --context: #0f766e;
        --instance: #b45309;
        --resolved: #1d4ed8;
        --sensitive: #991b1b;
      }}

      * {{ box-sizing: border-box; }}

      body {{
        margin: 0;
        min-height: 100vh;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 26%),
          radial-gradient(circle at top right, rgba(124, 58, 237, 0.12), transparent 24%),
          linear-gradient(180deg, #f0e6d7 0%, var(--canvas) 56%, #ece4d8 100%);
        font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      }}

      main {{
        width: min(1280px, calc(100vw - 32px));
        margin: 0 auto;
        padding: 32px 0 56px;
      }}

      .hero, .panel {{
        border: 1px solid var(--line);
        border-radius: 28px;
        background: var(--panel);
        box-shadow: var(--shadow);
      }}

      .hero {{
        padding: 30px;
        background: linear-gradient(135deg, rgba(255, 251, 245, 0.96), rgba(244, 236, 224, 0.86));
      }}

      .eyebrow {{
        margin: 0 0 8px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
        font-size: 0.74rem;
      }}

      h1, h2, h3 {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        font-weight: 700;
      }}

      h1 {{
        font-size: clamp(2.2rem, 5vw, 4.3rem);
        line-height: 0.96;
        max-width: 12ch;
      }}

      .hero-grid {{
        display: grid;
        grid-template-columns: minmax(0, 1.3fr) minmax(260px, 0.7fr);
        gap: 24px;
        align-items: end;
      }}

      .hero-copy, .panel-copy, .tile-note, .source-note, .meta-note {{
        color: var(--muted);
        line-height: 1.6;
      }}

      .hero-copy {{
        margin-top: 16px;
        max-width: 70ch;
      }}

      .hero-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 18px;
        color: var(--muted);
        font-size: 0.94rem;
      }}

      .summary-grid, .panel-grid {{
        display: grid;
        gap: 16px;
      }}

      .summary-grid {{
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        margin-top: 28px;
      }}

      .summary-tile {{
        padding: 18px;
        border-radius: 22px;
        background: var(--panel-strong);
        border: 1px solid var(--line);
      }}

      .summary-value {{
        display: block;
        margin-top: 8px;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 2rem;
      }}

      .surface {{
        display: grid;
        grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
        gap: 18px;
        margin-top: 24px;
      }}

      .column, .panel-stack {{
        display: grid;
        gap: 18px;
      }}

      .panel {{
        padding: 24px;
      }}

      .panel-header {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: end;
        justify-content: space-between;
        margin-bottom: 18px;
      }}

      .search-box {{
        display: flex;
        align-items: center;
        gap: 12px;
        width: min(380px, 100%);
        padding: 14px 16px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255, 251, 245, 0.9);
      }}

      .search-box input {{
        width: 100%;
        border: 0;
        background: transparent;
        color: var(--ink);
        font: inherit;
        outline: none;
      }}

      .context-chip-row, .instance-chip-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 12px;
      }}

      .context-chip, .instance-chip {{
        display: inline-flex;
        flex-direction: column;
        gap: 4px;
        padding: 12px 14px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: rgba(249, 242, 231, 0.92);
      }}

      .instance-chip {{
        flex-direction: row;
        align-items: center;
        padding: 10px 12px;
      }}

      .instance-chip-active {{
        background: var(--accent-soft);
        border-color: rgba(15, 118, 110, 0.22);
      }}

      .context-name {{
        font-weight: 700;
      }}

      .context-meta {{
        color: var(--muted);
        font-size: 0.86rem;
      }}

      .table-shell {{
        overflow-x: auto;
        border-radius: 20px;
        border: 1px solid var(--line);
        background: rgba(255, 252, 247, 0.82);
      }}

      .env-table {{
        width: 100%;
        border-collapse: collapse;
      }}

      .env-table th,
      .env-table td {{
        padding: 14px 16px;
        text-align: left;
        vertical-align: top;
        border-bottom: 1px solid var(--line);
      }}

      .env-table th {{
        position: sticky;
        top: 0;
        background: rgba(245, 236, 223, 0.96);
        font-size: 0.76rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--muted);
      }}

      .env-key {{
        min-width: 280px;
        font-weight: 700;
        word-break: break-word;
      }}

      .env-value {{
        display: inline-block;
        padding: 3px 6px;
        border-radius: 8px;
        background: rgba(27, 22, 18, 0.05);
        font-family: "SFMono-Regular", "Menlo", monospace;
        white-space: pre-wrap;
        word-break: break-word;
      }}

      .env-value-cell {{
        min-width: 320px;
      }}

      .value-badge, .source-badge {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 28px;
        padding: 4px 10px;
        border-radius: 999px;
        font-size: 0.76rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }}

      .value-badge {{
        margin-left: 10px;
      }}

      .value-badge-sensitive {{
        color: var(--sensitive);
        background: rgba(153, 27, 27, 0.12);
      }}

      .source-global {{ color: var(--global); background: rgba(124, 58, 237, 0.12); }}
      .source-context {{ color: var(--context); background: rgba(15, 118, 110, 0.12); }}
      .source-instance {{ color: var(--instance); background: rgba(180, 83, 9, 0.12); }}
      .source-resolved {{ color: var(--resolved); background: rgba(29, 78, 216, 0.12); }}

      .source-note {{
        display: block;
        margin-top: 8px;
        font-size: 0.86rem;
      }}

      .compact-empty {{
        margin-top: 0;
        padding: 24px 20px;
      }}

      .hidden {{ display: none; }}

      @media (max-width: 900px) {{
        main {{ width: min(100vw - 20px, 1280px); padding: 20px 0 32px; }}
        .hero {{ padding: 22px; }}
        .hero-grid, .surface {{ grid-template-columns: 1fr; }}
        .panel {{ padding: 18px; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="hero-grid">
          <div>
            <p class="eyebrow">Control Plane Environment Truth</p>
            <h1>Environment contract for {escape(context_name)}/{escape(instance_name)}</h1>
            <p class="hero-copy">
              This page renders the control-plane runtime-environment contract for one context and instance. It shows
              the global, context, and instance layers alongside the final resolved environment that downstream tools
              consume. Sensitive values are redacted here by design; use a trusted terminal command when you truly need
              the raw value.
            </p>
            <div class="hero-meta">
              <span>Generated at {escape(generated_at)}</span>
              <span>Source file: <strong>{escape(source_file)}</strong></span>
              <span>Resolved keys: <strong id="visible-count">{resolved_count}</strong></span>
            </div>
          </div>
          <div class="summary-stack">
            <article class="summary-tile">
              <span class="eyebrow">Selected instance</span>
              <span class="summary-value">{escape(instance_name)}</span>
              <p class="tile-note">Context <strong>{escape(context_name)}</strong> currently exposes {resolved_count} resolved keys.</p>
            </article>
            <article class="summary-tile">
              <span class="eyebrow">Safe raw fallback</span>
              <p class="tile-note">`uv run control-plane environments resolve --context {escape(context_name)} --instance {escape(instance_name)}`</p>
            </article>
          </div>
        </div>
        <div class="summary-grid">
          {layer_summary_markup}
        </div>
      </section>

      <section class="surface">
        <div class="column">
          <article class="panel">
            <div class="panel-header">
              <div>
                <p class="eyebrow">Resolved view</p>
                <h2>Final merged environment</h2>
                <p class="panel-copy">Each row shows the final winning layer for a key. Instance overrides context, and context overrides global shared values.</p>
              </div>
              <label class="search-box" for="env-filter">
                <span>Filter</span>
                <input id="env-filter" type="search" placeholder="Search keys or layer names">
              </label>
            </div>
            {_render_environment_rows(resolved_rows, row_class="env-row")}
          </article>

          <div class="panel-grid">
            <article class="panel">
              <p class="eyebrow">Layer</p>
              <h2>Global shared values</h2>
              <p class="panel-copy">These keys apply to every context unless a more specific layer overrides them.</p>
              {_render_environment_rows(global_rows, row_class="env-layer-row")}
            </article>

            <article class="panel">
              <p class="eyebrow">Layer</p>
              <h2>{escape(context_name)} shared values</h2>
              <p class="panel-copy">These keys apply to every instance inside the selected context.</p>
              {_render_environment_rows(context_rows, row_class="env-layer-row")}
            </article>

            <article class="panel">
              <p class="eyebrow">Layer</p>
              <h2>{escape(instance_name)} instance values</h2>
              <p class="panel-copy">These keys apply only to the selected instance and win last during resolution.</p>
              {_render_environment_rows(instance_rows, row_class="env-layer-row")}
            </article>
          </div>
        </div>

        <div class="column">
          <div class="panel-stack">
            <article class="panel">
              <p class="eyebrow">Navigation</p>
              <h2>Available contexts</h2>
              <p class="panel-copy">The contract file currently defines these tenant contexts.</p>
              <div class="context-chip-row">{context_markup}</div>
            </article>

            <article class="panel">
              <p class="eyebrow">Selection</p>
              <h2>Instances in {escape(context_name)}</h2>
              <p class="panel-copy">Render this page again with a different `--instance` value to inspect another environment contract.</p>
              <div class="instance-chip-row">{instance_markup}</div>
            </article>

            <article class="panel">
              <p class="eyebrow">Safety</p>
              <h2>Secret handling</h2>
              <p class="panel-copy">This UI deliberately redacts values that look like passwords, tokens, secrets, or other sensitive keys. The goal is to make the control-plane contract inspectable without turning a static HTML page into a secret dump.</p>
              <p class="meta-note">If a value is redacted here, use the CLI in a trusted local terminal session when you truly need the raw value.</p>
            </article>
          </div>
        </div>
      </section>
    </main>
    <script>
      const filterInput = document.getElementById("env-filter");
      const rows = Array.from(document.querySelectorAll(".env-row"));
      const visibleCount = document.getElementById("visible-count");

      function syncFilter() {{
        const query = filterInput.value.trim().toLowerCase();
        let visible = 0;
        for (const row of rows) {{
          const haystack = row.innerText.toLowerCase();
          const matches = query === "" || haystack.includes(query);
          row.classList.toggle("hidden", !matches);
          if (matches) {{
            visible += 1;
          }}
        }}
        visibleCount.textContent = String(visible);
      }}

      filterInput.addEventListener("input", syncFilter);
      syncFilter();
    </script>
  </body>
</html>
"""


def render_inventory_overview_dashboard(
    payloads: Sequence[Mapping[str, object]],
    *,
    context_name: str = "",
) -> str:
    total_count = len(payloads)
    pass_count = 0
    fail_count = 0
    pending_count = 0
    skipped_count = 0
    for payload in payloads:
        status = _overall_status(_mapping_value(payload, "live"))
        if status == "pass":
            pass_count += 1
        elif status == "fail":
            fail_count += 1
        elif status == "pending":
            pending_count += 1
        else:
            skipped_count += 1

    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    title_suffix = f" for {context_name}" if context_name else ""
    cards_markup = "".join(_render_environment_card(payload) for payload in payloads)
    empty_markup = ""
    if not payloads:
        empty_markup = (
            '<section class="empty-state">'
            '<h2>No live inventory records</h2>'
            '<p>Write or refresh environment inventory records, then render the dashboard again.</p>'
            '</section>'
        )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Control Plane Inventory Overview</title>
    <style>
      :root {{
        --paper: #f4f0e8;
        --ink: #1e1b18;
        --muted: #5f574f;
        --line: rgba(30, 27, 24, 0.12);
        --panel: rgba(255, 252, 246, 0.88);
        --accent: #0f766e;
        --accent-soft: rgba(15, 118, 110, 0.12);
        --pass: #166534;
        --pass-bg: rgba(22, 101, 52, 0.14);
        --fail: #991b1b;
        --fail-bg: rgba(153, 27, 27, 0.14);
        --pending: #9a6700;
        --pending-bg: rgba(154, 103, 0, 0.15);
        --skipped: #475569;
        --skipped-bg: rgba(71, 85, 105, 0.14);
        --shadow: 0 24px 60px rgba(30, 27, 24, 0.16);
      }}

      * {{ box-sizing: border-box; }}

      body {{
        margin: 0;
        min-height: 100vh;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 28%),
          radial-gradient(circle at top right, rgba(180, 83, 9, 0.14), transparent 24%),
          linear-gradient(180deg, #efe8db 0%, var(--paper) 54%, #ebe3d4 100%);
        font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      }}

      main {{
        width: min(1200px, calc(100vw - 32px));
        margin: 0 auto;
        padding: 48px 0 56px;
      }}

      .hero {{
        padding: 28px;
        border: 1px solid var(--line);
        border-radius: 28px;
        background: linear-gradient(135deg, rgba(255, 252, 246, 0.92), rgba(246, 239, 228, 0.78));
        box-shadow: var(--shadow);
      }}

      .eyebrow {{
        margin: 0 0 8px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
        font-size: 0.74rem;
      }}

      h1, h2, h3 {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        font-weight: 700;
      }}

      h1 {{
        font-size: clamp(2.2rem, 5vw, 4.2rem);
        line-height: 0.95;
        max-width: 12ch;
      }}

      .hero-copy {{
        margin-top: 16px;
        max-width: 62ch;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1.6;
      }}

      .hero-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 18px;
        color: var(--muted);
        font-size: 0.92rem;
      }}

      .toolbar {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        align-items: center;
        justify-content: space-between;
        margin-top: 28px;
      }}

      .search-box {{
        flex: 1 1 280px;
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 14px 16px;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: rgba(255, 252, 246, 0.9);
      }}

      .search-box input {{
        width: 100%;
        border: 0;
        background: transparent;
        color: var(--ink);
        font: inherit;
        outline: none;
      }}

      .summary-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 12px;
        margin-top: 28px;
      }}

      .summary-tile {{
        padding: 18px;
        border-radius: 20px;
        background: var(--panel);
        border: 1px solid var(--line);
      }}

      .summary-value {{
        display: block;
        margin-top: 8px;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 2rem;
      }}

      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        gap: 18px;
        margin-top: 28px;
      }}

      .environment-card {{
        padding: 22px;
        border-radius: 24px;
        background: var(--panel);
        border: 1px solid var(--line);
        box-shadow: 0 16px 36px rgba(30, 27, 24, 0.1);
      }}

      .card-header, .badge-row, .meta-grid, .detail-grid {{
        display: grid;
        gap: 12px;
      }}

      .card-header {{
        grid-template-columns: 1fr auto;
        align-items: start;
      }}

      .badge-row {{
        grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        margin-top: 18px;
      }}

      .status-badge {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 38px;
        padding: 8px 12px;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 700;
        letter-spacing: 0.03em;
      }}

      .status-pass {{ color: var(--pass); background: var(--pass-bg); }}
      .status-fail {{ color: var(--fail); background: var(--fail-bg); }}
      .status-pending {{ color: var(--pending); background: var(--pending-bg); }}
      .status-skipped {{ color: var(--skipped); background: var(--skipped-bg); }}

      .meta-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        margin-top: 18px;
      }}

      .detail-grid {{
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        margin-top: 20px;
      }}

      .detail-panel {{
        padding: 16px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: rgba(250, 245, 236, 0.92);
      }}

      .detail-panel h3 {{
        font-size: 1rem;
        margin-bottom: 12px;
      }}

      .meta-item + .meta-item {{
        margin-top: 10px;
      }}

      .meta-label {{
        display: block;
        font-size: 0.72rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--muted);
      }}

      .meta-value {{
        display: block;
        margin-top: 6px;
        font-size: 0.98rem;
        word-break: break-word;
      }}

      .empty-state {{
        margin-top: 28px;
        padding: 36px 28px;
        border-radius: 24px;
        border: 1px dashed var(--line);
        background: rgba(255, 252, 246, 0.74);
      }}

      .empty-state p {{
        color: var(--muted);
        max-width: 52ch;
      }}

      .hidden {{ display: none; }}

      @media (max-width: 720px) {{
        main {{ width: min(100vw - 20px, 1200px); padding: 20px 0 28px; }}
        .hero {{ padding: 22px; border-radius: 24px; }}
        .meta-grid {{ grid-template-columns: 1fr; }}
        .card-header {{ grid-template-columns: 1fr; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <p class="eyebrow">Control Plane Operator View</p>
        <h1>Inventory overview{escape(title_suffix)}</h1>
        <p class="hero-copy">
          This dashboard is generated from the control-plane inventory read model. It is intended to become the
          operator-facing surface for answering what is live, what changed last, and which promotion or backup
          record authorized the current state.
        </p>
        <div class="hero-meta">
          <span>Generated at {escape(generated_at)}</span>
          <span>Visible environments: <strong id="visible-count">{total_count}</strong></span>
        </div>
        <div class="toolbar">
          <label class="search-box" for="environment-filter">
            <span>Filter</span>
            <input id="environment-filter" type="search" placeholder="Search context, instance, artifact, or source ref">
          </label>
        </div>
        <div class="summary-grid">
          <article class="summary-tile"><span class="eyebrow">Tracked</span><span class="summary-value">{total_count}</span></article>
          <article class="summary-tile"><span class="eyebrow">Healthy</span><span class="summary-value">{pass_count}</span></article>
          <article class="summary-tile"><span class="eyebrow">Pending</span><span class="summary-value">{pending_count}</span></article>
          <article class="summary-tile"><span class="eyebrow">Failed</span><span class="summary-value">{fail_count}</span></article>
          <article class="summary-tile"><span class="eyebrow">Skipped</span><span class="summary-value">{skipped_count}</span></article>
        </div>
      </section>
      {empty_markup}
      <section class="grid" id="environment-grid">
        {cards_markup}
      </section>
    </main>
    <script>
      const filterInput = document.getElementById("environment-filter");
      const cards = Array.from(document.querySelectorAll(".environment-card"));
      const visibleCount = document.getElementById("visible-count");

      function syncFilter() {{
        const query = filterInput.value.trim().toLowerCase();
        let visible = 0;
        for (const card of cards) {{
          const haystack = [
            card.dataset.context,
            card.dataset.instance,
            card.dataset.artifact,
            card.dataset.sourceRef,
          ].join(" ").toLowerCase();
          const matches = query === "" || haystack.includes(query);
          card.classList.toggle("hidden", !matches);
          if (matches) {{
            visible += 1;
          }}
        }}
        visibleCount.textContent = String(visible);
      }}

      filterInput.addEventListener("input", syncFilter);
      syncFilter();
    </script>
  </body>
</html>
"""


def render_environment_status_dashboard(payload: Mapping[str, object]) -> str:
    live_payload = _mapping_value(payload, "live")
    live_promotion_payload = _mapping_value(payload, "live_promotion")
    latest_promotion_payload = _mapping_value(payload, "latest_promotion")
    latest_deployment_payload = _mapping_value(payload, "latest_deployment")
    backup_gate_payload = _mapping_value(payload, "authorized_backup_gate")
    next_step = _derive_next_operator_step(payload)
    checklist = next_step.get("checklist", ())
    if not isinstance(checklist, Sequence):
        checklist = ()
    commands = next_step.get("commands", ())
    if not isinstance(commands, Sequence):
        commands = ()

    context_name = _string_value(payload.get("context"))
    instance_name = _string_value(payload.get("instance"))
    overall_status = _overall_status(live_payload)
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Control Plane Environment Status</title>
    <style>
      :root {{
        --canvas: #f5f1e7;
        --ink: #1c1917;
        --muted: #57534e;
        --line: rgba(28, 25, 23, 0.12);
        --panel: rgba(255, 252, 247, 0.9);
        --panel-strong: rgba(251, 247, 240, 0.96);
        --shadow: 0 22px 48px rgba(28, 25, 23, 0.14);
        --pass: #166534;
        --pass-bg: rgba(22, 101, 52, 0.14);
        --fail: #991b1b;
        --fail-bg: rgba(153, 27, 27, 0.14);
        --pending: #9a6700;
        --pending-bg: rgba(154, 103, 0, 0.15);
        --skipped: #475569;
        --skipped-bg: rgba(71, 85, 105, 0.14);
      }}

      * {{ box-sizing: border-box; }}

      body {{
        margin: 0;
        min-height: 100vh;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(15, 118, 110, 0.12), transparent 26%),
          radial-gradient(circle at bottom right, rgba(180, 83, 9, 0.14), transparent 24%),
          linear-gradient(180deg, #efe6d5 0%, var(--canvas) 52%, #ece5d8 100%);
        font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      }}

      main {{
        width: min(1220px, calc(100vw - 32px));
        margin: 0 auto;
        padding: 32px 0 56px;
      }}

      .hero {{
        padding: 30px;
        border-radius: 30px;
        border: 1px solid var(--line);
        background: linear-gradient(135deg, rgba(255, 251, 244, 0.96), rgba(247, 239, 228, 0.82));
        box-shadow: var(--shadow);
      }}

      .eyebrow {{
        margin: 0 0 10px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
        font-size: 0.74rem;
      }}

      h1, h2, h3 {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        font-weight: 700;
      }}

      h1 {{
        font-size: clamp(2.3rem, 4.8vw, 4.4rem);
        line-height: 0.96;
        max-width: 11ch;
      }}

      .hero-grid {{
        display: grid;
        grid-template-columns: minmax(0, 1.25fr) minmax(260px, 0.75fr);
        gap: 24px;
        align-items: end;
      }}

      .hero-copy {{
        margin-top: 16px;
        max-width: 64ch;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1.65;
      }}

      .hero-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 18px;
        color: var(--muted);
      }}

      .summary-stack {{
        display: grid;
        gap: 12px;
      }}

      .summary-card {{
        padding: 18px;
        border-radius: 22px;
        border: 1px solid var(--line);
        background: var(--panel);
      }}

      .summary-value {{
        display: block;
        margin-top: 8px;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 1.8rem;
      }}

      .status-badge {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 38px;
        padding: 8px 12px;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 700;
        letter-spacing: 0.03em;
      }}

      .status-pass {{ color: var(--pass); background: var(--pass-bg); }}
      .status-fail {{ color: var(--fail); background: var(--fail-bg); }}
      .status-pending {{ color: var(--pending); background: var(--pending-bg); }}
      .status-skipped {{ color: var(--skipped); background: var(--skipped-bg); }}

      .badge-row {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 12px;
        margin-top: 24px;
      }}

      .layout {{
        display: grid;
        grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
        gap: 18px;
        margin-top: 26px;
      }}

      .column {{
        display: grid;
        gap: 18px;
      }}

      .panel {{
        padding: 22px;
        border-radius: 24px;
        border: 1px solid var(--line);
        background: var(--panel-strong);
        box-shadow: 0 16px 36px rgba(28, 25, 23, 0.08);
      }}

      .panel-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 16px;
        margin-top: 18px;
      }}

      .panel-stack {{
        display: grid;
        gap: 18px;
      }}

      .detail-panel-tall {{ min-height: 100%; }}

      .next-step-panel {{
        margin-top: 24px;
        padding: 22px;
        border-radius: 24px;
        border: 1px solid var(--line);
        background: rgba(255, 251, 244, 0.92);
        box-shadow: 0 14px 32px rgba(28, 25, 23, 0.08);
      }}

      .next-step-panel.status-pass {{ border-color: rgba(22, 101, 52, 0.38); }}
      .next-step-panel.status-fail {{ border-color: rgba(153, 27, 27, 0.38); }}
      .next-step-panel.status-pending {{ border-color: rgba(154, 103, 0, 0.38); }}
      .next-step-panel.status-skipped {{ border-color: rgba(71, 85, 105, 0.28); }}

      .next-step-title {{
        margin-top: 12px;
        font-size: 2rem;
      }}

      .next-step-copy {{
        max-width: 72ch;
        margin: 14px 0 0;
        color: var(--muted);
        line-height: 1.6;
      }}

      .check-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
        margin-top: 18px;
      }}

      .check-item {{
        padding: 14px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: rgba(252, 248, 242, 0.92);
      }}

      .check-label,
      .check-status {{
        display: block;
      }}

      .check-label {{
        font-size: 0.8rem;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }}

      .check-status {{
        margin-top: 8px;
        font-family: Georgia, "Times New Roman", serif;
        font-size: 1.35rem;
      }}

      .check-detail {{
        margin: 10px 0 0;
        color: var(--muted);
        line-height: 1.45;
        font-size: 0.92rem;
      }}

      .command-list {{
        display: grid;
        gap: 10px;
        margin-top: 18px;
      }}

      .command-snippet {{
        margin: 0;
        padding: 14px 16px;
        border-radius: 16px;
        border: 1px solid var(--line);
        background: #1f1a16;
        color: #f8efe0;
        overflow-x: auto;
        font-size: 0.88rem;
        line-height: 1.45;
      }}

      .timeline {{
        display: grid;
        gap: 14px;
        margin-top: 18px;
      }}

      .timeline-item {{
        padding: 18px;
        border-radius: 18px;
        border: 1px solid var(--line);
        background: rgba(252, 248, 242, 0.95);
      }}

      .meta-item + .meta-item {{ margin-top: 10px; }}

      .meta-label {{
        display: block;
        font-size: 0.72rem;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--muted);
      }}

      .meta-value {{
        display: block;
        margin-top: 6px;
        font-size: 0.98rem;
        word-break: break-word;
      }}

      @media (max-width: 860px) {{
        main {{ width: min(100vw - 20px, 1220px); padding: 20px 0 28px; }}
        .hero {{ padding: 22px; border-radius: 24px; }}
        .hero-grid, .layout {{ grid-template-columns: 1fr; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="hero-grid">
          <div>
            <p class="eyebrow">Control Plane Environment Status</p>
            <h1>{escape(context_name)} / {escape(instance_name)}</h1>
            <p class="hero-copy">
              This page renders the single-environment control-plane read model: live inventory, latest deployment,
              latest promotion, and the backup record that authorized the current promoted state when one exists.
            </p>
            <div class="hero-meta">
              <span>Generated at {escape(generated_at)}</span>
              <span>Artifact: <strong>{escape(_string_value(live_payload.get("artifact_id")) or "-")}</strong></span>
            </div>
          </div>
          <div class="summary-stack">
            <article class="summary-card">
              <span class="eyebrow">Overall</span>
              <span class="summary-value">{escape(overall_status)}</span>
            </article>
            <article class="summary-card">
              <span class="eyebrow">Promotion path</span>
              <span class="summary-value">{escape(_string_value(live_payload.get("promoted_from_instance")) or "direct ship")}</span>
            </article>
          </div>
        </div>
        <div class="badge-row">
          {_render_status_badge("deploy", _string_value(live_payload.get("deploy_status")))}
          {_render_status_badge("update", _string_value(live_payload.get("post_deploy_update_status")))}
          {_render_status_badge("health", _string_value(live_payload.get("destination_health_status")))}
          {_render_status_badge("backup", _string_value(backup_gate_payload.get("status")))}
        </div>
      </section>

      <section class="next-step-panel {_status_class(_string_value(next_step.get('tone')) or 'skipped')}">
        <p class="eyebrow">Suggested next step</p>
        <h2 class="next-step-title">{escape(_string_value(next_step.get("title")) or "Review current environment state")}</h2>
        <p class="next-step-copy">{escape(_string_value(next_step.get("summary")) or "Use the linked control-plane records below to decide the next operator action.")}</p>
        <div class="check-grid">
          {''.join(_render_checklist_item(item) for item in checklist if isinstance(item, Mapping))}
        </div>
        {_render_command_list(tuple(_string_value(command) for command in commands))}
      </section>

      <section class="layout">
        <div class="column">
          <article class="panel">
            <p class="eyebrow">Live Inventory</p>
            <h2>Current runtime state</h2>
            <div class="panel-grid">
              {_render_detail_section("Runtime", live_payload, (("artifact", "artifact_id"), ("source ref", "source_git_ref"), ("updated", "updated_at"), ("promoted from", "promoted_from_instance")))}
              {_render_detail_section("Records", live_payload, (("deployment record", "deployment_record_id"), ("promotion record", "promotion_record_id"), ("deploy status", "deploy_status"), ("health status", "destination_health_status")))}
            </div>
          </article>

          <article class="panel">
            <p class="eyebrow">Timeline</p>
            <h2>Latest control-plane events</h2>
            <div class="timeline">
              <div class="timeline-item">
                <h3>Latest deployment</h3>
                {_render_value("record", _string_value(latest_deployment_payload.get("record_id")))}
                {_render_value("deployment", _string_value(latest_deployment_payload.get("deployment_id")))}
                {_render_value("target", _string_value(latest_deployment_payload.get("target_name")))}
                {_render_value("status", _string_value(latest_deployment_payload.get("deploy_status")))}
                {_render_value("started", _string_value(latest_deployment_payload.get("started_at")))}
                {_render_value("finished", _string_value(latest_deployment_payload.get("finished_at")))}
              </div>
              <div class="timeline-item">
                <h3>Latest promotion</h3>
                {_render_value("record", _string_value(latest_promotion_payload.get("record_id")))}
                {_render_value("from", _string_value(latest_promotion_payload.get("from_instance")))}
                {_render_value("backup record", _string_value(live_promotion_payload.get("backup_record_id")))}
                {_render_value("status", _string_value(latest_promotion_payload.get("deploy_status")))}
              </div>
            </div>
          </article>
        </div>

        <div class="column">
          <div class="panel-stack">
            <article class="panel">
              <p class="eyebrow">Authorization</p>
              <h2>Backup gate</h2>
              {_render_value("record", _string_value(backup_gate_payload.get("record_id")))}
              {_render_value("status", _string_value(backup_gate_payload.get("status")))}
              {_render_value("source", _string_value(backup_gate_payload.get("source")))}
              {_render_value("created", _string_value(backup_gate_payload.get("created_at")))}
              {_render_evidence_pairs(backup_gate_payload.get("evidence"))}
            </article>

            <article class="panel">
              <p class="eyebrow">Promotion</p>
              <h2>Live promotion record</h2>
              {_render_value("record", _string_value(live_promotion_payload.get("record_id")))}
              {_render_value("artifact", _string_value(live_promotion_payload.get("artifact_id")))}
              {_render_value("backup gate record", _string_value(live_promotion_payload.get("backup_record_id")))}
              {_render_value("from", _string_value(live_promotion_payload.get("from_instance")))}
              {_render_value("to", _string_value(live_promotion_payload.get("to_instance")))}
              {_render_value("deploy status", _string_value(live_promotion_payload.get("deploy_status")))}
              {_render_value("health status", _string_value(live_promotion_payload.get("destination_health_status")))}
            </article>
          </div>
        </div>
      </section>
    </main>
  </body>
</html>
"""
