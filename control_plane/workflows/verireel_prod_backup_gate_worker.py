from __future__ import annotations

import json
import sys

import click

from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateWorkerRequest,
    VeriReelProdBackupGateWorkerResult,
)
from control_plane.workflows.ship import utc_now_timestamp


def execute_worker(
    request: VeriReelProdBackupGateWorkerRequest,
) -> VeriReelProdBackupGateWorkerResult:
    raise click.ClickException(
        "Legacy VeriReel backup execution is unavailable; configure typed production backup "
        "authority through /v1/production-backup-authority/apply and capture both required "
        "backups through /v1/production-backup-gates."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        request = VeriReelProdBackupGateWorkerRequest.model_validate(payload)
        result = execute_worker(request)
        sys.stdout.write(f"{result.model_dump_json()}\n")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        failure = VeriReelProdBackupGateWorkerResult(
            status="fail",
            snapshot_name="",
            started_at="",
            finished_at=utc_now_timestamp(),
            detail=message,
            evidence={},
        )
        sys.stdout.write(f"{failure.model_dump_json()}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
