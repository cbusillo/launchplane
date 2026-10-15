"""Complete planned maintenance in a private, immutable candidate runner."""

from collections.abc import Callable
import hashlib
import json
from typing import Any

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooMaintenanceReceipt
from control_plane.dokploy.slot_website_settings import StartupSettings, run_slot_website_settings

# The driver passes a transport executing argv and stdin on the bound provider
# host. Secrets stay in the existing managed container environment.
ContainerCommand = Callable[[tuple[str, ...], str | None], str]


def maintenance_payload(plan: OdooCutoverPlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "database": plan.new.binding.database,
        "database_uuid": plan.new.runtime_identity.database_uuid,
        "release": plan.compatibility.model_dump(mode="json"),
        "candidate_manifest": plan.candidate.model_dump(mode="json"),
        "production_manifest": plan.production.model_dump(mode="json"),
        "cutover_binding": plan.new.binding.model_dump(mode="json"),
        "cutover_slot": plan.new.slot_id,
    }

    if plan.website_settings is not None or plan.production_website_settings is not None:
        payload["website_settings"] = (
            plan.website_settings.model_dump(mode="json") if plan.website_settings else None
        )
        payload["production_website_settings"] = (
            plan.production_website_settings.model_dump(mode="json")
            if plan.production_website_settings
            else None
        )
    return payload


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_planned_slot_maintenance(
    plan: OdooCutoverPlan,
    *,
    runner_container: str,
    execute: ContainerCommand,
    python: str = "/venv/bin/python",
    workflow: str = "/volumes/scripts/run_odoo_data_workflows.py",
    configure_startup: StartupSettings | None = None,
) -> OdooMaintenanceReceipt:
    """Read back before repeating; a running/failed attempt remains uncertain.

    The existing SQL-only devkit boundary is the only credential policy. The
    full planned entrypoint verifies the baked declaration/graph, commits that
    boundary and creates its registry once. Never invoke the legacy install
    pass, infer success from generic schedule text, or start candidate web here.
    """
    if plan.website_settings is not None and configure_startup is None:
        raise ValueError("website settings require the bound startup configuration capability")
    if not runner_container:
        raise ValueError("private maintenance requires an explicit runner/state directory")
    observed_image = execute(
        ("docker", "inspect", "--format", "{{.Config.Image}}", runner_container), None
    ).strip()
    if observed_image != plan.new.image:
        raise ValueError("maintenance runner is not the candidate's immutable image")
    payload = maintenance_payload(plan)
    digest = payload_digest(payload)
    directory = "/volumes/data/.launchplane-cutovers/" + digest
    request = json.dumps({"directory": directory, "payload": payload})
    # This script does not import Odoo or load a registry. It owns a durable
    # attempt marker before the child, and exposes only its receipt/guard ID.
    program = """
import json, os, subprocess, sys
from pathlib import Path
request = json.loads(sys.stdin.read())
directory = Path(request['directory'])
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
# An independent, registry-free connection binds the selected physical DB.
import psycopg2
connection_args=dict(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),dbname=request['payload']['database'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'])
def boundary():
    with psycopg2.connect(**connection_args) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT key,value FROM ir_config_parameter WHERE key IN ('database.uuid','odoo_devkit.credential_boundary_commit')")
        values=dict(cursor.fetchall())
    if values.get('database.uuid') != request['payload']['database_uuid']:
        raise RuntimeError('selected database UUID differs from the release plan')
    return values.get('odoo_devkit.credential_boundary_commit')
previous_boundary=boundary()
plan_file, receipt, attempt = (directory / name for name in ('plan.json', 'receipt.json', 'attempt.json'))
attestation=directory/'boundary.json'
payload = json.dumps(request['payload'], sort_keys=True, separators=(',', ':'))
if plan_file.exists() and plan_file.read_text() != payload:
    raise RuntimeError('immutable maintenance plan changed')
if not attempt.exists():
    # Reuse the image's registry-free resolver against an independent SQL
    # inventory. Static roots may include optional uninstalled addons; exact
    # work means the resolved native plan, not a copied approximation of it.
    sys.path.insert(0,str(Path(sys.argv[1]).parent))
    from odoo_release_plan import resolve_plan
    with psycopg2.connect(**connection_args) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT name,state FROM ir_module_module')
        states=dict(cursor.fetchall())
    graph={module['name']:set(module['depends']) for module in request['payload']['candidate_manifest']['release_compatibility']['modules']}
    expected=resolve_plan(request['payload'],database=request['payload']['database'],image=request['payload']['release']['candidate_image'],states=states,graph=graph)
    plan_file.write_text(payload)
    descriptor = os.open(attempt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write('started')
        stream.flush()
        os.fsync(stream.fileno())
    environment = dict(os.environ, ODOO_RELEASE_MODULE_PLAN_FILE=str(plan_file),
        ODOO_RELEASE_IMAGE=request['payload']['release']['candidate_image'],
        ODOO_RELEASE_MAINTENANCE_RECEIPT_FILE=str(receipt), ODOO_MAX_CRON_THREADS='0')
    result = subprocess.run([sys.executable, sys.argv[1], '--post-deploy-maintenance'],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log = directory / 'run.log'
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as stream:
        stream.write(result.stdout + result.stderr)
        stream.flush(); os.fsync(stream.fileno())
    if result.returncode:
        raise RuntimeError('full planned maintenance failed')
    committed=boundary()
    if not committed or committed==previous_boundary:
        raise RuntimeError('passing maintenance has no new committed boundary witness')
    with os.fdopen(os.open(attestation,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600),'w') as stream:
        json.dump({'plan':payload,'credential_commit_id':committed,'install_modules':expected['install_modules'],'update_modules':expected['update_modules']},stream)
        stream.flush();os.fsync(stream.fileno())
if not receipt.is_file():
    raise RuntimeError('maintenance outcome is uncertain')
result = json.loads(receipt.read_text())
if result.get('state') != 'passed':
    raise RuntimeError('maintenance has no passing receipt')
# Replay requires the same run's sealed witness in the current database.
proof=json.loads(attestation.read_text())
if proof.get('plan') != payload or not proof.get('credential_commit_id') or boundary()!=proof['credential_commit_id']:
    raise RuntimeError('committed boundary does not belong to this passing run')
if any(result.get(field)!=proof.get(field) for field in ('install_modules','update_modules')):
    raise RuntimeError('passing receipt omits resolved native module work')
print(json.dumps({'receipt': result, 'credential_commit_id': proof['credential_commit_id']}, sort_keys=True))
"""
    output = execute(
        ("docker", "exec", "-i", runner_container, python, "-c", program, workflow), request
    )
    response = json.loads(output)
    result = response["receipt"]
    if (
        result.get("state") != "passed"
        or result.get("plan_sha256") != digest
        or result.get("database") != plan.new.binding.database
        or result.get("candidate_artifact_id") != plan.candidate.artifact_id
        or result.get("candidate_image") != plan.new.image
        or result.get("candidate_manifest_sha256") != plan.compatibility.candidate_manifest_sha256
        or result.get("odoo_processes") != 1
        or not set(result.get("update_modules", ())) <= set(result.get("updated_by_odoo", ()))
        or any(state != "installed" for state in result.get("module_states", {}).values())
    ):
        raise ValueError("full planned maintenance receipt is missing or mismatched")
    receipt = OdooMaintenanceReceipt(
        release_id=plan.release_id,
        binding=plan.new.binding,
        image=plan.new.image,
        candidate_manifest_sha256=plan.compatibility.candidate_manifest_sha256,
        credential_commit_id=response["credential_commit_id"],
        credential_commit_verified_before_registry=True,
        install_modules=tuple(result["install_modules"]),
        update_modules=tuple(result["update_modules"]),
        module_readback=True,
        evidence_id=digest,
    )
    if plan.website_settings is not None:
        receipt = receipt.model_copy(
            update={
                "website_settings": run_slot_website_settings(
                    plan,
                    plan.new,
                    plan.website_settings,
                    runner_container=runner_container,
                    execute=execute,
                    configure_startup=configure_startup,
                    python=python,
                )
            }
        )
    # Only the validated full-run receipt releases the candidate's startup
    # guard. No registry-capable web process can precede the SQL commit.
    stamp = hashlib.sha256(plan.new.model_dump_json().encode()).hexdigest()
    execute(
        (
            "docker",
            "exec",
            "-i",
            runner_container,
            python,
            "-c",
            "import os,sys; from pathlib import Path; p=Path(sys.argv[1]); "
            "t=p.with_suffix('.pending'); t.write_text(sys.stdin.read()); "
            "f=os.open(t,os.O_RDONLY); os.fsync(f); os.close(f); os.replace(t,p)",
            directory + "/web-ready",
        ),
        stamp,
    )
    return receipt
