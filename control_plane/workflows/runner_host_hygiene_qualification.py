"""Acceptance and summaries of the existing hygiene report/audit artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import click

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAuditDeliveryEnvelope


class HygieneQualification(TypedDict):
    audit_record_key: str
    observed_at: str
    as_of: str
    fresh: bool
    report_delivery: str
    report_status: str
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
    blockers = tuple(
        item.code
        for item in audit.planned_audit.plan.blockers
        if item.code != "mutate_not_requested"
    )
    retained = (
        report.require_builder_cache_evidence
        and bool(report.builder_cache_usage)
        and all(
            item.qualification in {"within_target", "protected_cache", "unbudgeted_report_only"}
            for item in report.builder_cache_usage
        )
    )
    return {
        "audit_record_key": audit.audit_record_key,
        "observed_at": report.observed_at,
        "as_of": as_of.isoformat(),
        "fresh": fresh,
        "report_delivery": audit.planned_delivery_state,
        "report_status": report.status,
        "maintenance_completed": (
            audit.terminal_audit is not None
            and audit.terminal_audit.status == "completed"
            and audit.terminal_audit.request.mutate
            and audit.planned_delivery_state == "delivered"
            and audit.terminal_delivery_state == "delivered"
        ),
        "retention_accounted_for": retained,
        "qualified": fresh
        and retained
        and report.status == "healthy"
        and not blockers
        and audit.planned_delivery_state == "delivered",
        "blockers": blockers,
        "findings": tuple(item.code for item in report.findings),
        "builder_cache_usage": tuple(
            {**item.model_dump(mode="json"), "qualification": item.qualification}
            for item in report.builder_cache_usage
        ),
    }


@click.command("runner-host-hygiene-qualify")
@click.option("--audit-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--as-of", type=click.DateTime(formats=["%Y-%m-%dT%H:%M:%S%z"]))
@click.option("--maximum-age-seconds", type=click.IntRange(min=1), default=3600, show_default=True)
@click.option("--require-qualified/--report-only", default=False)
@click.option("--summary-file", type=click.Path(path_type=Path))
def runner_host_hygiene_qualify(
    audit_file: Path,
    as_of: datetime | None,
    maximum_age_seconds: int,
    require_qualified: bool,
    summary_file: Path | None,
) -> None:
    """Read one existing audit artifact; never contact or change a host."""
    import json

    try:
        audit = RunnerHostHygieneAuditDeliveryEnvelope.model_validate_json(audit_file.read_text())
        result = qualify_runner_host_hygiene(
            audit,
            as_of=as_of or datetime.now(timezone.utc),
            maximum_age_seconds=maximum_age_seconds,
        )
        if summary_file is not None:
            # Render only bounded contract codes and booleans, never host names,
            # paths, raw Docker output or free-text evidence in Actions summaries.
            with summary_file.open("a") as output:
                output.write("\n### Runner host hygiene qualification\n\n")
                for key, value in result.items():
                    if key not in (
                        "report_delivery",
                        "report_status",
                        "maintenance_completed",
                        "retention_accounted_for",
                        "fresh",
                        "qualified",
                        "blockers",
                        "findings",
                    ):
                        continue
                    output.write(f"- {key}: `{value}`\n")
        click.echo(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    if require_qualified and not result["qualified"]:
        raise click.ClickException("runner host hygiene qualification needs attention")
