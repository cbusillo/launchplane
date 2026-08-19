import base64
from io import BytesIO
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile


ACTION_ENTRYPOINT = Path(".github/actions/generic-web-deploy-recovery-dry-run/dist/index.mjs")
DOWNLOAD_ENTRYPOINT = Path(
    ".github/actions/generic-web-deploy-recovery-dry-run/dist/download-artifact.mjs"
)
ACTION_METADATA = Path(".github/actions/generic-web-deploy-recovery-dry-run/action.yml")
REUSABLE_WORKFLOW = Path(".github/workflows/reusable-generic-web-stable-deploy.yml")


class GenericWebDeployRecoveryActionTests(unittest.TestCase):
    def run_action(
        self,
        *,
        request: dict[str, object] | None,
        output_path: Path,
        launchplane_url: str = "https://launchplane.example",
        request_file: Path | None = None,
        workflow_run: dict[str, object] | None = None,
        response_overrides: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the recovery dry-run action")

        env = os.environ.copy()
        env.update(
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
                "GITHUB_OUTPUT": str(output_path),
                "INPUT_REQUEST-JSON": json.dumps(request) if request is not None else "",
            }
        )
        if launchplane_url:
            env["INPUT_LAUNCHPLANE-URL"] = launchplane_url
        effective_request = request
        artifact_archive_data = ""
        if request_file is not None:
            effective_request = json.loads(request_file.read_text(encoding="utf-8"))
            archive = BytesIO()
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr(
                    "launchplane-recovery-apply-request.json",
                    json.dumps(effective_request),
                )
            artifact_archive_data = base64.b64encode(archive.getvalue()).decode("ascii")
            event_path = output_path.parent / "event.json"
            event_path.write_text(
                json.dumps({"workflow_run": workflow_run}),
                encoding="utf-8",
            )
            env.update(
                {
                    "GITHUB_EVENT_NAME": "workflow_run",
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_API_URL": "https://api.github.example",
                    "GITHUB_REPOSITORY": "cbusillo/repairshopr_api",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "400",
                    "INPUT_GITHUB-TOKEN": "github-token",
                    "INPUT_EXPECTED-INSTANCE": "prod",
                    "INPUT_EXPECTED-PRODUCT": "repairshopr-sync",
                    "INPUT_WORKFLOW-RUN-ID": "32213365281",
                    "RUNNER_TEMP": str(output_path.parent),
                }
            )
        assert effective_request is not None
        if "expected_recovery_digest" in effective_request:
            response_status = 202
            response_payload = {
                "schema_version": 1,
                "status": "accepted",
                "mode": "apply",
                "trace_id": "recovery-apply-trace",
                "product": "repairshopr-sync",
                "context": "repairshopr-sync",
                "instance": "prod",
                "reservation_state": "completed",
                "reservation_attempt": 1,
                "recovery_action": "adopt_observed",
                "recovery_digest": "a" * 64,
                "provider_outcome": "present",
                "provider_status": "done",
                "retry_safe": False,
            }
        else:
            response_status = 200
            response_payload = {
                "recovery_digest": "a" * 64,
                "proposed_action": "retry_original_operation",
                "reservation_state": "reconcile_required",
                "provider_outcome": "absent",
                "provider_status": "missing",
                "retry_safe": True,
                "observed_at": "2026-08-16T22:00:00Z",
            }
        if response_overrides:
            response_payload.update(response_overrides)
        script = f"""
const calls = [];
const artifactArchive = Buffer.from('{artifact_archive_data}', 'base64');
global.fetch = async (url, init) => {{
  calls.push({{url, init}});
  if (url.includes('/actions/runs/32213365281/artifacts?')) {{
    return new Response(JSON.stringify({{
      artifacts: [{{
        id: 42,
        name: 'launchplane-recovery-apply-request-32213365281',
        expired: false,
        size_in_bytes: artifactArchive.length,
        archive_download_url: 'https://artifacts.example/request.zip'
      }}]
    }}), {{status: 200}});
  }}
  if (url === 'https://artifacts.example/request.zip') {{
    return new Response(artifactArchive, {{status: 200}});
  }}
  if (url.startsWith('https://oidc.example/token')) {{
    return new Response(JSON.stringify({{value: 'oidc-token'}}), {{status: 200}});
  }}
  return new Response(JSON.stringify({json.dumps(response_payload)}), {{status: {response_status}}});
}};
process.on('beforeExit', () => {{
  console.error(JSON.stringify(calls.map((call) => ({{
    url: call.url,
    method: call.init.method,
    headers: call.init.headers,
    body: call.init.body || ''
  }}))));
}});
await import('./{ACTION_ENTRYPOINT.as_posix()}');
"""
        return subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            env=env,
            text=True,
        )

    def test_action_metadata_declares_bounded_dry_run_and_apply_outputs(self) -> None:
        metadata = ACTION_METADATA.read_text(encoding="utf-8")

        self.assertIn("using: node24", metadata)
        self.assertIn("  expected-product:\n", metadata)
        self.assertIn("  expected-instance:\n", metadata)
        self.assertIn("default: ${{ github.event.workflow_run.id }}", metadata)
        self.assertIn("default: ${{ github.token }}", metadata)
        self.assertIn("main: dist/index.mjs", metadata)
        for output_name in (
            "status",
            "mode",
            "trace_id",
            "recovery_digest",
            "proposed_action",
            "reservation_state",
            "reservation_attempt",
            "recovery_action",
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

    def test_action_rejects_explicit_digest_bound_apply_before_oidc(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "a" * 64,
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        with TemporaryDirectory() as temporary_directory:
            result = self.run_action(
                request=request,
                output_path=Path(temporary_directory) / "github-output.txt",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires workflow-run artifact provenance", result.stderr)
        calls = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(calls, [])

    def test_action_validates_workflow_run_artifact_before_apply(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "a" * 64,
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        workflow_run = {
            "id": 32213365281,
            "name": "Launchplane Recovery Apply Request",
            "path": ".github/workflows/launchplane-recovery-apply-request.yml",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "7bbfa12578cea62cedb23f1770f9f5b7d9e288b2",
            "head_repository": {"full_name": "cbusillo/repairshopr_api"},
        }
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_directory = root / "request"
            request_directory.mkdir()
            request_file = request_directory / "launchplane-recovery-apply-request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            calls[-1]["url"],
            "https://launchplane.example/v1/admin/generic-web/deploy-recovery/apply",
        )

    def test_action_downloads_exact_source_run_artifact(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "a" * 64,
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr(
                "launchplane-recovery-apply-request.json",
                json.dumps(request),
            )
        archive_data = base64.b64encode(archive.getvalue()).decode("ascii")
        with TemporaryDirectory() as temporary_directory:
            script = f"""
const calls = [];
const archive = Buffer.from('{archive_data}', 'base64');
global.fetch = async (url, init) => {{
  calls.push({{url, headers: init.headers}});
  if (url.includes('/artifacts?')) {{
    return new Response(JSON.stringify({{
      artifacts: [{{
        id: 42,
        name: 'launchplane-recovery-apply-request-32213365281',
        expired: false,
        size_in_bytes: archive.length,
        archive_download_url: 'https://artifacts.example/request.zip'
      }}]
    }}), {{status: 200}});
  }}
  return new Response(archive, {{status: 200}});
}};
process.env.GITHUB_API_URL = 'https://api.github.example';
process.env.GITHUB_REPOSITORY = 'cbusillo/repairshopr_api';
process.env.GITHUB_RUN_ATTEMPT = '1';
process.env.GITHUB_RUN_ID = '400';
process.env.RUNNER_TEMP = '{temporary_directory}';
const module = await import('./{DOWNLOAD_ENTRYPOINT.as_posix()}');
const requestPath = await module.downloadArtifact('github-token', '32213365281');
console.log(JSON.stringify({{
  calls,
  requestPath,
  request: JSON.parse(await (await import('node:fs/promises')).readFile(requestPath, 'utf8'))
}}));
"""
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["request"], request)
        self.assertEqual(len(evidence["calls"]), 2)
        self.assertEqual(
            evidence["calls"][0]["headers"]["Authorization"],
            "Bearer github-token",
        )

    def test_action_rejects_invalid_workflow_run_provenance_before_oidc(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "a" * 64,
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        workflow_run = {
            "id": 32213365281,
            "name": "Launchplane Recovery Apply Request",
            "path": ".github/workflows/untrusted.yml",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "7bbfa12578cea62cedb23f1770f9f5b7d9e288b2",
            "head_repository": {"full_name": "cbusillo/repairshopr_api"},
        }
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_directory = root / "request"
            request_directory.mkdir()
            request_file = request_directory / "launchplane-recovery-apply-request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source workflow provenance is invalid", result.stderr)
        calls = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(calls, [])

    def test_action_rejects_apply_response_with_mismatched_digest(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "a" * 64,
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        workflow_run = {
            "id": 32213365281,
            "name": "Launchplane Recovery Apply Request",
            "path": ".github/workflows/launchplane-recovery-apply-request.yml",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "7bbfa12578cea62cedb23f1770f9f5b7d9e288b2",
            "head_repository": {"full_name": "cbusillo/repairshopr_api"},
        }
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_directory = root / "request"
            request_directory.mkdir()
            request_file = request_directory / "launchplane-recovery-apply-request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                response_overrides={"recovery_digest": "c" * 64},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not return adoption-only evidence", result.stderr)

    def test_action_rejects_apply_response_that_allows_provider_retry(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
            "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "a" * 64,
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        workflow_run = {
            "id": 32213365281,
            "name": "Launchplane Recovery Apply Request",
            "path": ".github/workflows/launchplane-recovery-apply-request.yml",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "7bbfa12578cea62cedb23f1770f9f5b7d9e288b2",
            "head_repository": {"full_name": "cbusillo/repairshopr_api"},
        }
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_directory = root / "request"
            request_directory.mkdir()
            request_file = request_directory / "launchplane-recovery-apply-request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                response_overrides={"retry_safe": True},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not return adoption-only evidence", result.stderr)

    def test_action_rejects_invalid_apply_digest_before_oidc(self) -> None:
        request = {
            "schema_version": 1,
            "product": "repairshopr-sync",
            "instance": "prod",
            "artifact_id": "artifact",
            "source_git_ref": "source",
            "original_run_id": "29609495343",
            "original_run_attempt": "1",
            "expected_recovery_digest": "not-a-digest",
            "reason": "Adopt the reviewed legacy deploy effect.",
        }
        with TemporaryDirectory() as temporary_directory:
            result = self.run_action(
                request=request,
                output_path=Path(temporary_directory) / "github-output.txt",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a lowercase SHA-256 digest", result.stderr)
        calls = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(calls, [])

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
            "actions: read",
            "recovery_request_json:",
            "github.event.inputs.original_run_id == ''",
            "github.event.inputs.original_run_id != ''",
            "github.event.workflow_run.name != 'Launchplane Recovery Request'",
            "github.event.workflow_run.name == 'Launchplane Recovery Request'",
            "github.event.workflow_run.path == '.github/workflows/launchplane-recovery-request.yml'",
            "name: Download staged recovery request",
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "launchplane-recovery-request-${{ github.event.workflow_run.id }}",
            "name: Resolve Launchplane recovery request",
            "ORIGINAL_RUN_ATTEMPT: ${{ github.event.inputs.original_run_attempt }}",
            "ORIGINAL_RUN_ID: ${{ github.event.inputs.original_run_id }}",
            "REASON: ${{ github.event.inputs.reason }}",
            "RECOVERY_ARTIFACT_RUN_ID: ${{ github.event.workflow_run.id }}",
            "Recovery request artifact must contain exactly one file.",
            "Recovery request artifact exceeds the size limit.",
            "name: Resolve provider evidence request",
            "name: Inspect exact provider evidence",
            "route-path: /v1/admin/generic-web/deploy-recovery/provider-evidence",
            "provider_evidence=provider_evidence",
            "provider_read_error_class=provider_read_error_class",
            "continue-on-error: true",
            "name: Request Launchplane recovery dry run",
            "uses: cbusillo/launchplane/.github/actions/"
            "generic-web-deploy-recovery-dry-run@b2055d2944626234664390d6fcd96975ded38511",
            "request-json: ${{ steps.request.outputs.request }}",
            "Recovery digest:",
            "Proposed action:",
            "Reservation state:",
            "Exact provider evidence:",
            "Provider read error class:",
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
