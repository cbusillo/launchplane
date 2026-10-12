from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import click

from control_plane.dokploy import api as dokploy_api
from control_plane.cron import cron_matches_utc as cron_matches_utc
from control_plane.cron import validate_cron_expression


VeriReelRecoveryScheduleInstance = Literal["testing", "prod", "preview"]

VERIREEL_BILLING_RECOVERY_SCHEDULE_NAME = "verireel-billing-recovery"
VERIREEL_BILLING_RECOVERY_TARGET_URL = "http://127.0.0.1:3000/api/cron/billing-recovery"
VERIREEL_BILLING_RECOVERY_CANARY_TIMEOUT_SECONDS = 180

_RECOVERY_CRON_EXPRESSIONS: dict[VeriReelRecoveryScheduleInstance, str] = {
    "testing": "2,17,32,47 * * * *",
    "prod": "7,22,37,52 * * * *",
    "preview": "12,27,42,57 * * * *",
}


@dataclass(frozen=True)
class VeriReelRecoveryScheduleSnapshot:
    existed: bool
    enabled: bool = False


def recovery_schedule_cron(instance: VeriReelRecoveryScheduleInstance) -> str:
    return _RECOVERY_CRON_EXPRESSIONS[instance]


def recovery_schedule_command(instance: VeriReelRecoveryScheduleInstance) -> str:
    del instance
    return (
        "sh ./docker/invoke-billing-recovery-cron.sh --target-url "
        f"'{VERIREEL_BILLING_RECOVERY_TARGET_URL}'"
    )


def _validate_cron_expression(expression: str) -> None:
    try:
        validate_cron_expression(expression)
    except (TypeError, ValueError) as error:
        raise click.ClickException(
            f"Invalid VeriReel billing-recovery cron expression: {expression!r}. {error}"
        ) from error


def _schedule_payload(
    *,
    application_id: str,
    instance: VeriReelRecoveryScheduleInstance,
    enabled: bool,
) -> dokploy_api.JsonObject:
    return {
        "name": VERIREEL_BILLING_RECOVERY_SCHEDULE_NAME,
        "cronExpression": recovery_schedule_cron(instance),
        "scheduleType": "application",
        "shellType": "sh",
        "command": recovery_schedule_command(instance),
        "applicationId": application_id,
        "enabled": enabled,
        "timezone": "UTC",
    }


def _read_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    instance: VeriReelRecoveryScheduleInstance,
    require_exact: bool,
) -> tuple[dokploy_api.JsonObject | None, bool]:
    schedule = dokploy_api.read_dokploy_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=VERIREEL_BILLING_RECOVERY_SCHEDULE_NAME,
    )
    if schedule is None:
        return None, False
    enabled = schedule.get("enabled")
    if not isinstance(enabled, bool):
        raise click.ClickException(
            "Dokploy managed VeriReel billing-recovery schedule readback did not expose a boolean enabled value."
        )
    if require_exact:
        dokploy_api.assert_dokploy_application_schedule_exact(
            application_id=application_id,
            schedule=schedule,
            expected_payload=_schedule_payload(
                application_id=application_id,
                instance=instance,
                enabled=enabled,
            ),
        )
    return schedule, enabled


def reconcile_verireel_billing_recovery_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    instance: VeriReelRecoveryScheduleInstance,
    enabled: bool = True,
) -> None:
    _validate_cron_expression(recovery_schedule_cron(instance))
    existing_schedule = dokploy_api.read_dokploy_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=VERIREEL_BILLING_RECOVERY_SCHEDULE_NAME,
    )
    disabled_payload = _schedule_payload(
        application_id=application_id,
        instance=instance,
        enabled=False,
    )
    schedule = dokploy_api.upsert_dokploy_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_payload=disabled_payload,
    )
    if not enabled:
        return
    schedule_id = dokploy_api.schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            "Dokploy managed VeriReel billing-recovery schedule did not expose a schedule id for its canary."
        )
    try:
        dokploy_api.run_dokploy_schedule(
            host=host,
            token=token,
            schedule_id=schedule_id,
            timeout_seconds=VERIREEL_BILLING_RECOVERY_CANARY_TIMEOUT_SECONDS,
        )
        dokploy_api.upsert_dokploy_application_schedule(
            host=host,
            token=token,
            application_id=application_id,
            schedule_payload=_schedule_payload(
                application_id=application_id,
                instance=instance,
                enabled=True,
            ),
        )
    except click.ClickException as error:
        if existing_schedule is not None:
            raise
        try:
            dokploy_api.delete_dokploy_application_schedule(
                host=host,
                token=token,
                application_id=application_id,
                schedule_payload=disabled_payload,
            )
        except click.ClickException as cleanup_error:
            raise click.ClickException(
                f"{error}\nNew disabled recovery schedule cleanup failed: {cleanup_error}"
            ) from error
        raise


def quiesce_verireel_billing_recovery_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    instance: VeriReelRecoveryScheduleInstance,
) -> VeriReelRecoveryScheduleSnapshot:
    schedule, enabled = _read_schedule(
        host=host,
        token=token,
        application_id=application_id,
        instance=instance,
        require_exact=False,
    )
    if schedule is None:
        return VeriReelRecoveryScheduleSnapshot(existed=False)
    reconcile_verireel_billing_recovery_schedule(
        host=host,
        token=token,
        application_id=application_id,
        instance=instance,
        enabled=False,
    )
    return VeriReelRecoveryScheduleSnapshot(existed=True, enabled=enabled)


def restore_verireel_billing_recovery_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    instance: VeriReelRecoveryScheduleInstance,
    snapshot: VeriReelRecoveryScheduleSnapshot,
) -> None:
    if not snapshot.existed:
        return
    dokploy_api.upsert_dokploy_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_payload=_schedule_payload(
            application_id=application_id,
            instance=instance,
            enabled=snapshot.enabled,
        ),
    )


def finalize_verireel_billing_recovery_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    instance: VeriReelRecoveryScheduleInstance,
    snapshot: VeriReelRecoveryScheduleSnapshot,
) -> None:
    if snapshot.existed:
        restore_verireel_billing_recovery_schedule(
            host=host,
            token=token,
            application_id=application_id,
            instance=instance,
            snapshot=snapshot,
        )
        return
    reconcile_verireel_billing_recovery_schedule(
        host=host,
        token=token,
        application_id=application_id,
        instance=instance,
    )


def delete_verireel_billing_recovery_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
) -> None:
    schedule, _enabled = _read_schedule(
        host=host,
        token=token,
        application_id=application_id,
        instance="preview",
        require_exact=False,
    )
    if schedule is None:
        return
    observed_payload = {
        field_name: schedule.get(field_name)
        for field_name in (
            "name",
            "cronExpression",
            "scheduleType",
            "shellType",
            "command",
            "applicationId",
            "enabled",
            "timezone",
        )
    }
    dokploy_api.delete_dokploy_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_payload=observed_payload,
    )
