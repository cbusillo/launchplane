import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
)
from control_plane.every_code_reconciliation import (
    reconcile_every_code_issue,
    rerun_every_code_issue,
)
from control_plane.storage.filesystem import FilesystemRecordStore


class EveryCodeIssueReconciliationTests(unittest.TestCase):
    def test_cli_authz_grant_workflow_posts_service_dry_run_request(self) -> None:
        runner = CliRunner()
        captured_request: dict[str, object] = {}

        def fake_post(**kwargs: object) -> dict[str, object]:
            captured_request.update(kwargs)
            return {
                "status": "accepted",
                "result": {"mode": "dry_run", "changed": True},
            }

        with (
            patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": "service-token"}),
            patch("control_plane.cli._post_launchplane_service_json", side_effect=fake_post),
        ):
            result = runner.invoke(
                main,
                [
                    "authz-policies",
                    "grant-workflow",
                    "--service-url",
                    "https://launchplane.example",
                    "--schema-version",
                    "2",
                    "--repository",
                    "cbusillo/launchplane",
                    "--repository-id",
                    "1001",
                    "--repository-owner-id",
                    "2001",
                    "--workflow-ref",
                    "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",
                    "--event-name",
                    "workflow_dispatch",
                    "--product",
                    "sellyouroutboard",
                    "--context",
                    "launchplane",
                    "--instance",
                    "testing",
                    "--action",
                    "deployment.write",
                    "--reason",
                    "Inspect grant before apply.",
                    "--related-issue",
                    "cbusillo/launchplane#83",
                    "--dry-run",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured_request["bearer_token"], "service-token")
        self.assertEqual(captured_request["session_cookie"], "")
        self.assertEqual(captured_request["path"], "/v1/authz-policies/github-actions/grants")
        payload = captured_request["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["reason"], "Inspect grant before apply.")
        grant = payload["grant"]
        assert isinstance(grant, dict)
        self.assertEqual(grant["repository"], "cbusillo/launchplane")
        self.assertEqual(grant["repository_id"], "1001")
        self.assertEqual(grant["repository_owner_id"], "2001")
        self.assertEqual(grant["instances"], ["testing"])
        self.assertEqual(grant["actions"], ["deployment.write"])
        response_payload = json.loads(result.output)
        self.assertEqual(response_payload["result"]["mode"], "dry_run")

    def test_cli_authz_grant_workflow_requires_one_auth_source(self) -> None:
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "authz-policies",
                "grant-workflow",
                "--service-url",
                "https://launchplane.example",
                "--repository",
                "cbusillo/launchplane",
                "--repository-id",
                "1001",
                "--repository-owner-id",
                "2001",
                "--product",
                "launchplane",
                "--action",
                "product_profile.read",
            ],
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("LAUNCHPLANE_SERVICE_TOKEN is required", result.output)

    def test_cli_authz_remove_workflow_rule_posts_service_dry_run_request(self) -> None:
        runner = CliRunner()
        captured_request: dict[str, object] = {}

        def fake_post(**kwargs: object) -> dict[str, object]:
            captured_request.update(kwargs)
            return {
                "status": "accepted",
                "result": {"mode": "dry_run", "changed": True},
            }

        with (
            patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": "service-token"}),
            patch("control_plane.cli._post_launchplane_service_json", side_effect=fake_post),
        ):
            result = runner.invoke(
                main,
                [
                    "authz-policies",
                    "remove-workflow-rule",
                    "--service-url",
                    "https://launchplane.example",
                    "--repository",
                    "cbusillo/launchplane",
                    "--repository-id",
                    "1001",
                    "--repository-owner-id",
                    "2001",
                    "--product",
                    "launchplane",
                    "--context",
                    "launchplane",
                    "--action",
                    "launchplane_service_deploy.execute",
                    "--reason",
                    "Inspect broad deploy authority removal.",
                    "--related-issue",
                    "cbusillo/launchplane#1049",
                    "--dry-run",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured_request["bearer_token"], "service-token")
        self.assertEqual(captured_request["session_cookie"], "")
        self.assertEqual(captured_request["path"], "/v1/authz-policies/github-actions/removals")
        payload = captured_request["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["reason"], "Inspect broad deploy authority removal.")
        removal = payload["removal"]
        assert isinstance(removal, dict)
        self.assertEqual(removal["repository"], "cbusillo/launchplane")
        self.assertEqual(removal["repository_id"], "1001")
        self.assertEqual(removal["repository_owner_id"], "2001")
        self.assertEqual(removal["instances"], [])
        self.assertEqual(removal["actions"], ["launchplane_service_deploy.execute"])
        response_payload = json.loads(result.output)
        self.assertEqual(response_payload["result"]["mode"], "dry_run")

    def test_cli_authz_remove_workflow_rule_requires_repository_id_pair(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "authz-policies",
                "remove-workflow-rule",
                "--service-url",
                "https://launchplane.example",
                "--repository",
                "cbusillo/launchplane",
                "--repository-id",
                "1001",
                "--product",
                "launchplane",
                "--action",
                "product_profile.read",
            ],
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("must be provided together", result.output)

    def test_cli_authz_grant_human_posts_service_request(self) -> None:
        runner = CliRunner()
        captured_request: dict[str, object] = {}

        def fake_post(**kwargs: object) -> dict[str, object]:
            captured_request.update(kwargs)
            return {
                "status": "accepted",
                "result": {"mode": "apply", "changed": True},
            }

        with patch("control_plane.cli._post_launchplane_service_json", side_effect=fake_post):
            result = runner.invoke(
                main,
                [
                    "authz-policies",
                    "grant-human",
                    "--service-url",
                    "https://launchplane.example",
                    "--session-cookie",
                    "launchplane_session=signed",
                    "--login",
                    "cbusillo",
                    "--role",
                    "admin",
                    "--product",
                    "sellyouroutboard",
                    "--context",
                    "sellyouroutboard",
                    "--action",
                    "generic_web_prod_promotion.dispatch",
                    "--reason",
                    "Allow SYO workflow dry-run validation.",
                    "--related-issue",
                    "cbusillo/launchplane#153",
                    "--idempotency-key",
                    "authz-human-grant:syo-dispatch",
                    "--apply",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured_request["bearer_token"], "")
        self.assertEqual(captured_request["session_cookie"], "launchplane_session=signed")
        self.assertEqual(captured_request["path"], "/v1/authz-policies/github-humans/grants")
        self.assertEqual(captured_request["idempotency_key"], "authz-human-grant:syo-dispatch")
        payload = captured_request["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["mode"], "apply")
        grant = payload["grant"]
        assert isinstance(grant, dict)
        self.assertEqual(grant["logins"], ["cbusillo"])
        self.assertEqual(grant["roles"], ["admin"])
        self.assertEqual(grant["instances"], [])
        self.assertEqual(grant["actions"], ["generic_web_prod_promotion.dispatch"])

    def test_cli_authz_grant_terminal_agent_posts_service_request(self) -> None:
        runner = CliRunner()
        captured_request: dict[str, object] = {}

        def fake_post(**kwargs: object) -> dict[str, object]:
            captured_request.update(kwargs)
            return {
                "status": "accepted",
                "result": {"mode": "apply", "changed": True},
            }

        with patch("control_plane.cli._post_launchplane_service_json", side_effect=fake_post):
            result = runner.invoke(
                main,
                [
                    "authz-policies",
                    "grant-terminal-agent",
                    "--service-url",
                    "https://launchplane.example",
                    "--session-cookie",
                    "launchplane_session=signed",
                    "--subject",
                    "local-owner-agent",
                    "--token-label",
                    "local-owner-read",
                    "--product",
                    "sellyouroutboard",
                    "--context",
                    "sellyouroutboard",
                    "--action",
                    "product_environment.read",
                    "--reason",
                    "Allow local terminal agent product context reads.",
                    "--related-issue",
                    "cbusillo/launchplane#426",
                    "--idempotency-key",
                    "authz-terminal-agent-grant:syo-read",
                    "--apply",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured_request["bearer_token"], "")
        self.assertEqual(captured_request["session_cookie"], "launchplane_session=signed")
        self.assertEqual(captured_request["path"], "/v1/authz-policies/terminal-agents/grants")
        self.assertEqual(captured_request["idempotency_key"], "authz-terminal-agent-grant:syo-read")
        payload = captured_request["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["mode"], "apply")
        grant = payload["grant"]
        assert isinstance(grant, dict)
        self.assertEqual(grant["subjects"], ["local-owner-agent"])
        self.assertEqual(grant["token_labels"], ["local-owner-read"])
        self.assertEqual(grant["instances"], [])
        self.assertEqual(grant["actions"], ["product_environment.read"])

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

    def test_rerun_issue_requeues_terminal_request(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = FilesystemRecordStore(state_dir=Path(tempdir))
            created = reconcile_every_code_issue(
                record_store=store,
                repository="cbusillo/launchplane",
                issue_number=278,
                issue_url="https://github.com/cbusillo/launchplane/issues/278",
                issue_title="Build Launchplane-backed Every Code automation",
                labels=("every-code",),
                actor="first",
            )
            assert created.request is not None
            claimed = store.claim_every_code_work_request_record(
                request_id=created.request.request_id,
                host="worker-host",
                claimed_at="2026-05-06T00:00:00Z",
            )
            assert claimed is not None
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="worker-host",
                    fencing_token=claimed.fencing_token,
                    updated_at="2026-05-06T00:01:00Z",
                    error_message="Needs another pass.",
                ),
            )
            store.write_every_code_work_request_record(blocked)

            result = rerun_every_code_issue(
                record_store=store,
                repository="cbusillo/launchplane",
                issue_number=278,
                actor="ops",
            )

            self.assertEqual(result.status, "queued")
            self.assertIsNotNone(result.request)
            assert result.request is not None
            self.assertEqual(result.request.state, "queued")
            self.assertEqual(result.request.trigger_actor, "ops")
            self.assertEqual(result.request.claimed_by_host, "")
            self.assertEqual(result.request.error_message, "")

    def test_cli_rerun_issue_writes_json_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runner = CliRunner()
            create_result = runner.invoke(
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
                    "every-code",
                ],
            )
            create_payload = json.loads(create_result.output)
            store = FilesystemRecordStore(state_dir=Path(tempdir))
            claimed = store.claim_every_code_work_request_record(
                request_id=create_payload["request"]["request_id"],
                host="worker-host",
                claimed_at="2026-05-06T00:00:00Z",
            )
            assert claimed is not None
            done = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="done",
                    host="worker-host",
                    fencing_token=claimed.fencing_token,
                    updated_at="2026-05-06T00:01:00Z",
                    result_pr_url="https://github.com/cbusillo/launchplane/pull/351",
                ),
            )
            store.write_every_code_work_request_record(done)

            rerun_result = runner.invoke(
                main,
                [
                    "every-code",
                    "rerun-issue",
                    "--state-dir",
                    tempdir,
                    "--repository",
                    "cbusillo/launchplane",
                    "--issue-number",
                    "278",
                    "--actor",
                    "ops",
                ],
            )

            self.assertEqual(create_result.exit_code, 0, create_result.output)
            self.assertEqual(rerun_result.exit_code, 0, rerun_result.output)
            rerun_payload = json.loads(rerun_result.output)
            self.assertEqual(rerun_payload["status"], "queued")
            self.assertEqual(rerun_payload["request"]["state"], "queued")
            self.assertEqual(rerun_payload["request"]["trigger_actor"], "ops")
            self.assertEqual(rerun_payload["request"]["result_pr_url"], "")

    def test_cli_rerun_issue_reports_service_failure_without_traceback(self) -> None:
        runner = CliRunner()

        with patch.dict(os.environ, {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-token"}):
            result = runner.invoke(
                main,
                [
                    "every-code",
                    "rerun-issue",
                    "--service-url",
                    "http://127.0.0.1:1",
                    "--repository",
                    "cbusillo/launchplane",
                    "--issue-number",
                    "278",
                    "--actor",
                    "ops",
                ],
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error: Launchplane API request failed", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_cli_rerun_issue_reports_invalid_service_input_without_traceback(self) -> None:
        runner = CliRunner()

        with patch.dict(os.environ, {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-token"}):
            result = runner.invoke(
                main,
                [
                    "every-code",
                    "rerun-issue",
                    "--service-url",
                    "http://127.0.0.1:1",
                    "--repository",
                    " ",
                    "--issue-number",
                    "278",
                    "--actor",
                    "ops",
                ],
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error: Every Code work request id requires", result.output)
        self.assertNotIn("Traceback", result.output)


if __name__ == "__main__":
    unittest.main()
