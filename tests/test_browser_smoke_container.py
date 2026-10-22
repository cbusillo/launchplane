import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


class BrowserSmokeContainerTests(unittest.TestCase):
    def run_smoke(
        self, smoke_exit: int, create_exit: int = 0, attach_exit: int = 0, pack_exit: int = 0
    ) -> tuple[int, str]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, body in {
                "node": "exit 0\n",
                "readlink": "echo 'net:[fixture-host]'\n",
                "corepack": """
[ "$COREPACK_ENABLE_NETWORK" = 0 ] || exit 91
[ "$PACK_EXIT" = 0 ] || exit "$PACK_EXIT"
printf fixture-pnpm > "$3"
""",
                "docker": """
case "$1" in
  create) if [ "$CREATE_EXIT" != 0 ]; then exit "$CREATE_EXIT"; fi
          for argument in "$@"; do
            case "$argument" in
              type=bind,source=*,target=/opt/browser-pnpm,readonly)
                source=${argument#type=bind,source=}
                source=${source%,target=/opt/browser-pnpm,readonly}
                [ "$(cat "$source/corepack.tgz")" = fixture-pnpm ] || exit 92
                hydrated=1 ;;
              COREPACK_ENABLE_NETWORK=0) offline=1 ;;
            esac
          done
          [ "$hydrated:$offline" = 1:1 ] || exit 93
          echo fixture-container ;;
  start) exit "$ATTACH_EXIT" ;;
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
                    "ATTACH_EXIT": str(attach_exit),
                    "CLEANUP_RECEIPT": str(receipt),
                    "PACK_EXIT": str(pack_exit),
                    "RUNNER_TEMP": directory,
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(list(root.glob("browser-pnpm.*")), [])
            return result.returncode, receipt.read_text().strip() if receipt.exists() else ""

    def test_failed_suite_fails_the_job_and_removes_its_container(self) -> None:
        status, removed = self.run_smoke(13)
        self.assertEqual(status, 13)
        self.assertEqual(removed, "fixture-container")

    def test_successful_suite_removes_its_container(self) -> None:
        status, removed = self.run_smoke(0)
        self.assertEqual(status, 0)
        self.assertEqual(removed, "fixture-container")

    def test_failed_attach_propagates_and_removes_its_container(self) -> None:
        status, removed = self.run_smoke(13, attach_exit=13)
        self.assertEqual(status, 13)
        self.assertEqual(removed, "fixture-container")

    def test_failed_creation_does_not_remove_an_unowned_container(self) -> None:
        status, removed = self.run_smoke(0, create_exit=17)
        self.assertEqual(status, 17)
        self.assertEqual(removed, "")

    def test_unavailable_cached_package_manager_stops_before_container_creation(self) -> None:
        status, removed = self.run_smoke(0, pack_exit=19)
        self.assertEqual(status, 19)
        self.assertEqual(removed, "")
