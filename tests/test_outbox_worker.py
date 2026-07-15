import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import click

from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_delivery_id,
    build_outbox_dedupe_key,
)
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _workflow_delivery() -> OutboxDeliveryRecord:
    dedupe_key = build_outbox_dedupe_key(
        kind="github_workflow_dispatch",
        parts=("example-product", "example-context", "deploy.yml", "main", "false", "patch"),
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(
            kind="github_workflow_dispatch",
            dedupe_key=dedupe_key,
        ),
        kind="github_workflow_dispatch",
        aggregate_type="generic_web_promotion_workflow",
        aggregate_id="example-product:example-context",
        dedupe_key=dedupe_key,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:00:00Z",
        next_attempt_at="2026-07-13T00:00:00Z",
        payload={
            "repository": "example/repo",
            "workflow_id": "deploy.yml",
            "ref": "main",
            "inputs": {"dry_run": "false", "bump": "patch"},
            "previous_run_ids": [100],
            "dispatch_started_at": "2026-07-13T00:00:00Z",
            "credential_context": "example-context",
            "observe_timeout_seconds": 0,
        },
    )


class OutboxWorkerTests(unittest.TestCase):
    def test_transient_github_failure_schedules_bounded_retry(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            delivery = _workflow_delivery()
            store.write_outbox_delivery_record(delivery)

            def github_request(
                *,
                path: str,
                token: str,
                method: str = "GET",
                body: dict[str, object] | None = None,
            ) -> dict[str, Any] | None:
                del path, token, method, body
                try:
                    raise OSError("temporary network failure")
                except OSError as cause:
                    raise click.ClickException("GitHub request failed") from cause

            with (
                patch.object(
                    store,
                    "_database_mutation_timestamp",
                    side_effect=lambda _session: "2026-07-13T00:00:01Z",
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                    side_effect=github_request,
                ),
            ):
                worker_result = run_outbox_worker_once(
                    record_store=store,
                    control_plane_root=Path("."),
                    lease_owner="worker-a",
                )
            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            early_claim = store.claim_next_outbox_delivery_record(
                lease_owner="worker-b",
                now="2026-07-13T00:00:05Z",
            )
            retry_claim = store.claim_next_outbox_delivery_record(
                lease_owner="worker-b",
                now="2026-07-13T00:00:06Z",
            )
            store.close()

        self.assertEqual(worker_result.status, "pending")
        self.assertEqual(loaded.state, "pending")
        self.assertEqual(loaded.attempt, 1)
        self.assertEqual(loaded.error_code, "github_provider_error")
        self.assertEqual(loaded.next_attempt_at, "2026-07-13T00:00:06Z")
        self.assertEqual(early_claim.status, "empty")
        self.assertEqual(retry_claim.status, "claimed")
        assert retry_claim.record is not None
        self.assertEqual(retry_claim.record.attempt, 2)

    def test_crash_before_send_leaves_pending_delivery_for_later_worker(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            delivery = _workflow_delivery()
            store.write_outbox_delivery_record(delivery)

            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            requests: list[tuple[str, str]] = []
            posted = False

            def github_request(
                *,
                path: str,
                token: str,
                method: str = "GET",
                body: dict[str, object] | None = None,
            ) -> dict[str, Any] | None:
                nonlocal posted
                del token, body
                requests.append((method, path))
                if method == "POST":
                    posted = True
                    return None
                if not posted:
                    return {"workflow_runs": []}
                return {
                    "workflow_runs": [
                        {
                            "id": 101,
                            "html_url": "https://github.example/actions/runs/101",
                            "status": "queued",
                            "created_at": "2026-07-13T00:00:00Z",
                        }
                    ]
                }

            with (
                patch(
                    "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                    side_effect=github_request,
                ),
            ):
                worker_result = run_outbox_worker_once(
                    record_store=store,
                    control_plane_root=Path("."),
                    lease_owner="worker-a",
                )
            store.close()

        self.assertEqual(loaded.state, "pending")
        self.assertEqual(worker_result.status, "delivered")
        self.assertEqual([method for method, _path in requests], ["POST", "GET"])

    def test_crash_after_send_reconciles_existing_workflow_run_before_resend(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            delivery = _workflow_delivery()
            store.write_outbox_delivery_record(delivery)
            claimed = store.claim_next_outbox_delivery_record(
                lease_owner="worker-a",
                now="2026-07-13T00:00:01Z",
            )
            assert claimed.record is not None
            with patch.object(
                store,
                "_database_mutation_timestamp",
                return_value="2026-07-13T00:00:02Z",
            ):
                store.mark_outbox_delivery_provider_started(
                    record=claimed.record,
                    lease_owner="worker-a",
                    provider_operation_key="github_workflow_dispatch:example/repo:deploy.yml:main:2026-07-13T00:00:00Z:bump=patch|dry_run=false",
                    provider_id="github",
                    updated_at="2026-07-13T00:00:01Z",
                )
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: "2026-07-13T00:10:00Z",
            ):
                requests: list[tuple[str, str]] = []

                def github_request(
                    *,
                    path: str,
                    token: str,
                    method: str = "GET",
                    body: dict[str, object] | None = None,
                ) -> dict[str, Any] | None:
                    del token, body
                    requests.append((method, path))
                    if method == "POST":
                        self.fail("reconciliation must not resend workflow_dispatch")
                    return {
                        "workflow_runs": [
                            {
                                "id": 101,
                                "html_url": "https://github.example/actions/runs/101",
                                "status": "queued",
                                "created_at": "2026-07-13T00:00:00Z",
                            }
                        ]
                    }

                with (
                    patch(
                        "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                        return_value="github-token",
                    ),
                    patch(
                        "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                        side_effect=github_request,
                    ),
                ):
                    worker_result = run_outbox_worker_once(
                        record_store=store,
                        control_plane_root=Path("."),
                        lease_owner="worker-b",
                    )
            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            store.close()

        self.assertEqual(worker_result.status, "delivered")
        self.assertEqual(loaded.state, "delivered")
        self.assertEqual(loaded.provider_id, "github")
        self.assertEqual(loaded.external_id, "101")
        self.assertEqual(loaded.payload["run_status"], "queued")
        self.assertEqual(loaded.payload["run_conclusion"], "")
        self.assertEqual([method for method, _path in requests], ["GET"])

    def test_provider_marker_without_visible_run_never_resends_dispatch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            delivery = _workflow_delivery()
            store.write_outbox_delivery_record(delivery)
            claimed = store.claim_next_outbox_delivery_record(
                lease_owner="worker-a",
                now="2026-07-13T00:00:01Z",
            )
            assert claimed.record is not None
            with patch.object(
                store,
                "_database_mutation_timestamp",
                return_value="2026-07-13T00:00:02Z",
            ):
                store.mark_outbox_delivery_provider_started(
                    record=claimed.record,
                    lease_owner="worker-a",
                    provider_operation_key=(
                        "github_workflow_dispatch:example/repo:deploy.yml:main:"
                        "2026-07-13T00:00:00Z:bump=patch|dry_run=false"
                    ),
                    provider_id="github",
                    updated_at="2026-07-13T00:00:01Z",
                )
            requests: list[tuple[str, str]] = []

            def github_request(
                *,
                path: str,
                token: str,
                method: str = "GET",
                body: dict[str, object] | None = None,
            ) -> dict[str, Any] | None:
                del token, body
                requests.append((method, path))
                if method == "POST":
                    self.fail("a persisted provider marker must never resend workflow_dispatch")
                return {"workflow_runs": []}

            with (
                patch.object(
                    store,
                    "_database_mutation_timestamp",
                    side_effect=lambda _session: "2026-07-13T00:10:00Z",
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                    side_effect=github_request,
                ),
            ):
                worker_result = run_outbox_worker_once(
                    record_store=store,
                    control_plane_root=Path("."),
                    lease_owner="worker-b",
                )
            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            store.close()

        self.assertEqual(worker_result.status, "reconcile_required")
        self.assertEqual(loaded.state, "reconcile_required")
        self.assertEqual(loaded.action, "workflow_dispatch_in_doubt")
        self.assertEqual(loaded.error_code, "workflow_run_not_observed")
        self.assertEqual([method for method, _path in requests], ["GET"])


if __name__ == "__main__":
    unittest.main()
