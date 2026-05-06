import json
from pathlib import Path
import tempfile
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.every_code_reconciliation import reconcile_every_code_issue
from control_plane.storage.filesystem import FilesystemRecordStore


class EveryCodeIssueReconciliationTests(unittest.TestCase):
    def test_creates_queued_request_when_trigger_label_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = FilesystemRecordStore(state_dir=Path(tempdir))

            result = reconcile_every_code_issue(
                record_store=store,
                repository="cbusillo/launchplane",
                issue_number=278,
                issue_url="https://github.com/cbusillo/launchplane/issues/278",
                issue_title="Build Launchplane-backed Every Code automation",
                labels=("plan", "Every-Code"),
                actor="ops",
            )

            self.assertEqual(result.status, "created")
            self.assertIsNotNone(result.request)
            assert result.request is not None
            self.assertEqual(result.request.source, "reconciliation")
            self.assertEqual(result.request.state, "queued")
            self.assertEqual(result.request.repository, "cbusillo/launchplane")
            self.assertEqual(result.request.issue_number, 278)
            self.assertEqual(result.request.trigger_actor, "ops")

    def test_dedupes_existing_request_without_overwriting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = FilesystemRecordStore(state_dir=Path(tempdir))
            first = reconcile_every_code_issue(
                record_store=store,
                repository="cbusillo/launchplane",
                issue_number=278,
                issue_url="https://github.com/cbusillo/launchplane/issues/278",
                issue_title="Original title",
                labels=("every-code",),
                actor="first",
            )
            assert first.request is not None
            claimed = store.claim_every_code_work_request_record(
                request_id=first.request.request_id,
                host="worker-host",
                claimed_at="2026-05-06T00:00:00Z",
            )
            self.assertIsNotNone(claimed)

            second = reconcile_every_code_issue(
                record_store=store,
                repository="CBUSILLO/LAUNCHPLANE",
                issue_number=278,
                issue_url="https://github.com/cbusillo/launchplane/issues/278",
                issue_title="Changed title",
                labels=("EVERY-CODE",),
                actor="second",
            )

            self.assertEqual(second.status, "deduped")
            self.assertIsNotNone(second.request)
            assert second.request is not None
            self.assertEqual(second.request.state, "claimed")
            self.assertEqual(second.request.issue_title, "Original title")
            self.assertEqual(second.request.trigger_actor, "first")

    def test_skips_when_trigger_label_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = FilesystemRecordStore(state_dir=Path(tempdir))

            result = reconcile_every_code_issue(
                record_store=store,
                repository="cbusillo/launchplane",
                issue_number=278,
                issue_url="https://github.com/cbusillo/launchplane/issues/278",
                issue_title="Build Launchplane-backed Every Code automation",
                labels=("plan",),
            )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(
                store.list_every_code_work_request_records(limit=10),
                (),
            )

    def test_cli_reconcile_issue_writes_json_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runner = CliRunner()

            result = runner.invoke(
                main,
                [
                    "every-code",
                    "reconcile-issue",
                    "--state-dir",
                    tempdir,
                    "--repository",
                    "cbusillo/launchplane",
                    "--issue-number",
                    "278",
                    "--issue-url",
                    "https://github.com/cbusillo/launchplane/issues/278",
                    "--issue-title",
                    "Build Launchplane-backed Every Code automation",
                    "--label",
                    "plan",
                    "--label",
                    "every-code",
                    "--actor",
                    "ops",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "created")
            self.assertEqual(payload["request"]["source"], "reconciliation")
            self.assertEqual(payload["request"]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
