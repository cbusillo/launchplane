import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/generic-web-deploy-recovery-dry-run/dist/index.mjs")
ACTION_METADATA = Path(".github/actions/generic-web-deploy-recovery-dry-run/action.yml")
REUSABLE_WORKFLOW = Path(".github/workflows/reusable-generic-web-stable-deploy.yml")


class GenericWebDeployRecoveryActionTests(unittest.TestCase):
    def run_action(
        self,
        *,
        request: dict[str, object],
        output_path: Path,
        launchplane_url: str = "https://launchplane.example",
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the recovery dry-run action")

        env = os.environ.copy()
        env.update(
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
                "GITHUB_OUTPUT": str(output_path),
                "INPUT_REQUEST-JSON": json.dumps(request),
            }
        )
        if launchplane_url:
            env["INPUT_LAUNCHPLANE-URL"] = launchplane_url
        script = f"""
const calls = [];
global.fetch = async (url, init) => {{
  calls.push({{url, init}});
  if (url.startsWith('https://oidc.example/token')) {{
    return new Response(JSON.stringify({{value: 'oidc-token'}}), {{status: 200}});
  }}
  return new Response(JSON.stringify({{
    recovery_digest: '{"a" * 64}',
    proposed_action: 'retry_original_operation',
    reservation_state: 'reconcile_required',
    provider_outcome: 'absent',
    provider_status: 'missing',
    retry_safe: true,
    observed_at: '2026-08-16T22:00:00Z'
  }}), {{status: 200}});
}};
process.on('beforeExit', () => {{
  console.error(JSON.stringify(calls.map((call) => ({{
    url: call.url,
    method: call.init.method,
    headers: call.init.headers,
    body: call.init.body || ''
  }}))));
}});
import('./{ACTION_ENTRYPOINT.as_posix()}');
"""
        return subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            env=env,
            text=True,
        )

    def test_action_metadata_is_dry_run_only_and_declares_bounded_outputs(self) -> None:
        metadata = ACTION_METADATA.read_text(encoding="utf-8")

        self.assertIn("using: node24", metadata)
        self.assertNotIn("apply", metadata.lower())
        for output_name in (
            "recovery_digest",
            "proposed_action",
            "reservation_state",
            "provider_outcome",
            "provider_status",
            "retry_safe",
            "observed_at",
        ):
            with self.subTest(output_name=output_name):
                self.assertIn(f"  {output_name}:\n", metadata)

    def test_action_reconstructs_exact_legacy_request_and_projects_evidence(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "reason": "Inspect the legacy deploy reservation.",
        }
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "github-output.txt"
            result = self.run_action(request=request, output_path=output_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = json.loads(result.stderr.splitlines()[-1])
            self.assertEqual(len(calls), 2)
            launchplane_call = calls[1]
            self.assertEqual(
                launchplane_call["url"],
                "https://launchplane.example/v1/admin/generic-web/deploy-recovery/dry-run",
            )
            self.assertEqual(launchplane_call["method"], "POST")
            self.assertEqual(
                launchplane_call["headers"]["Idempotency-Key"],
                "generic-web-stable-deploy:repairshopr-sync:prod:29609495343:1",
            )
            self.assertEqual(
                json.loads(launchplane_call["body"]),
                {
                    "schema_version": 1,
                    "product": "repairshopr-sync",
                    "instance": "prod",
                    "original_deploy": {
                        "schema_version": 1,
                        "product": "repairshopr-sync",
                        "deploy": {
                            "schema_version": 1,
                            "product": "repairshopr-sync",
                            "instance": "prod",
                            "artifact_id": request["artifact_id"],
                            "source_git_ref": request["source_git_ref"],
                        },
                    },
                    "reason": request["reason"],
                },
            )
            outputs = output_path.read_text(encoding="utf-8")
            for output_name, output_value in (
                ("recovery_digest", "a" * 64),
                ("proposed_action", "retry_original_operation"),
                ("reservation_state", "reconcile_required"),
                ("provider_outcome", "absent"),
                ("provider_status", "missing"),
                ("retry_safe", "true"),
                ("observed_at", "2026-08-16T22:00:00Z"),
            ):
                with self.subTest(output_name=output_name):
                    self.assertIn(f"{output_name}<<", outputs)
                    self.assertIn(f"\n{output_value}\n", outputs)

    def test_action_rejects_unknown_request_fields_before_oidc(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "artifact",
            "source_git_ref": "source",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "reason": "Inspect the legacy deploy reservation.",
            "apply": True,
        }
        with TemporaryDirectory() as temporary_directory:
            result = self.run_action(
                request=request,
                output_path=Path(temporary_directory) / "github-output.txt",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported fields: apply", result.stderr)
        calls = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(calls, [])

    def test_action_accepts_launchplane_url_from_connector_envelope(self) -> None:
        request = {
            "schema_version": 1,
            "launchplane_url": "https://launchplane.example",
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "artifact",
            "source_git_ref": "source",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "reason": "Inspect the legacy deploy reservation.",
        }
        with TemporaryDirectory() as temporary_directory:
            result = self.run_action(
                request=request,
                output_path=Path(temporary_directory) / "github-output.txt",
                launchplane_url="",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(
            calls[1]["url"],
            "https://launchplane.example/v1/admin/generic-web/deploy-recovery/dry-run",
        )
        self.assertNotIn("launchplane_url", json.loads(calls[1]["body"]))

    def test_stable_deploy_reusable_workflow_has_dry_run_only_recovery_mode(self) -> None:
        workflow = REUSABLE_WORKFLOW.read_text(encoding="utf-8")

        required_fragments = (
            "recovery_request_json:",
            "github.event.inputs.original_run_id == ''",
            "github.event.inputs.original_run_id != ''",
            "name: Resolve Launchplane recovery request",
            "ORIGINAL_RUN_ATTEMPT: ${{ github.event.inputs.original_run_attempt }}",
            "ORIGINAL_RUN_ID: ${{ github.event.inputs.original_run_id }}",
            "REASON: ${{ github.event.inputs.reason }}",
            "name: Request Launchplane recovery dry run",
            "uses: cbusillo/launchplane/.github/actions/"
            "generic-web-deploy-recovery-dry-run@b2055d2944626234664390d6fcd96975ded38511",
            "request-json: ${{ steps.request.outputs.request }}",
            "Recovery digest:",
            "Proposed action:",
            "Reservation state:",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, workflow)

        self.assertNotIn("generic-web/deploy-recovery/apply", workflow)
        self.assertNotIn("expected_recovery_digest", workflow)
        self.assertNotIn(
            "launchplane-url: >-\n            ${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",
            workflow.split("  recovery-dry-run:", maxsplit=1)[1],
        )


if __name__ == "__main__":
    unittest.main()
