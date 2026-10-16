"""One-time conversion of retired nonproduction extension behavior to declarations."""

from copy import deepcopy
from control_plane.contracts.product_profile_record import (
    APPLICATION_LIFECYCLE_PROFILE_SCHEMA_VERSION,
    LaunchplaneProductProfileRecord,
)


def migrate_product_application_lifecycle_payload(payload: object) -> object:
    if not isinstance(payload, dict) or payload.get("driver_id") != "generic-web":
        return payload
    if payload.get("schema_version", 1) >= APPLICATION_LIFECYCLE_PROFILE_SCHEMA_VERSION:
        return payload
    migrated = deepcopy(payload)
    converted = False
    for lane in migrated.get("lanes", []):
        extensions = lane.get("generic_web_extensions", {})
        if (
            lane.get("instance") == "testing"
            and extensions.get("verification_extension") == "verireel-testing"
        ):
            converted = True
            _convert(extensions, preview=False)
            lane["generic_web_extensions"] = extensions
    preview = migrated.get("preview", {})
    extensions = preview.get("generic_web_extensions", {})
    if preview.get("transport_extension") == "verireel-postgres":
        converted = True
        _convert(extensions, preview=True)
        preview["generic_web_extensions"] = extensions
    if converted:
        migrated["schema_version"] = APPLICATION_LIFECYCLE_PROFILE_SCHEMA_VERSION
        try:
            LaunchplaneProductProfileRecord.model_validate(
                migrated, context={"stored_product_profile": True}
            )
        except ValueError:
            raise ValueError(
                "Stored profile lifecycle conversion conflicts with existing declarations; review its testing/preview commands before upgrade."
            ) from None
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
    schedules = extensions.setdefault("schedules", [])
    if not isinstance(schedules, list):
        raise ValueError("Legacy application schedules must be a list.")
    names = {item["name"] for item in existing}
    schedule_names = {item["name"] for item in schedules}
    for command in commands:
        if command["name"] in names or any(
            item.get("command") == command["command"]
            and bool(item.get("on_reset", False)) == bool(command.get("on_reset", False))
            for item in existing
        ):
            continue
        if command["name"] in schedule_names:
            raise ValueError(
                "Legacy maintenance name conflicts with a declared schedule; review the testing/preview profile before upgrade."
            )
        existing.append(command)
    recovery_command = "sh ./docker/invoke-billing-recovery-cron.sh --target-url 'http://127.0.0.1:3000/api/cron/billing-recovery'"
    if any(item.get("command") == recovery_command for item in schedules):
        return
    if "verireel-billing-recovery" not in schedule_names:
        if "verireel-billing-recovery" in names:
            raise ValueError(
                "Legacy schedule name conflicts with a declared maintenance command; review the testing/preview profile before upgrade."
            )
        schedules.append(
            {
                "name": "verireel-billing-recovery",
                "command": recovery_command,
                "cron_expression": "12,27,42,57 * * * *" if preview else "2,17,32,47 * * * *",
                "timeout_seconds": 180,
            }
        )
