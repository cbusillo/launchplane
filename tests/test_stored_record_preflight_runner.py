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
command_name="$1"
shift
readonly=false
candidate=false
forwarded=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --read-only|--read-only=true) readonly=true ;;
    --entrypoint|--entrypoint=*) candidate=true ;;
    -e|--env)
      shift
      if [[ "$1" == *=* ]]; then forwarded+="${1#*=}"; else forwarded+="${!1}"; fi
      ;;
    --env-file) shift; forwarded+="$(cat "$1")" ;;
    --env-file=*) forwarded+="$(cat "${1#*=}")" ;;
  esac
  shift
done
if [[ "$command_name" == run && "$candidate" == true ]]; then
  printf '%s' "$readonly" > "$PREFLIGHT_TEST_READONLY"
  printf '%s' "$forwarded" > "$PREFLIGHT_TEST_FORWARDED"
  exit 23
fi
exit 0
""")
            docker.chmod(0o755)
            unavailable_uuidgen = root / "uuidgen"
            unavailable_uuidgen.write_text("#!/bin/bash\nexit 127\n")
            unavailable_uuidgen.chmod(0o755)
            snapshot = root / "snapshot.json"
            snapshot.write_text("{}")
            log = root / "commands.log"
            readonly = root / "readonly"
            forwarded = root / "forwarded"
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
                    "PREFLIGHT_TEST_READONLY": str(readonly),
                    "PREFLIGHT_TEST_FORWARDED": str(forwarded),
                    "LIVE_TOKEN_SENTINEL": "never-forward",
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(readonly.read_text(), "true")
            self.assertNotIn("never-forward", forwarded.read_text())
            commands = [line.split("\t")[:-1] for line in log.read_text().splitlines()]
            network = next(args for args in commands if args[:2] == ["network", "create"])
            self.assertTrue(any(arg in {"--internal", "--internal=true"} for arg in network))
            self.assertNotIn("/var/run/docker.sock", log.read_text())
            self.assertTrue(any(args[0] == "rm" for args in commands))
            self.assertTrue(any(args[:2] == ["network", "rm"] for args in commands))

    def test_mutable_image_refuses_before_creating_any_resource(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "resource-created"
            docker = root / "docker"
            docker.write_text('#!/bin/bash\ntouch "$PREFLIGHT_TEST_MARKER"\nexit 0\n')
            docker.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "example/candidate:latest",
                    "/unused",
                    "example/postgres:latest",
                ],
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "PREFLIGHT_TEST_MARKER": str(marker),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
