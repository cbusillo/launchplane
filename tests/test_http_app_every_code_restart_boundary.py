"""Real service restart boundaries; SQLite does not prove PostgreSQL locking."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
import unittest

from fastapi import FastAPI

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _every_code_work_request_rerun_policy,
    _every_code_work_request_status_policy,
    _post_every_code_work_request_claim,
    _post_every_code_work_request_rerun,
    _post_every_code_work_request_status,
    _RejectingVerifier,
    _seed_every_code_claim_request,
    _seed_every_code_rerun_intent,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request
from tests.support.stores import _sqlite_database_url


class EveryCodeRestartBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory.name) / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()

    def terminal_request(
        self, state: Literal["done", "blocked"] = "done"
    ) -> EveryCodeWorkRequestRecord:
        queued = _seed_every_code_claim_request(self.store)
        claimed = self.store.claim_every_code_work_request_record(
            request_id=queued.request_id,
            host="worker-a",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed is not None
        return self.store.update_every_code_work_request_status_record(
            request_id=claimed.request_id,
            update=EveryCodeWorkRequestStatusUpdate(
                state=state,
                host=claimed.claimed_by_host,
                fencing_token=claimed.fencing_token,
                updated_at="2026-05-05T22:02:00Z",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Preserve the completed workspace for feedback.",
                error_message="Operator input required." if state == "blocked" else "",
            ),
        )

    def app(self, *, worker: bool = False, rerun: bool = False) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier() if worker else _StubVerifier(_identity()),
            authz_policy=(
                LaunchplaneAuthzPolicy()
                if worker
                else (
                    _every_code_work_request_rerun_policy()
                    if rerun
                    else _every_code_work_request_status_policy()
                )
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

    async def test_terminal_status_cannot_restart_under_update_or_worker_authority(self) -> None:
        for terminal_state in ("done", "blocked"):
            for worker in (False, True):
                with self.subTest(state=terminal_state, worker=worker):
                    terminal = self.terminal_request(terminal_state)
                    response = await _post_every_code_work_request_status(
                        self.app(worker=worker),
                        {
                            "request_id": terminal.request_id,
                            "host": terminal.claimed_by_host,
                            "fencing_token": terminal.fencing_token,
                            "state": "running",
                            "updated_at": "2026-05-05T22:03:00Z",
                            "result_pr_url": terminal.result_pr_url,
                            "result_summary": "Resumed for PR feedback.",
                        },
                        authorization="Bearer worker-token" if worker else "Bearer valid-token",
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error"]["code"], "invalid_payload")
                    self.assertIn("finished", response.json()["error"]["message"])
                    self.assertEqual(
                        self.store.read_every_code_work_request_record(terminal.request_id),
                        terminal,
                    )

    async def test_approved_intent_does_not_give_update_identity_rerun_authority(self) -> None:
        terminal = self.terminal_request()
        intent = _seed_every_code_rerun_intent(self.store)
        payload: dict[str, object] = {
            "request_id": terminal.request_id,
            "agent_write_intent_record_id": intent.record_id,
        }
        denied = await _post_every_code_work_request_rerun(self.app(), payload)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
        self.assertEqual(
            self.store.read_every_code_work_request_record(terminal.request_id), terminal
        )

        # The exact same intent is valid when the caller has the distinct action.
        accepted = await _post_every_code_work_request_rerun(self.app(rerun=True), payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["records"]["state"], "queued")

    async def test_rerun_authority_still_requires_approved_intent(self) -> None:
        terminal = self.terminal_request()
        response = await _post_every_code_work_request_rerun(
            self.app(rerun=True), {"request_id": terminal.request_id}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "agent_write_intent_required")
        self.assertEqual(
            self.store.read_every_code_work_request_record(terminal.request_id), terminal
        )

        denied_intent = _seed_every_code_rerun_intent(self.store, authorized=False)
        for worker in (False, True):
            with self.subTest(worker=worker):
                rejected = await _post_every_code_work_request_rerun(
                    self.app(worker=worker, rerun=True),
                    {
                        "request_id": terminal.request_id,
                        "agent_write_intent_record_id": denied_intent.record_id,
                    },
                    authorization="Bearer worker-token" if worker else "Bearer valid-token",
                )
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(
                    rejected.json()["error"]["code"], "agent_write_intent_not_executable"
                )
                self.assertEqual(
                    self.store.read_every_code_work_request_record(terminal.request_id), terminal
                )

    async def test_old_callbacks_cannot_mutate_new_claim_after_authorized_rerun(self) -> None:
        terminal = self.terminal_request()
        intent = _seed_every_code_rerun_intent(self.store)
        app = self.app(worker=True)
        rerun = await _post_every_code_work_request_rerun(
            app,
            {
                "request_id": terminal.request_id,
                "agent_write_intent_record_id": intent.record_id,
            },
            authorization="Bearer worker-token",
        )
        self.assertEqual(rerun.status_code, 202, rerun.text)
        claim = await _post_every_code_work_request_claim(
            app,
            {"request_id": terminal.request_id, "host": terminal.claimed_by_host},
            authorization="Bearer worker-token",
        )
        self.assertEqual(claim.status_code, 202, claim.text)
        fresh = self.store.read_every_code_work_request_record(terminal.request_id)
        self.assertGreater(fresh.fencing_token, terminal.fencing_token)
        self.assertNotEqual(fresh.lifecycle_id, terminal.lifecycle_id)

        # Same host/old fence and wrong host/current fence fail independently.
        for host, fence in (
            (terminal.claimed_by_host, terminal.fencing_token),
            ("worker-other", fresh.fencing_token),
        ):
            with self.subTest(host=host, fence=fence):
                heartbeat = await request(
                    app,
                    "POST",
                    "/v1/every-code/work-requests/heartbeat",
                    headers={"Authorization": "Bearer worker-token"},
                    payload={
                        "request_id": fresh.request_id,
                        "host": host,
                        "fencing_token": fence,
                    },
                )
                self.assertEqual(heartbeat.status_code, 409, heartbeat.text)
                self.assertEqual(heartbeat.json()["error"]["code"], "heartbeat_rejected")
                for state in ("running", "done", "blocked"):
                    completion = await _post_every_code_work_request_status(
                        app,
                        {
                            "request_id": fresh.request_id,
                            "host": host,
                            "fencing_token": fence,
                            "state": state,
                            "error_message": "Old failure" if state == "blocked" else "",
                        },
                        authorization="Bearer worker-token",
                    )
                    self.assertEqual(completion.status_code, 400, completion.text)
                    self.assertEqual(completion.json()["error"]["code"], "invalid_payload")
                    self.assertEqual(
                        self.store.read_every_code_work_request_record(fresh.request_id), fresh
                    )

        accepted = await _post_every_code_work_request_status(
            app,
            {
                "request_id": fresh.request_id,
                "host": fresh.claimed_by_host,
                "fencing_token": fresh.fencing_token,
                "state": "done",
            },
            authorization="Bearer worker-token",
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(
            self.store.read_every_code_work_request_record(fresh.request_id).state, "done"
        )
