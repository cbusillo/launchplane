"""Apply decision-bound website settings in a credential-safe slot runner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import base64
import json
from typing import TYPE_CHECKING

from control_plane._odoo_website_settings_program import (
    WEBSITE_INDEXING_PARAMETER,
    WEBSITE_SETTINGS_PROGRAM,
)
from control_plane.contracts.odoo_cutover import (
    OdooCutoverPlan,
    OdooSlot,
    OdooWebsiteSettingsReceipt,
)
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent

if TYPE_CHECKING:
    from control_plane.dokploy.slot_writers import SlotWriterContainer


@dataclass(frozen=True)
class StartupSettingsReadback:
    """Durable payload digests and the recreated, still-fenced writer inventory."""

    selected_digest: str
    retained_digest: str
    containers: tuple[SlotWriterContainer, ...]


# Update/read both durable payloads AND recreate their stopped consumers. A
# docker restart preserves creation-time env and cannot satisfy this contract.
StartupSettings = Callable[
    [OdooCutoverPlan, OdooSlot, OdooWebsiteSettingsIntent], StartupSettingsReadback
]


def _verify_startup_containers(
    plan: OdooCutoverPlan,
    intent: OdooWebsiteSettingsIntent,
    readback: StartupSettingsReadback,
    execute: Callable[[tuple[str, ...], str | None], str],
) -> None:
    expected = {
        (slot.slot_id, writer.writer_id) for slot in (plan.old, plan.new) for writer in plan.writers
    }
    targets = readback.containers
    if (
        {(item.slot_id, item.writer.writer_id) for item in targets} != expected
        or len(targets) != len(expected)
        or len({item.container for item in targets}) != len(targets)
    ):
        raise ValueError("startup readback requires every writer in both slots")
    for target in targets:
        slot = plan.old if target.slot_id == plan.old.slot_id else plan.new
        observed = json.loads(execute(("docker", "inspect", target.container), None))
        if (
            target.writer not in plan.writers
            or target.image != slot.image
            or len(observed) != 1
            or observed[0]["Config"]["Image"] != slot.image
            or observed[0]["State"].get("Running")
            or observed[0]["State"].get("Restarting")
        ):
            raise ValueError("startup container image/inventory differs or writers are unfenced")
        environment = dict(item.split("=", 1) for item in observed[0]["Config"]["Env"])
        payload = json.loads(base64.b64decode(environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"]))
        indexing = [
            item["value"]
            for item in payload.get("config_parameters", [])
            if item["key"] == WEBSITE_INDEXING_PARAMETER
        ]
        if (
            payload.get("instance") != "prod"
            or payload.get("website_settings") != intent.settings.model_dump(mode="json")
            or indexing
            != [
                {
                    "source": "literal",
                    "value": "1" if intent.settings.public_indexing_enabled else "0",
                }
            ]
        ):
            raise ValueError("effective container startup settings differ from the snapshot")


def run_slot_website_settings(
    plan: OdooCutoverPlan,
    slot: OdooSlot,
    intent: OdooWebsiteSettingsIntent,
    *,
    runner_container: str,
    execute: Callable[[tuple[str, ...], str | None], str],
    configure_startup: StartupSettings | None,
    python: str = "/venv/bin/python",
) -> OdooWebsiteSettingsReceipt:
    """Idempotent application/readback, including historical images.

    The caller completes planned credential maintenance first on promotion.
    Rollback uses the retained slot whose credential boundary already passed.
    The generated native consumer is supplied by Launchplane, so old images do
    not need the new website module. A digest is printed only after commit.
    """
    expected = plan.website_settings if slot == plan.new else plan.rollback_website_settings
    if (
        slot not in (plan.old, plan.new)
        or intent != expected
        or intent.artifact_id != slot.binding.artifact_id
    ):
        raise ValueError("website settings differ from the immutable slot snapshot")
    if not runner_container or configure_startup is None:
        raise ValueError("website settings require a bound runner/startup configuration capability")
    image = execute(
        ("docker", "inspect", "--format", "{{.Config.Image}}", runner_container), None
    ).strip()
    if image != slot.image:
        raise ValueError("website settings runner differs from the immutable slot image")
    # Set the legacy startup parameter as well as typed native intent. Merge in
    # the runner, retaining unrelated managed values without exporting secrets.
    raw = intent.settings.model_dump(mode="json")
    request = {
        "settings": raw,
        "indexing_parameter": WEBSITE_INDEXING_PARAMETER,
        "program": WEBSITE_SETTINGS_PROGRAM,
        "database": slot.binding.database,
        "database_uuid": slot.runtime_identity.database_uuid,
    }
    readback = configure_startup(plan, slot, intent)
    startup_digest, retained_digest = readback.selected_digest, readback.retained_digest
    if startup_digest != intent.settings.digest or retained_digest != intent.settings.digest:
        raise ValueError("durable startup website settings readback differs from the snapshot")
    _verify_startup_containers(plan, intent, readback, execute)
    program = """
import base64, json, os, subprocess, sys
request=json.load(sys.stdin)
import psycopg2
with psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),dbname=request['database'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD']) as connection, connection.cursor() as cursor:
    cursor.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
    row=cursor.fetchone()
    if not row or row[0]!=request['database_uuid']:
        raise RuntimeError('website settings database UUID differs from the slot')
payload=json.loads(base64.b64decode(os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64') or 'e30='))
payload['instance']='prod'
payload['website_settings']=request['settings']
parameters=[item for item in payload.get('config_parameters',[]) if item['key']!=request['indexing_parameter']]
parameters.append({'key':request['indexing_parameter'],'value':{'source':'literal','value':'1' if request['settings']['public_indexing_enabled'] else '0'}})
payload['config_parameters']=parameters
environment=dict(os.environ,ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64=base64.b64encode(json.dumps(payload).encode()).decode())
result=subprocess.run([sys.executable,'-c',request['program'],request['database'],'prod'],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
if result.returncode:
    raise RuntimeError('native website settings commit/readback failed')
lines=[line.split('=',1)[1] for line in result.stdout.decode().splitlines() if line.startswith('website_settings_sha256=')]
if len(lines)!=1:
    raise RuntimeError('native website settings receipt is missing or ambiguous')
print(json.dumps({'settings_sha256':lines[0]}))
"""
    output = execute(
        ("docker", "exec", "-i", runner_container, python, "-c", program), json.dumps(request)
    )
    digest = json.loads(output).get("settings_sha256")
    if digest != intent.settings.digest:
        raise ValueError("committed website settings receipt differs from the snapshot")
    return OdooWebsiteSettingsReceipt(
        binding=slot.binding,
        image=slot.image,
        settings_sha256=digest,
        startup_settings_sha256=startup_digest,
        retained_startup_settings_sha256=retained_digest,
        committed=True,
    )
