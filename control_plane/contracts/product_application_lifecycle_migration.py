"""One-time conversion of retired nonproduction extension behavior to declarations."""

from copy import deepcopy


def migrate_product_application_lifecycle_payload(payload: object) -> object:
    if not isinstance(payload, dict) or payload.get("driver_id") != "generic-web":
        return payload
    migrated = deepcopy(payload)
    for lane in migrated.get("lanes", []):
        extensions = lane.get("generic_web_extensions", {})
        if (
            lane.get("instance") == "testing"
            and extensions.get("verification_extension") == "verireel-testing"
            and "schema_version" not in extensions
        ):
            _convert(extensions, preview=False)
            lane["generic_web_extensions"] = extensions
    preview = migrated.get("preview", {})
    extensions = preview.get("generic_web_extensions", {})
    if (
        preview.get("transport_extension") == "verireel-postgres"
        and "schema_version" not in extensions
    ):
        _convert(extensions, preview=True)
        preview["generic_web_extensions"] = extensions
    return migrated


def _convert(extensions: dict[str, object], *, preview: bool) -> None:
    # These are legacy behavior translations, never runtime defaults or a product catalog.
    commands: list[dict[str, str | bool]] = [
        {
            "name": "ver-apply-prisma-migrations",
            "command": "./node_modules/.bin/prisma migrate deploy --config prisma.config.ts",
        },
    ]
    if preview:
        commands.append({"name": "ver-preview-seed", "command": "node prisma/seed.mjs"})
    else:
        commands.append(
            {
                "name": "ver-testing-reset",
                "command": "node prisma/reset-testing-job.mjs && ./node_modules/.bin/prisma migrate deploy --schema prisma/schema.prisma && node prisma/seed.mjs",
                "on_reset": True,
            }
        )
    existing = extensions.setdefault("maintenance", [])
    if not isinstance(existing, list):
        raise ValueError("Legacy application maintenance must be a list.")
    names = {item["name"] for item in existing}
    existing.extend(item for item in commands if item["name"] not in names)
    schedules = extensions.setdefault("schedules", [])
    if not isinstance(schedules, list):
        raise ValueError("Legacy application schedules must be a list.")
    if not any(item["name"] == "verireel-billing-recovery" for item in schedules):
        schedules.append(
            {
                "name": "verireel-billing-recovery",
                "command": "sh ./docker/invoke-billing-recovery-cron.sh --target-url 'http://127.0.0.1:3000/api/cron/billing-recovery'",
                "cron_expression": "12,27,42,57 * * * *" if preview else "2,17,32,47 * * * *",
                "timeout_seconds": 180,
            }
        )
    extensions["schema_version"] = 1
