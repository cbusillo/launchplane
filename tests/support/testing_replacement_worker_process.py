"""Run the real worker shutdown path with an isolated, pipe-controlled effect."""

from pathlib import Path
import sys
from unittest.mock import patch

from control_plane.cli_service import service_odoo_workers_run
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.storage.postgres import PostgresRecordStore


def main(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)

    def effect(**kwargs: object) -> OdooStableTargetReplacementApplyResult:
        print("effect admitted", flush=True)
        if sys.stdin.readline().strip() != "finish":
            raise AssertionError("The fixture did not release its admitted effect")
        (operation,) = store.list_odoo_stable_target_replacement_operation_records(
            statuses=("running",)
        )
        assert operation.request.strategy == "recreate-in-place"
        return OdooStableTargetReplacementApplyResult(
            product=operation.product,
            context=operation.context,
            instance=operation.instance,
            strategy=operation.request.strategy,
            deployment_record_id="isolated-deployment",
            deploy_status="pass",
            post_deploy_status="pass",
            health_status="pass",
            canonical_status="pass",
            logo_status="pass",
            runtime_identity_injected=True,
        )

    try:
        with (
            patch("control_plane.cli_service._store", return_value=store),
            patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                side_effect=effect,
            ),
        ):
            assert service_odoo_workers_run.callback is not None
            service_odoo_workers_run.callback(
                state_dir=Path("state"),
                database_url=database_url,
                control_plane_root=Path.cwd(),
                lease_owner="isolated-worker",
                lease_seconds=300,
                heartbeat_seconds=1,
                max_attempts=5,
                poll_seconds=1,
                error_backoff_seconds=1,
                max_consecutive_errors=3,
            )
    finally:
        store.close()


if __name__ == "__main__":
    main(sys.argv[1])
