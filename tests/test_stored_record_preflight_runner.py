import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts/deploy/stored-record-preflight.sh"


class StoredRecordPreflightRunnerTests(unittest.TestCase):
    def test_candidate_failure_propagates_and_rehearsal_resources_are_cleaned(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text("""#!/bin/bash
printf '%s\\t' "$@" >> "$PREFLIGHT_TEST_LOG"
printf '\\n' >> "$PREFLIGHT_TEST_LOG"
if [[ "$1" == run && "$2" == --rm ]]; then
  exit 23
fi
exit 0
""")
            docker.chmod(0o755)
            snapshot = root / "snapshot.json"
            snapshot.write_text("{}")
            log = root / "commands.log"
            result = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "example/candidate@sha256:" + "a" * 64,
                    str(snapshot),
                    "example/postgres@sha256:" + "b" * 64,
                ],
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "PREFLIGHT_TEST_LOG": str(log),
                    "LIVE_TOKEN_SENTINEL": "never-forward",
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            commands = [line.split("\t")[:-1] for line in log.read_text().splitlines()]
            network = next(args for args in commands if args[:2] == ["network", "create"])
            self.assertIn("--internal", network)
            candidate = next(args for args in commands if args[:2] == ["run", "--rm"])
            self.assertIn("--read-only", candidate)
            self.assertIn("control_plane.stored_record_preflight", candidate)
            self.assertNotIn("never-forward", " ".join(candidate))
            self.assertNotIn("/var/run/docker.sock", " ".join(candidate))
            self.assertTrue(any(args[:2] == ["rm", "-f"] for args in commands))
            self.assertEqual(commands[-1][:2], ["network", "rm"])

    def test_mutable_image_refuses_before_creating_any_resource(self) -> None:
        result = subprocess.run(
            ["bash", str(RUNNER), "example/candidate:latest", "/unused", "example/postgres:latest"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable", result.stderr)


if __name__ == "__main__":
    unittest.main()
