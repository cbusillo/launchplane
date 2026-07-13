import json
import subprocess
import unittest
from collections.abc import Sequence
from typing import cast

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.every_code_webhooks import sync_every_code_webhooks


CLI_MAIN = cast(Command, main)


class _WebhookRunner:
    def __init__(self, *, hooks: dict[str, list[dict[str, object]]]) -> None:
        self.hooks = hooks
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self, args: Sequence[str], input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append((command, input_text))
        if command[:4] == ("gh", "repo", "list", "cbusillo"):
            return subprocess.CompletedProcess(
                command,
                0,
                "cbusillo/code\ncbusillo/launchplane\n",
                "",
            )
        if command[:3] == ("gh", "api", "repos/cbusillo/code/hooks"):
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.hooks["cbusillo/code"]), ""
            )
        if command[:3] == ("gh", "api", "repos/cbusillo/launchplane/hooks"):
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.hooks["cbusillo/launchplane"]), ""
            )
        if command[:5] == ("gh", "api", "-X", "PATCH", "repos/cbusillo/code/hooks/12"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"id": 12}), "")
        if command[:5] == ("gh", "api", "-X", "POST", "repos/cbusillo/launchplane/hooks"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"id": 34}), "")
        if command[:5] == ("gh", "label", "list", "--repo", "cbusillo/code"):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps([{"name": "every-code"}, {"name": "preview"}]),
                "",
            )
        if command[:5] == ("gh", "label", "list", "--repo", "cbusillo/launchplane"):
            return subprocess.CompletedProcess(command, 0, json.dumps([]), "")
        if command[:3] == ("gh", "label", "edit"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("gh", "label", "create"):
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class _FailingWebhookRunner(_WebhookRunner):
    def __init__(self, *, error_detail: str) -> None:
        super().__init__(hooks={"cbusillo/code": [], "cbusillo/launchplane": []})
        self.error_detail = error_detail

    def __call__(
        self, args: Sequence[str], input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        if command[:3] == ("gh", "api", "repos/cbusillo/code/hooks"):
            self.calls.append((command, input_text))
            raise subprocess.CalledProcessError(1, command, stderr=self.error_detail)
        return super().__call__(args, input_text)


class EveryCodeWebhookSyncTests(unittest.TestCase):
    def test_sync_updates_existing_hook_and_creates_missing_hook(self) -> None:
        webhook_url = "https://launchplane.example/v1/every-code/github-webhook"
        runner = _WebhookRunner(
            hooks={
                "cbusillo/code": [
                    {
                        "id": 12,
                        "config": {"url": webhook_url},
                    }
                ],
                "cbusillo/launchplane": [],
            }
        )

        results = sync_every_code_webhooks(
            owner="cbusillo",
            webhook_secret="secret",
            webhook_url=webhook_url,
            runner=runner,
        )

        self.assertEqual([result.status for result in results], ["updated", "created"])
        self.assertEqual(results[0].hook_id, 12)
        self.assertEqual(results[1].hook_id, 34)
        self.assertEqual(results[0].labels_synced, 6)
        self.assertEqual(results[1].labels_synced, 6)
        payloads = {
            command[3]: json.loads(input_text or "{}")
            for command, input_text in runner.calls
            if command[:3] == ("gh", "api", "-X")
        }
        patch_payload = payloads["PATCH"]
        post_payload = payloads["POST"]
        expected_events = [
            "issues",
            "pull_request",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
        ]
        self.assertEqual(patch_payload["events"], expected_events)
        self.assertEqual(post_payload["events"], expected_events)
        self.assertEqual(patch_payload["config"]["url"], webhook_url)
        self.assertEqual(post_payload["config"]["url"], webhook_url)
        self.assertEqual(patch_payload["config"]["secret"], "secret")
        label_edits = [
            command for command, _input in runner.calls if command[:3] == ("gh", "label", "edit")
        ]
        label_creates = [
            command for command, _input in runner.calls if command[:3] == ("gh", "label", "create")
        ]
        self.assertIn(
            (
                "gh",
                "label",
                "edit",
                "every-code",
                "--repo",
                "cbusillo/code",
                "--color",
                "FBCA04",
                "--description",
                "Ask Every Code to work this issue.",
            ),
            label_edits,
        )
        self.assertIn(
            (
                "gh",
                "label",
                "create",
                "preview-ready",
                "--repo",
                "cbusillo/launchplane",
                "--color",
                "1D76DB",
                "--description",
                "Every Code preview is ready for source issue validation.",
            ),
            label_creates,
        )

    def test_sync_requires_explicit_webhook_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires webhook_url"):
            sync_every_code_webhooks(
                owner="cbusillo",
                webhook_secret="secret",
                webhook_url="",
                runner=_WebhookRunner(hooks={}),
            )

    def test_sync_redacts_github_cli_failure_in_operator_payload(self) -> None:
        runner = _FailingWebhookRunner(
            error_detail=(
                "github_pat_example_token_value\n"
                "Authorization: Bearer example-bearer-value\n"
                "https://operator:password@hooks.internal/private\n"
                "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN=example-token-value"
            )
        )

        results = sync_every_code_webhooks(
            owner="cbusillo",
            webhook_secret="secret",
            webhook_url="https://launchplane.example/v1/every-code/github-webhook",
            runner=runner,
        )

        failed = results[0]
        payload = json.dumps(failed.as_payload())
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.error_code, "github_cli_failed")
        self.assertTrue(failed.error_correlation_id.startswith("cpf-"))
        self.assertNotIn("\n", failed.error)
        for value in (
            "github_pat_example_token_value",
            "example-bearer-value",
            "operator:password",
            "hooks.internal",
            "example-token-value",
        ):
            self.assertNotIn(value, payload)

    def test_cli_sync_webhooks_requires_webhook_url_config(self) -> None:
        result = CliRunner().invoke(
            CLI_MAIN,
            ["every-code", "sync-webhooks", "--owner", "cbusillo"],
            env={"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": "secret"},
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "--webhook-url or LAUNCHPLANE_EVERY_CODE_WEBHOOK_URL is required", result.output
        )


if __name__ == "__main__":
    unittest.main()
