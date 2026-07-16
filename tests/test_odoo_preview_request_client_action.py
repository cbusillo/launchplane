import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/setup-odoo-preview-request-client/dist/index.js")
ACTION_METADATA = Path(".github/actions/setup-odoo-preview-request-client/action.yml")


class OdooPreviewRequestClientActionTests(unittest.TestCase):
    def test_action_metadata_uses_supported_node_runtime(self) -> None:
        self.assertIn(
            "using: node24",
            ACTION_METADATA.read_text(encoding="utf-8"),
        )

    def run_setup_action(
        self,
        *,
        output_path: Path,
        github_output: Path,
        inputs: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the Odoo preview request action")

        env = os.environ.copy()
        env["INPUT_OUTPUT-PATH"] = str(output_path)
        env["GITHUB_OUTPUT"] = str(github_output)
        for name, value in (inputs or {}).items():
            env[f"INPUT_{name.replace(' ', '_').upper()}"] = value
        return subprocess.run(
            ["node", ACTION_ENTRYPOINT.as_posix()],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

    def read_outputs(self, github_output: Path) -> dict[str, str]:
        outputs: dict[str, str] = {}
        lines = github_output.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            if "<<" in line:
                name, delimiter = line.split("<<", 1)
                index += 1
                values: list[str] = []
                while index < len(lines) and lines[index] != delimiter:
                    values.append(lines[index])
                    index += 1
                outputs[name] = "\n".join(values)
            elif "=" in line:
                name, value = line.split("=", 1)
                outputs[name] = value
            index += 1
        return outputs

    def run_client_script(
        self, client_path: Path, script_body: str
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the Odoo preview request client")

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
            client_path = temporary_root / ".launchplane" / "odoo-preview-client.mjs"
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
            self.assertIn("buildOdooPreviewArtifactPublishInputsRequest", import_result.stdout)
            self.assertIn("buildOdooPreviewApplyInputsRequest", import_result.stdout)
            self.assertIn("buildOdooPreviewApplyRequest", import_result.stdout)

    def test_setup_action_renders_artifact_publish_request_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            client_path = temporary_root / "odoo-preview-client.mjs"
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=client_path,
                github_output=github_output,
                inputs={
                    "REQUEST-KIND": "artifact-publish-inputs",
                    "PRODUCT": "odoo-tenant-cm-website",
                    "CONTEXT": "cm_website",
                    "PR-NUMBER": "42",
                    "SOURCE-GIT-REF": "abc123",
                    "RUN-ID": "456",
                    "RUN-ATTEMPT": "2",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs["client-path"], str(client_path))
            self.assertEqual(outputs["route-path"], "/v1/drivers/odoo/artifact-publish-inputs")
            self.assertEqual(outputs["fail-result-paths"], "result.input_status")
            self.assertEqual(outputs["response-output-path"], "result")
            self.assertEqual(
                outputs["idempotency-key"],
                "odoo-artifact-publish-inputs:odoo-tenant-cm-website:cm_website:testing:preview-42-run-456-attempt-2",
            )
            self.assertEqual(outputs["payload-json-files"], "")
            self.assertEqual(
                json.loads(outputs["payload"]),
                {
                    "schema_version": 1,
                    "product": "odoo-tenant-cm-website",
                    "inputs": {
                        "schema_version": 1,
                        "context": "cm_website",
                        "instance": "testing",
                        "pr_number": 42,
                        "isolated": True,
                        "source_git_ref": "abc123",
                    },
                },
            )

    def test_setup_action_renders_preview_apply_inputs_request_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=temporary_root / "odoo-preview-client.mjs",
                github_output=github_output,
                inputs={
                    "REQUEST-KIND": "preview-apply-inputs",
                    "PRODUCT": "odoo-tenant-cm-website",
                    "CONTEXT": "cm_website",
                    "PR-NUMBER": "42",
                    "PREVIEW-SLUG": "preview-42",
                    "SOURCE-GIT-REF": "abc123",
                    "MANIFEST-FILE": "/tmp/preview-artifact.json",
                    "RUN-ID": "456",
                    "RUN-ATTEMPT": "2",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs["route-path"], "/v1/drivers/odoo/preview-apply-inputs")
            self.assertEqual(
                outputs["payload-json-files"], "inputs.manifest=/tmp/preview-artifact.json"
            )
            self.assertEqual(outputs["fail-result-paths"], "result.status")
            self.assertEqual(outputs["response-output-path"], "result.dry_run_plan")
            self.assertEqual(
                outputs["idempotency-key"],
                "odoo-preview-apply-inputs:odoo-tenant-cm-website:preview-42:abc123:inputs-run-456-attempt-2",
            )
            payload = json.loads(outputs["payload"])
            self.assertNotIn("preview_slug", payload["inputs"])

    def test_setup_action_renders_destroy_apply_request_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=temporary_root / "odoo-preview-client.mjs",
                github_output=github_output,
                inputs={
                    "REQUEST-KIND": "preview-apply",
                    "PRODUCT": "odoo-tenant-cm-website",
                    "CONTEXT": "cm_website",
                    "OPERATION": "destroy",
                    "PR-NUMBER": "42",
                    "DRY-RUN-PLAN-FILE": "/tmp/destroy-dry-run.json",
                    "PLAN-ID": "odoo-preview-plan-test",
                    "WAIT-FOR-DEPLOY": "false",
                    "SMOKE-CHECK": "false",
                    "RUN-ID": "456",
                    "RUN-ATTEMPT": "2",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs["route-path"], "/v1/drivers/odoo/preview-apply")
            self.assertEqual(
                outputs["payload-json-files"], "apply.dry_run_plan=/tmp/destroy-dry-run.json"
            )
            self.assertEqual(outputs["fail-result-paths"], "result.status")
            self.assertEqual(outputs["response-output-path"], "")
            self.assertEqual(outputs["idempotency-key"], "odoo-preview-plan-test")
            self.assertEqual(
                json.loads(outputs["payload"])["apply"],
                {"timeout_seconds": 600, "wait_for_deploy": False, "smoke_check": False},
            )

    def test_setup_action_renders_refresh_apply_request_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=temporary_root / "odoo-preview-client.mjs",
                github_output=github_output,
                inputs={
                    "REQUEST-KIND": "preview-apply",
                    "PRODUCT": "odoo-tenant-cm-website",
                    "CONTEXT": "cm_website",
                    "PR-NUMBER": "42",
                    "PREVIEW-SLUG": "preview-42",
                    "SOURCE-GIT-REF": "abc123",
                    "DRY-RUN-PLAN-FILE": "/tmp/dry-run.json",
                    "PLAN-ID": "odoo-preview-plan-test",
                    "MANIFEST-FILE": "/tmp/preview-artifact.json",
                    "RUN-ID": "456",
                    "RUN-ATTEMPT": "2",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs["route-path"], "/v1/drivers/odoo/preview-apply")
            self.assertEqual(
                outputs["payload-json-files"],
                "apply.dry_run_plan=/tmp/dry-run.json\napply.manifest=/tmp/preview-artifact.json",
            )
            self.assertEqual(outputs["fail-result-paths"], "result.status")
            self.assertEqual(outputs["response-output-path"], "")
            self.assertEqual(outputs["idempotency-key"], "odoo-preview-plan-test")
            self.assertEqual(
                json.loads(outputs["payload"]),
                {
                    "schema_version": 1,
                    "product": "odoo-tenant-cm-website",
                    "apply": {"timeout_seconds": 600, "wait_for_deploy": True},
                },
            )

    def test_setup_action_rejects_unknown_request_kind(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            result = self.run_setup_action(
                output_path=Path(temporary_directory) / "odoo-preview-client.mjs",
                github_output=Path(temporary_directory) / "out",
                inputs={"REQUEST-KIND": "preview-url"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("request-kind must be", result.stderr)

    def test_setup_action_render_mode_rejects_missing_required_facts(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            result = self.run_setup_action(
                output_path=Path(temporary_directory) / "odoo-preview-client.mjs",
                github_output=Path(temporary_directory) / "out",
                inputs={
                    "REQUEST-KIND": "preview-apply-inputs",
                    "PRODUCT": "odoo-tenant-cm-website",
                    "CONTEXT": "cm_website",
                    "PR-NUMBER": "42",
                    "SOURCE-GIT-REF": "abc123",
                    "RUN-ID": "456",
                    "RUN-ATTEMPT": "2",
                },
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refresh apply inputs require manifestFile", result.stderr)

    def test_client_builds_artifact_publish_inputs_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
const request = client.buildOdooPreviewArtifactPublishInputsRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  sourceGitRef: 'abc123',
  runId: '456',
  runAttempt: '2',
});
console.log(JSON.stringify(request));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(result.stdout)
        self.assertEqual(request["routePath"], "/v1/drivers/odoo/artifact-publish-inputs")
        self.assertEqual(
            request["payload"],
            {
                "schema_version": 1,
                "product": "odoo-tenant-cm-website",
                "inputs": {
                    "schema_version": 1,
                    "context": "cm_website",
                    "instance": "testing",
                    "pr_number": 42,
                    "isolated": True,
                    "source_git_ref": "abc123",
                },
            },
        )
        self.assertEqual(request["failResultPathsInput"], "result.input_status")
        self.assertEqual(json.loads(request["payloadInput"]), request["payload"])
        self.assertEqual(request["responseOutputPath"], "result")
        self.assertEqual(
            request["idempotencyKey"],
            "odoo-artifact-publish-inputs:odoo-tenant-cm-website:cm_website:testing:preview-42-run-456-attempt-2",
        )

    def test_client_builds_apply_inputs_request_without_preview_slug_payload_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
const request = client.buildOdooPreviewApplyInputsRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  previewSlug: 'preview-42',
  sourceGitRef: 'abc123',
  manifestFile: '/tmp/preview-artifact.json',
  runId: '456',
  runAttempt: '2',
});
console.log(JSON.stringify(request));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(result.stdout)
        self.assertEqual(request["routePath"], "/v1/drivers/odoo/preview-apply-inputs")
        self.assertNotIn("preview_slug", request["payload"]["inputs"])
        self.assertEqual(
            request["payloadJsonFiles"], {"inputs.manifest": "/tmp/preview-artifact.json"}
        )
        self.assertEqual(
            request["payloadJsonFilesInput"], "inputs.manifest=/tmp/preview-artifact.json"
        )
        self.assertEqual(request["responseOutputPath"], "result.dry_run_plan")
        self.assertEqual(request["failResultPathsInput"], "result.status")
        self.assertEqual(
            request["idempotencyKey"],
            "odoo-preview-apply-inputs:odoo-tenant-cm-website:preview-42:abc123:inputs-run-456-attempt-2",
        )

    def test_client_builds_destroy_apply_request_without_manifest_or_smoke(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
const inputs = client.buildOdooPreviewApplyInputsRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  operation: 'destroy',
  prNumber: 42,
  runId: '456',
  runAttempt: '2',
});
const apply = client.buildOdooPreviewApplyRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  operation: 'destroy',
  prNumber: 42,
  dryRunPlanFile: '/tmp/dry-run.json',
  planId: 'odoo-preview-plan-test',
  runId: '456',
  runAttempt: '2',
});
console.log(JSON.stringify({ inputs, apply }));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["inputs"]["payloadJsonFiles"], {})
        self.assertEqual(
            payload["inputs"]["idempotencyKey"],
            "odoo-preview-apply-inputs:odoo-tenant-cm-website:pr-42:destroy:inputs-run-456-attempt-2",
        )
        self.assertEqual(
            payload["apply"]["payloadJsonFiles"], {"apply.dry_run_plan": "/tmp/dry-run.json"}
        )
        self.assertNotIn("smoke_check", payload["apply"]["payload"]["apply"])
        self.assertEqual(payload["apply"]["idempotencyKey"], "odoo-preview-plan-test")

    def test_client_preserves_manual_apply_boolean_overrides(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
const request = client.buildOdooPreviewApplyRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  sourceGitRef: 'abc123',
  dryRunPlanFile: '/tmp/dry-run.json',
  planId: 'odoo-preview-plan-test',
  manifestFile: '/tmp/preview-artifact.json',
  waitForDeploy: 'false',
  smokeCheck: 'true',
  runId: '456',
  runAttempt: '2',
});
console.log(JSON.stringify(request.payload.apply));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "timeout_seconds": 600,
                "wait_for_deploy": False,
                "smoke_check": True,
            },
        )

    def test_client_omits_refresh_smoke_check_without_override(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
const request = client.buildOdooPreviewApplyRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  sourceGitRef: 'abc123',
  dryRunPlanFile: '/tmp/dry-run.json',
  planId: 'odoo-preview-plan-test',
  manifestFile: '/tmp/preview-artifact.json',
  runId: '456',
  runAttempt: '2',
});
console.log(JSON.stringify(request.payload.apply));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        apply = json.loads(result.stdout)
        self.assertEqual(
            apply,
            {
                "timeout_seconds": 600,
                "wait_for_deploy": True,
            },
        )

    def test_client_rejects_invalid_apply_boolean_override(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
client.buildOdooPreviewApplyRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  sourceGitRef: 'abc123',
  dryRunPlanFile: '/tmp/dry-run.json',
  planId: 'odoo-preview-plan-test',
  manifestFile: '/tmp/preview-artifact.json',
  waitForDeploy: 'sometimes',
  runId: '456',
  runAttempt: '2',
});
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("waitForDeploy must be a boolean", result.stderr)

    def test_client_rejects_refresh_requests_without_required_files(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "odoo-preview-client.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            apply_inputs = self.run_client_script(
                client_path,
                """
client.buildOdooPreviewApplyInputsRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  sourceGitRef: 'abc123',
  runId: '456',
  runAttempt: '2',
});
""",
            )
            apply = self.run_client_script(
                client_path,
                """
client.buildOdooPreviewApplyRequest({
  product: 'odoo-tenant-cm-website',
  context: 'cm_website',
  prNumber: 42,
  sourceGitRef: 'abc123',
  dryRunPlanFile: '/tmp/dry-run.json',
  planId: 'odoo-preview-plan-test',
  runId: '456',
  runAttempt: '2',
});
""",
            )

        self.assertNotEqual(apply_inputs.returncode, 0)
        self.assertIn("refresh apply inputs require manifestFile", apply_inputs.stderr)
        self.assertNotEqual(apply.returncode, 0)
        self.assertIn("refresh apply requires manifestFile", apply.stderr)


if __name__ == "__main__":
    unittest.main()
