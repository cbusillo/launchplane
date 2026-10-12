import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


ROOT = Path(__file__).resolve().parents[1]


class StoredRecordPreflightQualificationTests(unittest.TestCase):
    def test_registry_login_does_not_change_post_job_context_and_cleanup_preserves_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            persistent = root / "persistent-registry"
            persistent.mkdir()
            (persistent / "config.json").write_text("persistent sentinel")
            github_env = root / "github-env"
            github_env.write_text("EXISTING=value\n")
            config_used = root / "config-used"
            docker = root / "docker"
            docker.write_text("""#!/bin/bash
if [[ "$1" == login ]]; then
  cat >/dev/null
  printf '%s' "$DOCKER_CONFIG" > "$PREFLIGHT_TEST_CONFIG_USED"
  echo '{}' > "$DOCKER_CONFIG/config.json"
fi
""")
            docker.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "GITHUB_TOKEN": "fixture-job-token",
                "GITHUB_ACTOR": "fixture-actor",
                "GITHUB_ENV": str(github_env),
                "DOCKER_CONFIG": str(persistent),
                "RUNNER_TEMP": str(root),
                "LAUNCHPLANE_PREFLIGHT_OWNER": "fixture:login",
                "PREFLIGHT_TEST_CONFIG_USED": str(config_used),
            }
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/deploy/authenticate-preflight-registry.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(github_env.read_text(), "EXISTING=value\n")
            temporary_config = Path(config_used.read_text())
            self.assertNotEqual(temporary_config, persistent)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/deploy/cleanup-stored-record-preflight.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(temporary_config.exists())
            self.assertEqual((persistent / "config.json").read_text(), "persistent sentinel")

    def test_supplied_image_requires_its_configured_repository_and_digest(self) -> None:
        script = ROOT / "scripts/deploy/validate-image-reference.py"
        repository = "ghcr.io/example/service"
        for image, accepted in (
            (repository + "@sha256:" + "a" * 64, True),
            ("ghcr.io/other/service@sha256:" + "a" * 64, False),
            (repository + ":latest", False),
            (repository + "@sha256:" + "A" * 64, False),
        ):
            with self.subTest(image=image):
                result = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "CANDIDATE_IMAGE": image,
                        "LAUNCHPLANE_IMAGE_REPOSITORY": repository,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

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
        step = next(
            step
            for step in workflow.steps("qualify-stored-record-preflight")
            if "run-stored-record-preflight.sh" in step.run
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text("""#!/bin/bash
for argument in "$@"; do
  if [[ "$argument" == "$CANDIDATE_IMAGE" ]]; then exit 23; fi
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
                    "LAUNCHPLANE_PREFLIGHT_OWNER": "fixture:qualification",
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 23, result.stderr)


if __name__ == "__main__":
    unittest.main()
