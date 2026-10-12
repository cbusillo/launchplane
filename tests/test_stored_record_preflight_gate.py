import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

import yaml


class StoredRecordPreflightGateTests(unittest.TestCase):
    def test_embedded_gate_propagates_candidate_failure_through_tee(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/deploy-launchplane.yml").read_text())
        script = next(
            step["run"]
            for step in workflow["jobs"]["deploy"]["steps"]
            if "stored-record-preflight.sh" in step.get("run", "")
        )
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            docker = temporary / "docker"
            docker.write_text("""#!/bin/bash
for argument in "$@"; do
  if [[ "$argument" == --entrypoint || "$argument" == --entrypoint=* ]]; then
    exit 23
  fi
done
exit 0
""")
            docker.chmod(0o755)
            (temporary / "launchplane-stored-record-snapshot.json").write_text("{}")
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{temporary}:{os.environ['PATH']}",
                    "CANDIDATE_IMAGE": "example/candidate@sha256:" + "a" * 64,
                    "RUNNER_TEMP": str(temporary),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 23, result.stderr)


if __name__ == "__main__":
    unittest.main()
