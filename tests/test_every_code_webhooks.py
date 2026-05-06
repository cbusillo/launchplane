import json
import subprocess
import unittest
from collections.abc import Sequence

from control_plane.every_code_webhooks import sync_every_code_webhooks


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
        raise AssertionError(f"unexpected command: {command}")


class EveryCodeWebhookSyncTests(unittest.TestCase):
    def test_sync_updates_existing_hook_and_creates_missing_hook(self) -> None:
        runner = _WebhookRunner(
            hooks={
                "cbusillo/code": [
                    {
                        "id": 12,
                        "config": {
                            "url": "https://launchplane.shinycomputers.com/v1/every-code/github-webhook"
                        },
                    }
                ],
                "cbusillo/launchplane": [],
            }
        )

        results = sync_every_code_webhooks(
            owner="cbusillo",
            webhook_secret="secret",
            runner=runner,
        )

        self.assertEqual([result.status for result in results], ["updated", "created"])
        self.assertEqual(results[0].hook_id, 12)
        self.assertEqual(results[1].hook_id, 34)
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
        self.assertEqual(patch_payload["config"]["secret"], "secret")


if __name__ == "__main__":
    unittest.main()
