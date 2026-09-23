import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from control_plane.release_review_github import owner_test_notes


ACTION = Path(".github/actions/owner-test-notes/index.mjs")


class OwnerTestNotesActionTests(unittest.TestCase):
    def test_action_matches_release_parser_and_never_executes_notes(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the Owner notes action")
        bodies = (
            "## Owner test notes\nNothing for the owner to test",
            "## Owner test notes\nCheck checkout.\n### Mobile\nCheck narrow layout.\n## Tests\nPassed.",
            "```\n## Owner test notes\nExample only\n```",
            "## Owner test notes\n\n## Tests\nPassed.",
            "## Owner test notes\n$(touch should-not-exist) `false` ${{ secrets.EXAMPLE }}",
            "## Owner test notes\nOne\n## Owner test notes\nTwo",
        )
        with TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            for body in bodies:
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
                    try:
                        expected = bool(owner_test_notes(body))
                    except ValueError:
                        expected = False
                    self.assertEqual(result.returncode == 0, expected, result.stderr)
                    self.assertFalse((Path(directory) / "should-not-exist").exists())
