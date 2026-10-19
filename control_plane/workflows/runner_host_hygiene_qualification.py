"""Acceptance and summaries of the existing hygiene report/audit artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
from typing import TypedDict

import click

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAuditDeliveryEnvelope
from control_plane.workflows.runner_host_hygiene_audit_spool import RunnerHostHygieneAuditSpool
from control_plane.workflows.runner_host_hygiene_executor import _generated_cache_apply_not_needed


class HygieneQualification(TypedDict):
    audit_record_key: str
    observed_at: str
    as_of: str
    fresh: bool
    report_delivery: str
    report_status: str
    execution_state: str
    recovery_required: bool
    action_not_needed: bool
    maintenance_completed: bool
    retention_accounted_for: bool
    qualified: bool
    blockers: tuple[str, ...]
    findings: tuple[str, ...]
    builder_cache_usage: tuple[dict[str, object], ...]


def qualify_runner_host_hygiene(
    audit: RunnerHostHygieneAuditDeliveryEnvelope,
    *,
    as_of: datetime,
    maximum_age_seconds: int,
) -> HygieneQualification:
    report = audit.terminal_audit.post_apply_report if audit.terminal_audit else None
    report = report or audit.planned_audit.pre_apply_report
    try:
        observed = datetime.fromisoformat(report.observed_at)
        fresh = (
            observed.tzinfo is not None
            and 0 <= (as_of - observed).total_seconds() <= maximum_age_seconds
        )
    except (ValueError, TypeError):
        fresh = False
    not_needed = _generated_cache_apply_not_needed(audit.planned_audit.plan)
    unresolved = RunnerHostHygieneAuditSpool.is_unresolved(audit)
    blockers = tuple(
        item.code
        for item in audit.planned_audit.plan.blockers
        if item.code != "mutate_not_requested"
    )
    retained = (
        report.require_builder_cache_evidence
        and bool(report.builder_cache_usage)
        and all(item.accounted_for for item in report.builder_cache_usage)
    )
    return {
        "audit_record_key": audit.audit_record_key,
        "observed_at": report.observed_at,
        "as_of": as_of.isoformat(),
        "fresh": fresh,
        "report_delivery": audit.planned_delivery_state,
        "report_status": report.status,
        "execution_state": audit.execution_state,
        "recovery_required": unresolved,
        "action_not_needed": not_needed,
        "maintenance_completed": audit.maintenance_completed,
        "retention_accounted_for": retained,
        "qualified": fresh
        and retained
        and report.status == "healthy"
        and (not blockers or not_needed)
        and not unresolved
        and (audit.terminal_audit is None or audit.terminal_audit.status == "completed")
        and audit.planned_delivery_state == "delivered",
        "blockers": blockers,
        "findings": tuple(item.code for item in report.findings),
        "builder_cache_usage": tuple(
            {**item.model_dump(mode="json"), "qualification": item.qualification}
            for item in report.builder_cache_usage
        ),
    }


def _summary(
    audit: RunnerHostHygieneAuditDeliveryEnvelope, result: HygieneQualification, label: str
) -> str:
    reference = hashlib.sha256(audit.audit_record_key.encode()).hexdigest()[:12]
    try:
        observed = datetime.fromisoformat(str(result["observed_at"])).isoformat()
    except ValueError:
        observed = "unavailable"
    lines = [
        f"\n### Hygiene snapshot: {audit.action} ({label or reference})\n",
        f"- observed_at: `{observed}`",
    ]
    # Historical snapshots may be hours apart on Sunday. Do not imply the
    # current-time freshness verdict is a fact about that historical action.
    fields = {
        key: value
        for key, value in result.items()
        if key
        in {
            "report_delivery",
            "report_status",
            "execution_state",
            "recovery_required",
            "action_not_needed",
            "maintenance_completed",
            "retention_accounted_for",
            "blockers",
            "findings",
        }
    }
    lines.extend(f"- {key}: `{value}`" for key, value in fields.items())
    return "\n".join(lines) + "\n"


def _artifact_label(path: Path) -> str:
    prefix = "runner-host-hygiene-audit-evidence-"
    name = path.name
    if name.startswith(prefix) and name.endswith(".json"):
        suffix = name[len(prefix) : -5]
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,159}", suffix):
            return suffix
    return ""


@click.command("runner-host-hygiene-qualify")
@click.option("--audit-file", "audit_files", multiple=True, type=click.Path(path_type=Path))
@click.option("--as-of", type=click.DateTime(formats=["%Y-%m-%dT%H:%M:%S%z"]))
@click.option("--maximum-age-seconds", type=click.IntRange(min=1), default=3600, show_default=True)
@click.option("--require-qualified/--report-only", default=False)
@click.option("--summary-file", type=click.Path(path_type=Path))
def runner_host_hygiene_qualify(
    audit_files: tuple[Path, ...],
    as_of: datetime | None,
    maximum_age_seconds: int,
    require_qualified: bool,
    summary_file: Path | None,
) -> None:
    """Read existing audit artifacts; never contact or change a host."""
    outputs: list[dict[str, object]] = []
    summaries: list[str] = []
    for audit_file in audit_files:
        try:
            audit = RunnerHostHygieneAuditDeliveryEnvelope.model_validate_json(
                audit_file.read_text()
            )
            result = qualify_runner_host_hygiene(
                audit,
                as_of=as_of or datetime.now(timezone.utc),
                maximum_age_seconds=maximum_age_seconds,
            )
            outputs.append(dict(result))
            summaries.append(_summary(audit, result, _artifact_label(audit_file)))
        except (OSError, ValueError):
            outputs.append({"qualified": False, "reason_code": "audit_artifact_invalid"})
            summaries.append(
                f"\n### Hygiene snapshot unavailable ({_artifact_label(audit_file) or 'unattributed'})\n\nUnqualified: invalid or missing audit artifact.\n"
            )
    if not outputs:
        outputs.append({"qualified": False, "reason_code": "audit_artifact_missing"})
        summaries.append("\n### Hygiene snapshot unavailable\n\nUnqualified: no audit artifact.\n")
    if summary_file is not None:
        try:
            with summary_file.open("a") as output:
                output.write("".join(summaries))
        except OSError:
            outputs.append({"qualified": False, "reason_code": "summary_unavailable"})
            click.echo("Hygiene summary unavailable.", err=True)
    else:
        click.echo(json.dumps(outputs, indent=2))
    if require_qualified and any(not item["qualified"] for item in outputs):
        raise click.ClickException("runner host hygiene qualification needs attention")
