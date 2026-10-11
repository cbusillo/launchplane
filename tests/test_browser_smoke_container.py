import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


class BrowserSmokeContainerTests(unittest.TestCase):
    def run_smoke(self, smoke_exit: int, create_exit: int = 0) -> tuple[int, str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in {
                "node": "exit 0\n",
                "readlink": "echo 'net:[fixture-host]'\n",
                "docker": """
case "$1" in
  create) if [ "$CREATE_EXIT" != 0 ]; then exit "$CREATE_EXIT"; fi
          echo fixture-container ;;
  start) exit 0 ;;
  inspect) echo "$SMOKE_EXIT" ;;
  rm) printf '%s\\n' "$3" > "$CLEANUP_RECEIPT" ;;
  *) exit 99 ;;
esac
""",
            }.items():
                command = root / name
                command.write_text("#!/bin/sh\n" + body)
                command.chmod(0o755)
            receipt = root / "cleanup"
            result = subprocess.run(
                ["bash", "scripts/run-browser-smoke-container.sh"],
                env=os.environ
                | {
                    "PATH": directory + os.pathsep + os.environ["PATH"],
                    "BROWSER_SMOKE_IMAGE": "fixture/browser:1",
                    "SMOKE_EXIT": str(smoke_exit),
                    "CREATE_EXIT": str(create_exit),
                    "CLEANUP_RECEIPT": str(receipt),
                },
                capture_output=True,
                text=True,
            )
            return result.returncode, receipt.read_text().strip() if receipt.exists() else ""

    def test_failed_suite_fails_the_job_and_removes_its_container(self) -> None:
        status, removed = self.run_smoke(13)
        self.assertEqual(status, 13)
        self.assertEqual(removed, "fixture-container")

    def test_successful_suite_removes_its_container(self) -> None:
        status, removed = self.run_smoke(0)
        self.assertEqual(status, 0)
        self.assertEqual(removed, "fixture-container")

    def test_failed_creation_does_not_remove_an_unowned_container(self) -> None:
        status, removed = self.run_smoke(0, create_exit=17)
        self.assertEqual(status, 17)
        self.assertEqual(removed, "")
