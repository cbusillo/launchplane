import base64
from collections.abc import Callable
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
    @staticmethod
    def apply_request_and_workflow_run() -> tuple[dict[str, object], dict[str, object]]:
        return (
            {
                "schema_version": 1,
                "product": "repairshopr-sync",
                "instance": "prod",
                "artifact_id": "ghcr.io/cbusillo/repairshopr_api@sha256:" + "b" * 64,
                "source_git_ref": "2d66fb6b2708f975b1645ac912a5b576a9282853",
                "original_run_id": "29609495343",
                "original_run_attempt": "1",
                "expected_recovery_digest": "a" * 64,
                "reason": "Adopt the reviewed legacy deploy effect.",
            },
            {
                "id": 32213365281,
                "name": "Launchplane Recovery Apply Request",
                "path": ".github/workflows/launchplane-recovery-apply-request.yml",
                "conclusion": "success",
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": "7bbfa12578cea62cedb23f1770f9f5b7d9e288b2",
                "head_repository": {"full_name": "cbusillo/repairshopr_api"},
            },
        )

    def test_action_extracts_artifacts_without_external_zip_tools(self) -> None:
        source = DOWNLOAD_ENTRYPOINT.read_text(encoding="utf-8")

        self.assertNotIn("node:child_process", source)
        self.assertNotIn("execFileSync", source)
        self.assertNotIn('"unzip"', source)

    def run_action(
        self,
        *,
        request: dict[str, object] | None,
        output_path: Path,
        launchplane_url: str = "https://launchplane.example",
        request_file: Path | None = None,
        workflow_run: dict[str, object] | None = None,
        response_overrides: dict[str, object] | None = None,
        archive_compression: int = zipfile.ZIP_STORED,
        archive_transform: Callable[[bytes], bytes] | None = None,
        artifact_total_count: int = 1,
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
            with zipfile.ZipFile(archive, "w", compression=archive_compression) as zip_file:
                zip_file.writestr(
                    "launchplane-recovery-apply-request.json",
                    json.dumps(effective_request),
                )
            archive_bytes = archive.getvalue()
            if archive_transform is not None:
                archive_bytes = archive_transform(archive_bytes)
            artifact_archive_data = base64.b64encode(archive_bytes).decode("ascii")
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
      total_count: {artifact_total_count},
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

    def test_action_accepts_deflated_workflow_run_artifact(self) -> None:
        request, workflow_run = self.apply_request_and_workflow_run()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                archive_compression=zipfile.ZIP_DEFLATED,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_action_accepts_signed_zip_data_descriptor(self) -> None:
        request, workflow_run = self.apply_request_and_workflow_run()

        def add_data_descriptor(archive: bytes) -> bytes:
            end_offset = archive.rfind(b"PK\x05\x06")
            central_offset = int.from_bytes(archive[end_offset + 16 : end_offset + 20], "little")
            checksum_and_sizes = archive[central_offset + 16 : central_offset + 28]
            descriptor = b"PK\x07\x08" + checksum_and_sizes
            mutated = bytearray(
                archive[:central_offset] + descriptor + archive[central_offset:]
            )
            mutated[6:8] = (int.from_bytes(mutated[6:8], "little") | 8).to_bytes(2, "little")
            mutated[14:26] = b"\0" * 12
            moved_central_offset = central_offset + len(descriptor)
            mutated[moved_central_offset + 8 : moved_central_offset + 10] = (
                int.from_bytes(
                    mutated[moved_central_offset + 8 : moved_central_offset + 10],
                    "little",
                )
                | 8
            ).to_bytes(2, "little")
            moved_end_offset = end_offset + len(descriptor)
            mutated[moved_end_offset + 16 : moved_end_offset + 20] = moved_central_offset.to_bytes(
                4,
                "little",
            )
            return bytes(mutated)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                archive_transform=add_data_descriptor,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_action_rejects_hidden_zip_data_before_central_directory(self) -> None:
        request, workflow_run = self.apply_request_and_workflow_run()

        def add_hidden_data(archive: bytes) -> bytes:
            end_offset = archive.rfind(b"PK\x05\x06")
            central_offset = int.from_bytes(archive[end_offset + 16 : end_offset + 20], "little")
            hidden_data = b"hidden"
            mutated = bytearray(
                archive[:central_offset] + hidden_data + archive[central_offset:]
            )
            moved_end_offset = end_offset + len(hidden_data)
            mutated[moved_end_offset + 16 : moved_end_offset + 20] = (
                central_offset + len(hidden_data)
            ).to_bytes(4, "little")
            return bytes(mutated)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                archive_transform=add_hidden_data,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains unreferenced ZIP data", result.stderr)

    def test_action_rejects_artifact_with_invalid_crc(self) -> None:
        request, workflow_run = self.apply_request_and_workflow_run()

        def corrupt_request(archive: bytes) -> bytes:
            mutated = bytearray(archive)
            file_name_length = int.from_bytes(mutated[26:28], "little")
            extra_length = int.from_bytes(mutated[28:30], "little")
            data_offset = 30 + file_name_length + extra_length
            mutated[data_offset] ^= 1
            return bytes(mutated)

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                archive_transform=corrupt_request,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("request data is invalid", result.stderr)

    def test_action_rejects_truncated_artifact_listing(self) -> None:
        request, workflow_run = self.apply_request_and_workflow_run()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request_file = root / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            result = self.run_action(
                request=None,
                request_file=request_file,
                workflow_run=workflow_run,
                output_path=root / "github-output.txt",
                artifact_total_count=101,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one matching artifact", result.stderr)

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
      total_count: 1,
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
        self.assertIn(
            "?name=launchplane-recovery-apply-request-32213365281&per_page=100",
            evidence["calls"][0]["url"],
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
