"""Consume the Odoo driver's native, database-bound observational probe."""

from datetime import UTC, datetime
import hashlib
import json
from typing import Any

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot, OdooSlotReadiness
from control_plane.dokploy.slot_maintenance import ContainerCommand


def verify_odoo_slot_readiness(
    plan: OdooCutoverPlan,
    slot: OdooSlot,
    *,
    web_container: str,
    contract: dict[str, Any],
    browser_receipt: dict[str, Any],
    execute: ContainerCommand,
    retained_assets_verified: bool,
) -> OdooSlotReadiness:
    """Preparation warms resources; the final HTTP probe only observes.

    The admitted driver supplies its exact DB/website contract and real browser
    receipt after maintenance and controlled release-marker preparation. A
    passing generic health endpoint cannot substitute for this private check.
    """
    identity = slot.runtime_identity.model_dump(mode="json")
    identity["source_revision"] = identity.pop("source_git_ref")
    if (
        contract.get("host") != plan.host
        or contract.get("database") != slot.binding.database
        or contract.get("database_uuid") != slot.runtime_identity.database_uuid
        or contract.get("runtime_identity") != identity
        or not retained_assets_verified
    ):
        raise ValueError("native readiness contract/asset retention differs from exact slot")
    if (
        execute(("docker", "inspect", "--format", "{{.Config.Image}}", web_container), None).strip()
        != slot.image
    ):
        raise ValueError("readiness target is not the immutable slot image")
    request = json.dumps({"contract": contract, "browser": browser_receipt})
    output = execute(
        (
            "docker",
            "exec",
            "-i",
            web_container,
            "/venv/bin/python",
            "-c",
            """
import json, subprocess, sys, tempfile
from pathlib import Path
value=json.loads(sys.stdin.read())
with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary); contract=root/'contract.json'; evidence=root/'evidence.json'
    contract.write_text(json.dumps(value['contract'])); evidence.write_text(json.dumps(value['browser']))
    argv=[sys.executable,'/opt/launchplane-readiness.py','--contract',str(contract),'--evidence',str(evidence)]
    prepared=subprocess.run(argv+['prepare'],capture_output=True,text=True,check=True)
    evidence.write_text(prepared.stdout)
    checked=subprocess.run(argv+['check'],capture_output=True,text=True,check=True)
    if json.loads(checked.stdout) != {'status':'pass'}: raise RuntimeError('private readiness failed')
    print(prepared.stdout.strip())
""",
        ),
        request,
    )
    observations = json.loads(output)
    if observations.get("browser") != browser_receipt:
        raise ValueError("native observations lost the supplied browser evidence")
    return OdooSlotReadiness(
        binding=slot.binding,
        slot_id=slot.slot_id,
        image=slot.image,
        host=plan.host,
        observed_at=datetime.now(UTC),
        evidence_id=hashlib.sha256(output.encode()).hexdigest(),
        runtime_identity=slot.runtime_identity,
        registry=True,
        pending_modules=False,
        render=True,
        assets=True,
        browser=True,
        side_effects_fenced=True,
        retained_previous_assets=True,
    )
