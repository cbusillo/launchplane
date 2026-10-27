import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.every_code_worker import (
    EveryCodeWorkerApiError,
    EveryCodeWorkerApiStore,
    every_code_worktree_branch,
    every_code_worktree_root,
)


def _queued_record() -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-123-test",
        source="github_issue_label",
        state="queued",
        repository="cbusillo/code",
        issue_number=123,
        issue_url="https://github.com/cbusillo/code/issues/123",
        issue_title="Wire local automation",
        trigger_label="every-code",
        trigger_actor="cbusillo",
        github_delivery_id="delivery-1",
        queued_at="2026-05-05T22:00:00Z",
        updated_at="2026-05-05T22:00:00Z",
    )


class _WorkRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        record = _queued_record()
        if self.headers.get("Authorization") != "Bearer worker-token":
            self.send_response(401)
            self.end_headers()
            return
        if self.path != f"/v1/every-code/work-requests/{record.request_id}":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"request": record.model_dump(mode="json")}).encode())


class EveryCodeHistoricalAdapterTests(unittest.TestCase):
    def test_worktree_path_and_branch_are_deterministic(self) -> None:
        record = _queued_record()
        root = every_code_worktree_root(record, state_dir=Path("state"))
        branch = every_code_worktree_branch(record)

        self.assertEqual(
            root,
            Path("state").resolve()
            / "every-code-worker"
            / "worktrees"
            / "cbusillo-code"
            / "every-code-cbusillo-code-123-test",
        )
        self.assertEqual(
            branch,
            "every-code/cbusillo-code-123-every-code-cbusillo-code-123-test",
        )

    def test_read_record_via_authenticated_service(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _WorkRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            store = EveryCodeWorkerApiStore(
                service_url=f"http://127.0.0.1:{server.server_port}",
                worker_token="worker-token",
            )
            record = _queued_record()
            self.assertEqual(store.read_every_code_work_request_record(record.request_id), record)
            denied = EveryCodeWorkerApiStore(
                service_url=store.service_url, worker_token="wrong-token"
            )
            with self.assertRaises(EveryCodeWorkerApiError):
                denied.read_every_code_work_request_record(record.request_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
