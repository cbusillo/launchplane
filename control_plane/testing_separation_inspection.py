from pathlib import Path
import re

import click

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import api, post_deploy, source
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_program_evidence import (
    classify_program_inventory,
    classify_program_history,
)
from control_plane.testing_separation_resources import (
    SeparationProgramInspection,
    STORAGE_PROGRAM_NAME,
    ProgramLogReadStatus,
)


def inspect_preparation_programs(
    *, root: Path, resources: OdooTestingResources, store: PostgresRecordStore, application: bool
) -> list[SeparationProgramInspection]:
    specifications: list[tuple[SeparationProgramInspection, str, str | None, str | None]] = []
    if application:
        from control_plane.testing_application_capabilities import (
            RETAINED_CAPABILITY_PROGRAM_NAME,
            retained_application_capability_spec,
            retained_application_capability_program,
        )

        capability_script = None
        capability_binding = None
        try:
            spec = retained_application_capability_spec(
                store, root, resources.product, resources.context
            )
            capability_script = retained_application_capability_program(spec)
            capability_binding = canonical_json_sha256(spec)
        except (ValueError, FileNotFoundError, click.ClickException):
            capability_script = None
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
                    capability_script,
                    capability_binding,
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
            return [item[0] for item in specifications]
        kind, lookup, _, _server = post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=resources.target_id,
            compose_name=resources.context,
            target_payload=payload,
        )
        rows = api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.list",
            query={"id": lookup, "scheduleType": kind},
        )
    except click.ClickException:
        return [item[0] for item in specifications]
    result = []
    for observed, name, expected_script, binding in specifications:
        inventory = classify_program_inventory(rows, name=name, namespace=resources.namespace)
        program = inventory.program
        if program is None:
            observed.status = (
                "missing"
                if inventory.status == "missing"
                else "ambiguous"
                if inventory.status == "ambiguous"
                else "unavailable"
            )
        else:
            observed.status = "observed"
            observed.schedule_id = program_identifier(api.schedule_key(program))
            observed.script_matches = (
                program.get("script") == expected_script if expected_script is not None else None
            )
            enabled = program.get("enabled")
            observed.disabled = not enabled if isinstance(enabled, bool) else None
            if observed.schedule_id:
                inspect_program_history(host=host, token=token, observed=observed, binding=binding)
        result.append(observed)
    return result


def inspect_program_history(
    *, host: str, token: str, observed: SeparationProgramInspection, binding: str | None
) -> None:
    try:
        payload = api.dokploy_request(
            host=host,
            token=token,
            path="/api/deployment.allByType",
            query={"id": observed.schedule_id, "type": "schedule"},
        )
    except click.ClickException:
        observed.history_status = "unavailable"
        return
    history = classify_program_history(
        payload,
        schedule_id=observed.schedule_id,
        original=observed.kind in {"storage_preparation", "retained_capability"},
    )
    observed.history_status = history.status
    if history.status in {"unavailable", "uncorrelated"}:
        return
    observed.history_count = len(history.rows)
    if len(history.rows) != 1:
        return
    row = history.rows[0]
    identifier = program_identifier(api.deployment_key(row))
    if not identifier:
        return
    observed.deployment_id = identifier
    status = api.deployment_status(row)
    if status in {"done", "error", "running", "queued"}:
        observed.deployment_status = status
    if (
        history.status != "terminal"
        or observed.script_matches is not True
        or observed.disabled is not True
    ):
        return
    markers = post_deploy._read_odoo_post_deploy_log_markers(
        host=host,
        token=token,
        schedule_id=observed.schedule_id,
        schedule_deployment_key=observed.deployment_id,
        deployment_id=observed.deployment_id,
        deployment=row,
    )
    observed.log_read_status = (
        ProgramLogReadStatus.AVAILABLE
        if markers.get("log_available") == "true"
        else ProgramLogReadStatus.UNAVAILABLE
    )
    marker = markers.get("testing_resources_ok")
    if marker in {"true", "false"}:
        observed.resources_ok = marker == "true"
    marker_binding = markers.get("testing_resources_binding_sha256")
    if marker_binding and binding is not None:
        observed.result_binding_matches = marker_binding == binding


def program_identifier(value: str) -> str:
    return value if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value) else ""
