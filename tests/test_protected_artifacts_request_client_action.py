import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/setup-protected-artifacts-request-client/dist/index.js")
ACTION_METADATA = Path(".github/actions/setup-protected-artifacts-request-client/action.yml")


class ProtectedArtifactsRequestClientActionTests(unittest.TestCase):
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
            self.skipTest("node is required to test the protected artifacts request action")

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
        for line in github_output.read_text(encoding="utf-8").splitlines():
            name, value = line.split("=", 1)
            outputs[name] = value
        return outputs

    def run_client_script(
        self, client_path: Path, script_body: str
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the protected artifacts request client")

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
            client_path = temporary_root / ".launchplane" / "protected-artifacts.mjs"
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=client_path,
                github_output=github_output,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(client_path.exists())
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs, {"client-path": str(client_path)})

            import_result = self.run_client_script(
                client_path,
                """
console.log(Object.keys(client).sort().join(','));
""",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertIn("buildProtectedArtifactsRequest", import_result.stdout)
            self.assertIn("buildProtectedArtifactsRoutePath", import_result.stdout)

    def test_setup_action_renders_request_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            client_path = temporary_root / "protected-artifacts.mjs"
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=client_path,
                github_output=github_output,
                inputs={
                    "RENDER-REQUEST": "true",
                    "PRODUCT": "odoo tenant/cm",
                    "CONTEXT": "cm website",
                    "RESPONSE-OUTPUT-FILE": "protected-artifacts.json",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs["client-path"], str(client_path))
            self.assertEqual(
                outputs["route-path"],
                "/v1/artifacts/protected?product=odoo%20tenant%2Fcm&context=cm%20website",
            )
            self.assertEqual(outputs["method"], "GET")
            self.assertEqual(outputs["response-output-path"], "protected_artifacts")
            self.assertEqual(outputs["response-output-file"], "protected-artifacts.json")

    def test_setup_action_renders_whole_product_request_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            client_path = temporary_root / "protected-artifacts.mjs"
            github_output = temporary_root / "github-output.txt"

            result = self.run_setup_action(
                output_path=client_path,
                github_output=github_output,
                inputs={
                    "RENDER-REQUEST": "true",
                    "PRODUCT": "verireel",
                    "RESPONSE-OUTPUT-FILE": "protected-artifacts.json",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = self.read_outputs(github_output)
            self.assertEqual(outputs["client-path"], str(client_path))
            self.assertEqual(outputs["route-path"], "/v1/artifacts/protected?product=verireel")
            self.assertEqual(outputs["method"], "GET")
            self.assertEqual(outputs["response-output-path"], "protected_artifacts")
            self.assertEqual(outputs["response-output-file"], "protected-artifacts.json")

    def test_setup_action_render_mode_rejects_missing_product(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            result = self.run_setup_action(
                output_path=Path(temporary_directory) / "protected-artifacts.mjs",
                github_output=Path(temporary_directory) / "out",
                inputs={"RENDER-REQUEST": "true"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("product is required", result.stderr)

    def test_setup_action_rejects_invalid_render_request_boolean(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            result = self.run_setup_action(
                output_path=Path(temporary_directory) / "protected-artifacts.mjs",
                github_output=Path(temporary_directory) / "out",
                inputs={"RENDER-REQUEST": "sometimes"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("render-request must be a boolean", result.stderr)

    def test_client_builds_whole_product_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "protected-artifacts.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
const request = client.buildProtectedArtifactsRequest({
  product: 'verireel',
  responseOutputFile: 'protected-artifacts.json',
});
console.log(JSON.stringify(request));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(result.stdout)
        self.assertEqual(request["routePath"], "/v1/artifacts/protected?product=verireel")
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["responseOutputPath"], "protected_artifacts")
        self.assertEqual(request["responseOutputFile"], "protected-artifacts.json")

    def test_client_rejects_blank_product(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "protected-artifacts.mjs"
            self.run_setup_action(
                output_path=client_path, github_output=Path(temporary_directory) / "out"
            )

            result = self.run_client_script(
                client_path,
                """
try {
  client.buildProtectedArtifactsRequest({ product: '   ' });
} catch (error) {
  console.error(error.message);
  process.exit(2);
}
""",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Protected artifacts product is required", result.stderr)
