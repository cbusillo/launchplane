import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

ACTION = Path(".github/actions/owner-test-notes/index.mjs")


class OwnerTestNotesActionTests(unittest.TestCase):
    def test_action_checks_presence_without_executing_or_judging_notes(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the Owner notes action")
        cases = (
            ("## Owner test notes\nNothing for the owner to test", True),
            ("## Summary\nUpdated checkout.", False),
            ("```\n## Owner test notes\nExample only\n```", False),
            ("## Owner test notes\n\n## Tests\nPassed.", False),
            ("## Owner test notes\n$(touch should-not-exist) `false` ${{ secrets.EXAMPLE }}", True),
            ("## Owner test notes\nOne\n## Owner test notes\nTwo", True),
        )
        with TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            for body, expected in cases:
                with self.subTest(body=body):
                    event_path.write_text(json.dumps({"pull_request": {"body": body}}))
                    result = subprocess.run(
                        ["node", str(ACTION.resolve())],
                        env={
                            **os.environ,
                            "GITHUB_EVENT_NAME": "pull_request",
                            "GITHUB_EVENT_PATH": str(event_path),
                        },
                        cwd=directory,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, expected, result.stderr)
                    self.assertFalse((Path(directory) / "should-not-exist").exists())
