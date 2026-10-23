from pathlib import Path
import re

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.dokploy import api, post_deploy, source
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_separation_resources import (
    SeparationProgramInspection,
    STORAGE_PROGRAM_NAME,
)


def inspect_preparation_programs(
    *, root: Path, resources: OdooTestingResources, store: PostgresRecordStore, application: bool
) -> list[SeparationProgramInspection]:
    specifications: list[tuple[SeparationProgramInspection, str, str, str]] = []
    if application:
        from control_plane.testing_application_capabilities import (
            RETAINED_CAPABILITY_PROGRAM_NAME,
            retained_application_capability_spec,
            retained_application_capability_program,
        )

        spec = retained_application_capability_spec(
            store, root, resources.product, resources.context
        )
        script = retained_application_capability_program(spec)
        for kind, suffix in (
            ("retained_capability", ""),
            ("retained_capability_readback", " readback"),
        ):
            specifications.append(
                (
                    SeparationProgramInspection.model_validate(
                        {"kind": kind, "status": "unavailable"}
                    ),
                    RETAINED_CAPABILITY_PROGRAM_NAME + suffix,
                    script,
                    canonical_json_sha256(spec),
                )
            )
    for kind, suffix, script in (
        ("storage_preparation", "", resources.script("prepare")),
        ("storage_readback", " readback", resources.script("readback")),
    ):
        specifications.append(
            (
                SeparationProgramInspection.model_validate({"kind": kind, "status": "unavailable"}),
                STORAGE_PROGRAM_NAME + suffix,
                script,
                resources.binding_sha256,
            )
        )
    try:
        host, token = source.read_dokploy_config(control_plane_root=root)
        payload = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="application" if application else "compose",
            target_id=resources.target_id,
        )
        if payload.get("applicationId" if application else "composeId") != resources.target_id:
            raise ValueError("Original placement identity changed.")
        kind, lookup, _, _server = post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=resources.target_id,
            compose_name=resources.context,
            target_payload=payload,
        )
        rows = read_program_rows(
            host=host,
            token=token,
            path="/api/schedule.list",
            query={"id": lookup, "scheduleType": kind},
        )
    except click.ClickException:
        return [item[0] for item in specifications]
    if rows is None:
        return [item[0] for item in specifications]
    result = []
    for observed, name, script, binding in specifications:
        matches = [
            row
            for row in rows
            if row.get("name") == name and row.get("appName") == resources.namespace
        ]
        if not matches:
            observed.status = "missing"
        elif len(matches) != 1:
            observed.status = "ambiguous"
        elif matches[0].get("script") != script or matches[0].get("enabled") is not False:
            observed.status = "provenance_mismatch"
        else:
            observed.schedule_id = program_identifier(api.schedule_key(matches[0]))
            if observed.schedule_id:
                inspect_program_history(host=host, token=token, observed=observed, binding=binding)
        result.append(observed)
    return result


def inspect_program_history(
    *, host: str, token: str, observed: SeparationProgramInspection, binding: str
) -> None:
    try:
        rows = read_program_rows(
            host=host,
            token=token,
            path="/api/deployment.allByType",
            query={"id": observed.schedule_id, "type": "schedule"},
        )
        if rows is None or any(row.get("scheduleId") != observed.schedule_id for row in rows):
            return
        identifiers = [program_identifier(api.deployment_key(row)) for row in rows]
        if any(not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
            return
        observed.status = "observed"
        observed.history_count = len(rows)
        if len(rows) != 1:
            return
        row = rows[0]
        observed.deployment_id = identifiers[0]
        status = api.deployment_status(row)
        if status in {"done", "error", "running", "queued"}:
            observed.deployment_status = status
        if status not in {"done", "error"}:
            return
        started_at, finished_at = row.get("startedAt"), row.get("finishedAt")
        if not isinstance(started_at, str) or not isinstance(finished_at, str):
            return
        started = parse_launchplane_mutation_timestamp(started_at, field_name="startedAt")
        finished = parse_launchplane_mutation_timestamp(finished_at, field_name="finishedAt")
        if finished < started:
            return
        markers = post_deploy._read_odoo_post_deploy_log_markers(
            host=host,
            token=token,
            schedule_id=observed.schedule_id,
            schedule_deployment_key=observed.deployment_id,
            deployment_id=observed.deployment_id,
            deployment=row,
        )
        observed.log_available = markers.get("log_available") == "true"
        marker = markers.get("testing_resources_ok")
        if marker in {"true", "false"}:
            observed.resources_ok = marker == "true"
        marker_binding = markers.get("testing_resources_binding_sha256")
        if marker_binding:
            observed.result_binding_matches = marker_binding == binding
    except (click.ClickException, ValueError, TypeError):
        return


def program_identifier(value: str) -> str:
    return value if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value) else ""


def read_program_rows(
    *, host: str, token: str, path: str, query: dict[str, str | int]
) -> list[api.JsonObject] | None:
    try:
        payload = api.dokploy_request(host=host, token=token, path=path, query=query)
    except click.ClickException:
        return None
    if (
        not isinstance(payload, list)
        or len(payload) > 10000
        or any(not isinstance(row, dict) for row in payload)
    ):
        return None
    return [row for row in payload if isinstance(row, dict)]
