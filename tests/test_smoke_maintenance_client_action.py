import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/setup-smoke-maintenance-client/dist/index.js")
ACTION_METADATA = Path(".github/actions/setup-smoke-maintenance-client/action.yml")


class SmokeMaintenanceClientActionTests(unittest.TestCase):
    def test_action_metadata_uses_supported_node_runtime(self) -> None:
        self.assertIn(
            "using: node24",
            ACTION_METADATA.read_text(encoding="utf-8"),
        )

    def run_setup_action(self, *, output_path: Path, github_output: Path) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the smoke maintenance action")

        env = os.environ.copy()
        env["INPUT_OUTPUT-PATH"] = str(output_path)
        env["GITHUB_OUTPUT"] = str(github_output)
        return subprocess.run(
            ["node", ACTION_ENTRYPOINT.as_posix()],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    def run_client_script(self, client_path: Path, script_body: str) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the smoke maintenance client")

        script = f"""
import * as client from {json.dumps(client_path.as_uri())};
{script_body}
"""
        return subprocess.run(
            ["node", "--input-type=module", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_setup_action_writes_importable_client_and_output(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            client_path = temporary_root / ".launchplane" / "smoke-client.mjs"
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=client_path,
                github_output=github_output,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(client_path.exists())
            self.assertIn(f"client-path={client_path}", github_output.read_text(encoding="utf-8"))

            import_result = self.run_client_script(
                client_path,
                """
console.log(Object.keys(client).sort().join(','));
""",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertIn("requestLaunchplaneSmokeMaintenance", import_result.stdout)
            self.assertIn("smokeMaintenanceIntentForAction", import_result.stdout)

    def test_client_builds_verireel_smoke_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const request = client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel-testing',
  instance: 'preview',
  action: 'grant-sponsored',
  email: 'creator@example.com',
  previewSlug: 'pr-42',
  timeoutSeconds: 300,
});
const idempotencyKey = client.buildLaunchplaneSmokeMaintenanceIdempotencyKey({
  product: 'verireel',
  context: 'verireel-testing',
  instance: 'preview',
  action: 'grant-sponsored',
  email: 'creator@example.com',
  previewSlug: 'pr-42',
});
console.log(JSON.stringify({ request, idempotencyKey }));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["request"]["routePath"], "/v1/drivers/verireel/app-maintenance")
        self.assertEqual(payload["request"]["payload"]["product"], "verireel")
        self.assertEqual(
            payload["request"]["payload"]["maintenance"],
            {
                "schema_version": 1,
                "context": "verireel-testing",
                "instance": "preview",
                "action": "grant-sponsored",
                "intent": "remote-e2e-grant-sponsored",
                "email": "creator@example.com",
                "preview_slug": "pr-42",
                "timeout_seconds": 300,
            },
        )
        self.assertEqual(
            payload["idempotencyKey"],
            "product-smoke-maintenance:verireel:verireel-testing:preview:grant-sponsored:remote-e2e-grant-sponsored::pr-42:creator@example.com",
        )

    def test_client_derives_stable_owner_smoke_intent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const request = client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'promote-owner',
  email: 'owner@example.com',
  timeoutSeconds: 300,
});
console.log(JSON.stringify(request.payload.maintenance));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["intent"],
            "owner-route-promote-owner",
        )

    def test_client_derives_stable_grant_smoke_intent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const request = client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'grant-sponsored',
  email: 'creator@example.com',
  timeoutSeconds: 300,
});
console.log(JSON.stringify(request.payload.maintenance));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["intent"],
            "stable-testing-remote-e2e-grant-sponsored",
        )

    def test_client_derives_stable_delete_smoke_intent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const request = client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'delete-user',
  email: 'owner@example.com',
  timeoutSeconds: 300,
});
console.log(JSON.stringify(request.payload.maintenance));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["intent"],
            "owner-route-delete-user",
        )

    def test_client_rejects_mismatched_smoke_intent(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel-testing',
  instance: 'preview',
  action: 'delete-user',
  intent: 'owner-route-delete-user',
  email: 'creator@example.com',
  previewSlug: 'pr-42',
  timeoutSeconds: 300,
});
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match derived intent 'remote-e2e-delete-user'", result.stderr)

    def test_client_rejects_unsupported_smoke_context_action(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel-testing',
  instance: 'preview',
  action: 'promote-owner',
  email: 'creator@example.com',
  previewSlug: 'pr-42',
  timeoutSeconds: 300,
});
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported VeriReel smoke maintenance context/action combination", result.stderr)

    def test_client_rejects_workflow_maintenance_action(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
client.buildLaunchplaneSmokeMaintenanceRequest({
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'migrate',
  timeoutSeconds: 300,
});
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported Launchplane smoke maintenance action: migrate", result.stderr)

    def test_client_posts_oidc_authenticated_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const calls = [];
const fetchImpl = async (url, init) => {
  calls.push({ url, init });
  if (url.startsWith('https://oidc.example/token')) {
    return new Response(JSON.stringify({ value: 'oidc-token' }), { status: 200 });
  }
  return new Response(JSON.stringify({
    status: 'accepted',
    result: { maintenance_status: 'pass', error_message: '' },
  }), { status: 200 });
};
const result = await client.requestLaunchplaneSmokeMaintenance({
  launchplaneUrl: 'https://launchplane.example',
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'promote-owner',
  email: 'owner@example.com',
  timeoutSeconds: 300,
}, {
  ACTIONS_ID_TOKEN_REQUEST_URL: 'https://oidc.example/token',
  ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'request-token',
}, fetchImpl);
console.log(JSON.stringify({ calls, result }));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        calls = payload["calls"]
        self.assertEqual(calls[0]["url"], "https://oidc.example/token?audience=launchplane.example")
        self.assertEqual(calls[1]["url"], "https://launchplane.example/v1/drivers/verireel/app-maintenance")
        self.assertEqual(calls[1]["init"]["headers"]["Authorization"], "Bearer oidc-token")
        self.assertEqual(
            calls[1]["init"]["headers"]["Idempotency-Key"],
            "product-smoke-maintenance:verireel:verireel:testing:promote-owner:owner-route-promote-owner:::owner@example.com",
        )
        self.assertEqual(json.loads(calls[1]["init"]["body"])["maintenance"]["action"], "promote-owner")
        self.assertEqual(payload["result"]["statusCode"], 200)

    def test_client_reports_non_json_http_failure_body(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const fetchImpl = async (url) => {
  if (url.startsWith('https://oidc.example/token')) {
    return new Response(JSON.stringify({ value: 'oidc-token' }), { status: 200 });
  }
  return new Response('gateway unavailable', { status: 502 });
};
await client.requestLaunchplaneSmokeMaintenance({
  launchplaneUrl: 'https://launchplane.example',
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'delete-user',
  email: 'owner@example.com',
  timeoutSeconds: 300,
}, {
  ACTIONS_ID_TOKEN_REQUEST_URL: 'https://oidc.example/token',
  ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'request-token',
}, fetchImpl);
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Launchplane smoke maintenance request failed with 502: gateway unavailable",
            result.stderr,
        )

    def test_client_fails_on_blocked_maintenance_result(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const fetchImpl = async (url) => {
  if (url.startsWith('https://oidc.example/token')) {
    return new Response(JSON.stringify({ value: 'oidc-token' }), { status: 200 });
  }
  return new Response(JSON.stringify({
    result: { maintenance_status: 'blocked', error_message: 'owner route blocked' },
  }), { status: 200 });
};
await client.requestLaunchplaneSmokeMaintenance({
  launchplaneUrl: 'https://launchplane.example',
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'delete-user',
  email: 'owner@example.com',
  timeoutSeconds: 300,
}, {
  ACTIONS_ID_TOKEN_REQUEST_URL: 'https://oidc.example/token',
  ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'request-token',
}, fetchImpl);
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner route blocked", result.stderr)

    def test_client_requires_github_oidc_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "smoke-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
await client.requestLaunchplaneSmokeMaintenance({
  launchplaneUrl: 'https://launchplane.example',
  product: 'verireel',
  context: 'verireel',
  instance: 'testing',
  action: 'delete-user',
  email: 'owner@example.com',
  timeoutSeconds: 300,
}, {}, async () => new Response('{}', { status: 200 }));
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub OIDC request environment is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
