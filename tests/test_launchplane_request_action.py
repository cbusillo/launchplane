import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/launchplane-request/dist/index.js")


class LaunchplaneRequestActionTests(unittest.TestCase):
    def run_action(
        self,
        *,
        inputs: dict[str, str],
        environment: dict[str, str] | None = None,
        output_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the Launchplane request action")

        env = os.environ.copy()
        env.update(
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
            }
        )
        if output_path is not None:
            env["GITHUB_OUTPUT"] = str(output_path)
        if environment:
            env.update(environment)
        for name, value in inputs.items():
            env[f"INPUT_{name.upper()}"] = value

        script = f"""
const calls = [];
let launchplaneRequestCount = 0;
global.fetch = async (url, init) => {{
  calls.push({{url, init}});
  if (url.startsWith('https://oidc.example/token')) {{
    return new Response(JSON.stringify({{value: 'oidc-token'}}), {{status: 200}});
  }}
  launchplaneRequestCount += 1;
  const configuredStatuses = String(process.env.TEST_REFRESH_STATUSES || '').split(',').filter(Boolean);
  const refreshStatus = configuredStatuses[launchplaneRequestCount - 1] || process.env.TEST_REFRESH_STATUS || 'pass';
  return new Response(JSON.stringify({{
    ok: true,
    result: {{
      refresh_status: refreshStatus,
      error_message: process.env.TEST_ERROR_MESSAGE || '',
      application_id: 'app-123'
    }}
  }}), {{status: Number(process.env.TEST_STATUS || '200')}});
}};
require('./{ACTION_ENTRYPOINT.as_posix()}');
process.on('beforeExit', () => {{
  console.error(JSON.stringify(calls.map((call) => ({{
    url: call.url,
    method: call.init.method,
    headers: call.init.headers,
    body: call.init.body || ''
  }}))));
}});
"""
        return subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    def test_posts_oidc_authenticated_json_with_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(
                output_path=output_path,
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/generic-web/preview-refresh",
                    "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                    "idempotency-key": "generic-web-preview-refresh:sellyouroutboard:42:sha",
                    "output-paths": "application_id=result.application_id",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            self.assertEqual(
                calls[0]["url"],
                "https://oidc.example/token?audience=launchplane.example",
            )
            self.assertEqual(
                calls[1]["url"],
                "https://launchplane.example/v1/drivers/generic-web/preview-refresh",
            )
            self.assertEqual(calls[1]["headers"]["Authorization"], "Bearer oidc-token")
            self.assertEqual(
                calls[1]["headers"]["Idempotency-Key"],
                "generic-web-preview-refresh:sellyouroutboard:42:sha",
            )
            self.assertEqual(json.loads(calls[1]["body"])["product"], "sellyouroutboard")
            self.assertIn("application_id<<", output_path.read_text(encoding="utf-8"))

    def test_fails_when_driver_result_status_is_configured_as_failure(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/generic-web/preview-refresh",
                "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
            },
            environment={
                "TEST_REFRESH_STATUS": "blocked",
                "TEST_ERROR_MESSAGE": "Preview refresh blocked.",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Preview refresh blocked.", result.stderr)

    def test_polls_until_driver_status_leaves_pending(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(
                output_path=output_path,
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/generic-web/preview-refresh",
                    "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                    "poll-result-path": "result.refresh_status",
                    "poll-result-statuses": "pending",
                    "poll-interval-ms": "1",
                    "poll-timeout-ms": "1000",
                    "output-paths": "refresh_status=result.refresh_status",
                },
                environment={"TEST_REFRESH_STATUSES": "pending,pending,pass"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.strip().splitlines()[-1])
            launchplane_calls = [
                call
                for call in calls
                if call["url"]
                == "https://launchplane.example/v1/drivers/generic-web/preview-refresh"
            ]
            self.assertEqual(len(launchplane_calls), 3)
            self.assertIn("refresh_status<<", output_path.read_text(encoding="utf-8"))

    def test_fails_when_polling_times_out(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/generic-web/preview-refresh",
                "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                "poll-result-path": "result.refresh_status",
                "poll-result-statuses": "pending",
                "poll-interval-ms": "1",
                "poll-timeout-ms": "1",
            },
            environment={"TEST_REFRESH_STATUS": "pending"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for Launchplane result", result.stderr)


if __name__ == "__main__":
    unittest.main()
