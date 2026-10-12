import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


ROOT = Path(__file__).resolve().parents[1]


class StoredRecordPreflightQualificationTests(unittest.TestCase):
    def test_endpoint_uses_existing_health_origin_and_refuses_output_injection(self) -> None:
        script = ROOT / "scripts/deploy/resolve-service-endpoint.py"
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = {
                **os.environ,
                "GITHUB_OUTPUT": str(output),
                "LAUNCHPLANE_DEPLOY_HEALTH_URLS": "https://service.example:8443/health,https://other.example/health",
                "LAUNCHPLANE_PUBLIC_URL": "https://public.example/",
            }
            result = subprocess.run(
                [sys.executable, str(script)], env=env, capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["service_url"], "https://service.example:8443")
            self.assertEqual(values["service_audience"], "service.example")
            output.unlink()
            for invalid in ("https://service.example/x\ninjected=true", "relative/health"):
                with self.subTest(invalid=invalid):
                    result = subprocess.run(
                        [sys.executable, str(script)],
                        env={**env, "LAUNCHPLANE_DEPLOY_HEALTH_URLS": invalid},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())

    def test_qualification_executes_shared_wrapper_and_preserves_candidate_failure(self) -> None:
        workflow = load_workflow(".github/workflows/deploy-launchplane.yml")
        step = workflow.step_named(
            "qualify-stored-record-preflight", "Qualify isolated candidate on deploy runner"
        )
        assert step is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text("""#!/bin/bash
for argument in "$@"; do
  if [[ "$argument" == --entrypoint || "$argument" == --entrypoint=* ]]; then exit 23; fi
done
exit 0
""")
            docker.chmod(0o755)
            (root / "launchplane-stored-record-snapshot.json").write_text("{}")
            result = subprocess.run(
                ["bash", "-c", step.run],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "CANDIDATE_IMAGE": "example/candidate@sha256:" + "a" * 64,
                    "RUNNER_TEMP": str(root),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 23, result.stderr)


if __name__ == "__main__":
    unittest.main()
