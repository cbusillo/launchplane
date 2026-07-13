import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import check_fork_runner_isolation
from tests.support.workflows import check_launchplane_request_contract
from tests.support.workflows import load_workflow


class WorkflowInvariantCheckerTests(unittest.TestCase):
    def test_failure_message_names_invariant_and_workflow(self) -> None:
        workflow = load_workflow(".github/workflows/ci.yml")

        violations = check_fork_runner_isolation(
            workflow,
            same_repo_jobs=("static_checks",),
            fork_jobs=("static_checks",),
        )

        self.assertTrue(violations)
        rendered = str(violations[0])
        self.assertIn("ci.yml", rendered)
        self.assertIn("fork-runner-isolation", rendered)

    def test_parser_reads_request_step_with_block_scalars_and_inline_sequences(self) -> None:
        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "sample.yml"
            workflow_path.write_text(
                "---\n"
                "name: Sample\n"
                "on:\n"
                "  workflow_dispatch:\n"
                "permissions:\n"
                "  contents: read\n"
                "  id-token: write\n"
                "jobs:\n"
                "  request:\n"
                "    runs-on: [ubuntu-latest]\n"
                "    steps:\n"
                "      - name: Build payload\n"
                "        run: |\n"
                "          set -euo pipefail\n"
                "          echo ok\n"
                "      - name: Send request\n"
                "        uses: ./.github/actions/launchplane-request\n"
                "        with:\n"
                "          method: POST\n"
                "          route-path: /v1/example\n"
                '          log-response-body: "false"\n',
                encoding="utf-8",
            )

            workflow = load_workflow(workflow_path)

        violations = check_launchplane_request_contract(
            workflow,
            invariant="sample-request",
            expected_steps={
                "Send request": {
                    "method": "POST",
                    "route-path": "/v1/example",
                    "log-response-body": "false",
                },
            },
        )

        self.assertEqual([], [str(violation) for violation in violations])


if __name__ == "__main__":
    unittest.main()
