"""Standalone testing replacements participate in the self-deploy fence."""

from datetime import UTC, datetime, timedelta
import os
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.service_deploy_drain import confirm_startup, read_status, record_dispatch
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.durable_operations import durable_operation_policy_record
from tests.test_client_release_redeploy import _prepare, _replacement_env
from tests.test_odoo_stable_operation_worker import _replacement_payload


class TestingReplacementRedeployTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(database_url=f"sqlite:///{root / 'state.sqlite3'}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.operation = OdooStableTargetReplacementOperationRecord.model_validate(
            _replacement_payload()
        )
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)

    def claim(self) -> OdooStableTargetReplacementOperationRecord | None:
        now = datetime.now(UTC)
        return self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="isolated-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )

    def test_admitted_replacement_blocks_dispatch_until_terminal_commit(self) -> None:
        claimed = self.claim()
        assert claimed is not None
        # A missed heartbeat alone cannot prove that the provider effect ended.
        self.store.write_odoo_stable_target_replacement_operation_record(
            claimed.model_copy(update={"lease_expires_at": claimed.started_at})
        )
        record, running, dispatch = _prepare(self.store)
        self.assertEqual(record.state, "draining")
        self.assertEqual(running, (claimed.operation_id,))
        self.assertFalse(dispatch)
        self.assertEqual(read_status(self.store)["running_operation_ids"], running)

        # Restore the live fixture lease, then use the worker's guarded commit path.
        self.store.write_odoo_stable_target_replacement_operation_record(claimed)
        finished = datetime.now(UTC).isoformat()
        self.assertTrue(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=claimed.model_copy(
                    update={
                        "status": "fail",
                        "phase": "failed",
                        "finished_at": finished,
                        "updated_at": finished,
                    }
                ),
                lease_owner=claimed.lease_owner,
            )
        )
        _, running, dispatch = _prepare(self.store)
        self.assertFalse(running)
        self.assertTrue(dispatch)

    def test_pending_replacement_waits_for_matching_startup_and_fences_old_workers(self) -> None:
        record, _, dispatch = _prepare(self.store)
        self.assertTrue(dispatch)
        self.assertIsNone(self.claim())
        record_dispatch(self.store, record.request_fingerprint)
        self.assertIsNone(self.claim())
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )
        with patch.dict(
            os.environ, {**_replacement_env(), "LAUNCHPLANE_DEPLOYMENT_MARKER": "wrong"}
        ):
            confirm_startup(self.store)
            self.assertIsNone(self.claim())
        with patch.dict(os.environ, _replacement_env()):
            confirm_startup(self.store)
        self.assertIsNone(self.claim(), "old worker must remain fenced after confirmation")
        with patch.dict(os.environ, _replacement_env()):
            claimed = self.claim()
        assert claimed is not None
        self.assertEqual(claimed.operation_id, self.operation.operation_id)
        self.assertEqual(claimed.status, "running")

    def test_sigterm_finishes_admitted_effect_and_commits_before_dispatch(self) -> None:
        assert self.operation.authorization is not None
        self.store.seed_authz_policy_if_absent(
            durable_operation_policy_record(self.operation.authorization.model_dump())
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.support.testing_replacement_worker_process",
                self.store.database_url,
            ],
            env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 30)
            self.assertTrue(readable, "worker did not admit the fixture effect")
            self.assertEqual(process.stdout.readline().strip(), "effect admitted")
            _, running, dispatch = _prepare(self.store)
            self.assertEqual(running, (self.operation.operation_id,))
            self.assertFalse(dispatch)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate("finish\n", timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "stopped")
            completed = self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            )
            self.assertEqual(completed.status, "pass")
            self.assertEqual(completed.result.deploy_status if completed.result else None, "pass")
            self.assertTrue(completed.finished_at)
            _, running, dispatch = _prepare(self.store)
            self.assertFalse(running)
            self.assertTrue(dispatch)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
