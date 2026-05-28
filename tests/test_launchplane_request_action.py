import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/launchplane-request/dist/index.js")
ACTION_METADATA = Path(".github/actions/launchplane-request/action.yml")


class LaunchplaneRequestActionTests(unittest.TestCase):
    def test_action_metadata_uses_supported_node_runtime(self) -> None:
        self.assertIn(
            "using: node24",
            ACTION_METADATA.read_text(encoding="utf-8"),
        )

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

    def test_writes_mapped_response_value_to_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            response_output_path = Path(temporary_directory) / "result.json"
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/generic-web/preview-refresh",
                    "payload": '{"schema_version":1,"product":"sellyouroutboard"}',
                    "response-output-file": str(response_output_path),
                    "response-output-path": "result",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(response_output_path.read_text(encoding="utf-8")),
                {
                    "refresh_status": "pass",
                    "error_message": "",
                    "application_id": "app-123",
                },
            )

    def test_overlays_payload_fields_before_request(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/prod-rollback",
                "payload": '{"schema_version":1,"product":"odoo","rollback":{"schema_version":1}}',
                "payload-fields": "\n".join(
                    [
                        "rollback.context=cm",
                        "rollback.instance=prod",
                        "rollback.reason=manual rollback requested",
                        "rollback.wait_for_deploy=false",
                        "rollback.timeout_seconds=300",
                    ]
                ),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        payload = json.loads(calls[1]["body"])
        self.assertEqual(payload["rollback"]["context"], "cm")
        self.assertEqual(payload["rollback"]["reason"], "manual rollback requested")
        self.assertIs(payload["rollback"]["wait_for_deploy"], False)
        self.assertEqual(payload["rollback"]["timeout_seconds"], 300)

    def test_overlays_payload_json_files_before_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "artifact.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-cm-123",
                        "source_commit": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_action(
                inputs={
                    "launchplane-url": "https://launchplane.example",
                    "route-path": "/v1/drivers/odoo/artifact-publish",
                    "payload": '{"schema_version":1,"product":"odoo","publish":{"schema_version":1,"context":"cm"}}',
                    "payload-fields": "publish.instance=testing",
                    "payload-json-files": f"publish.manifest={manifest_path}",
                },
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.strip().splitlines()[-1])
        payload = json.loads(calls[1]["body"])
        self.assertEqual(payload["publish"]["instance"], "testing")
        self.assertEqual(payload["publish"]["manifest"]["artifact_id"], "artifact-cm-123")
        self.assertEqual(payload["publish"]["manifest"]["source_commit"], "abc123")

    def test_rejects_payload_fields_without_object_payload(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/prod-rollback",
                "payload-fields": "rollback.context=cm",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload-fields requires payload or payload-file", result.stderr)

    def test_rejects_payload_json_files_without_object_payload(self) -> None:
        result = self.run_action(
            inputs={
                "launchplane-url": "https://launchplane.example",
                "route-path": "/v1/drivers/odoo/artifact-publish",
                "payload-json-files": "publish.manifest=artifact.json",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload-json-files requires payload or payload-file", result.stderr)

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
