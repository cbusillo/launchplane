import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ACTION_ENTRYPOINT = Path(".github/actions/setup-preview-prepare-client/dist/index.js")
ACTION_METADATA = Path(".github/actions/setup-preview-prepare-client/action.yml")


class PreviewPrepareClientActionTests(unittest.TestCase):
    def test_action_metadata_uses_supported_node_runtime(self) -> None:
        self.assertIn(
            "using: node24",
            ACTION_METADATA.read_text(encoding="utf-8"),
        )

    def run_setup_action(self, *, output_path: Path, github_output: Path) -> subprocess.CompletedProcess[str]:
        if shutil.which("node") is None:
            self.skipTest("node is required to test the preview prepare action")

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
            self.skipTest("node is required to test the preview prepare client")

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
            client_path = temporary_root / ".launchplane" / "preview-client.mjs"
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
            self.assertIn("buildSameRepoPreviewPrepareOutputs", import_result.stdout)
            self.assertIn("hasPreviewLabel", import_result.stdout)
            self.assertIn("PREVIEW_LABEL_NAME", import_result.stdout)

    def test_client_builds_same_repo_refresh_outputs(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "preview-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const outputs = client.buildSameRepoPreviewPrepareOutputs({
  action: 'synchronize',
  labels: [{ name: 'preview' }],
  currentRepository: 'cbusillo/verireel',
  headRepository: 'cbusillo/verireel',
  actor: 'cbusillo',
  prNumber: 42,
  prSha: 'ABC1234',
  imageName: 'ghcr.io/CBUSILLO/verireel-app',
  runUrl: 'https://github.com/cbusillo/verireel/actions/runs/1',
});
console.log(JSON.stringify(outputs));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = json.loads(result.stdout)
        self.assertEqual(outputs["mode"], "refresh")
        self.assertEqual(outputs["same_repo"], "true")
        self.assertEqual(outputs["preview_supported"], "true")
        self.assertEqual(outputs["pr_number"], "42")
        self.assertEqual(outputs["pr_sha"], "abc1234")
        self.assertEqual(outputs["image_name"], "ghcr.io/cbusillo/verireel-app")
        self.assertEqual(outputs["preview_slug"], "pr-42")
        self.assertEqual(outputs["floating_tag"], "pr-42")
        self.assertEqual(outputs["immutable_tag"], "pr-42-sha-abc1234")
        self.assertEqual(
            outputs["immutable_image_reference"],
            "ghcr.io/cbusillo/verireel-app:pr-42-sha-abc1234",
        )
        self.assertEqual(
            outputs["floating_image_reference"],
            "ghcr.io/cbusillo/verireel-app:pr-42",
        )

    def test_client_marks_fork_or_dependabot_preview_as_unsupported(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "preview-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const fork = client.buildSameRepoPreviewPrepareOutputs({
  action: 'labeled',
  actionLabelName: 'preview',
  currentRepository: 'cbusillo/verireel',
  headRepository: 'someone/verireel',
  actor: 'contributor',
  prNumber: 42,
  prSha: 'abc1234',
  imageName: 'ghcr.io/cbusillo/verireel-app',
});
const dependabot = client.buildSameRepoPreviewPrepareOutputs({
  action: 'synchronize',
  labels: [{ name: 'preview' }],
  currentRepository: 'cbusillo/verireel',
  headRepository: 'cbusillo/verireel',
  actor: 'dependabot[bot]',
  prNumber: 43,
  prSha: 'def5678',
  imageName: 'ghcr.io/cbusillo/verireel-app',
});
console.log(JSON.stringify({ fork, dependabot }));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = json.loads(result.stdout)
        self.assertEqual(outputs["fork"]["mode"], "unsupported")
        self.assertEqual(outputs["fork"]["same_repo"], "false")
        self.assertEqual(outputs["dependabot"]["mode"], "unsupported")
        self.assertEqual(outputs["dependabot"]["preview_supported"], "false")

    def test_client_fails_closed_when_actor_is_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "preview-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
const outputs = client.buildSameRepoPreviewPrepareOutputs({
  action: 'synchronize',
  labels: [{ name: 'preview' }],
  currentRepository: 'cbusillo/verireel',
  headRepository: 'cbusillo/verireel',
  prNumber: 42,
  prSha: 'abc1234',
  imageName: 'ghcr.io/cbusillo/verireel-app',
});
console.log(JSON.stringify(outputs));
""",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = json.loads(result.stdout)
        self.assertEqual(outputs["mode"], "unsupported")
        self.assertEqual(outputs["same_repo"], "true")
        self.assertEqual(outputs["preview_supported"], "false")

    def test_client_rejects_invalid_preview_image_facts(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            client_path = Path(temporary_directory) / "preview-client.mjs"
            self.run_setup_action(output_path=client_path, github_output=Path(temporary_directory) / "out")

            result = self.run_client_script(
                client_path,
                """
client.buildSameRepoPreviewPrepareOutputs({
  action: 'synchronize',
  labels: [{ name: 'preview' }],
  currentRepository: 'cbusillo/verireel',
  headRepository: 'cbusillo/verireel',
  actor: 'cbusillo',
  prNumber: 42,
  prSha: 'not-a-sha',
  imageName: 'ghcr.io/cbusillo/verireel-app',
});
""",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hexadecimal commit SHA", result.stderr)


if __name__ == "__main__":
    unittest.main()
