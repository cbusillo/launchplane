import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import check_ci_aggregate_gate
from tests.support.workflows import check_fork_runner_isolation
from tests.support.workflows import check_frontend_browser_smoke
from tests.support.workflows import check_launchplane_request_contract
from tests.support.workflows import load_workflow


class WorkflowInvariantCheckerTests(unittest.TestCase):
    def test_postgres_service_uses_ipv4_only_dynamic_host_port(self) -> None:
        workflow = load_workflow(".github/workflows/ci.yml")

        postgres_job = workflow.job("postgres_integration")
        postgres_service = postgres_job["services"]["postgres"]

        self.assertEqual(["127.0.0.1::5432"], postgres_service["ports"])

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

    def test_browser_smoke_must_run_for_fork_pull_requests(self) -> None:
        workflow_text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        job_header = "  frontend_browser_smoke:\n    runs-on: ubuntu-latest\n"
        self.assertIn(job_header, workflow_text)
        drifted_workflow = workflow_text.replace(
            job_header,
            "  frontend_browser_smoke:\n"
            "    if: github.event_name != 'pull_request'\n"
            "    runs-on: ubuntu-latest\n",
            1,
        )

        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "ci.yml"
            workflow_path.write_text(drifted_workflow, encoding="utf-8")
            workflow = load_workflow(workflow_path)

        violations = check_frontend_browser_smoke(workflow)

        self.assertTrue(
            any("same-repository and fork pull requests" in item.message for item in violations)
        )

    def test_ci_gate_must_require_browser_smoke_on_both_paths(self) -> None:
        workflow_text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        browser_gate = (
            "            require_success frontend_browser_smoke "
            '"${FRONTEND_BROWSER_SMOKE_RESULT}"\n'
        )
        self.assertEqual(2, workflow_text.count(browser_gate))
        prefix, suffix = workflow_text.rsplit(browser_gate, 1)
        drifted_workflow = (prefix + suffix).replace(
            browser_gate,
            browser_gate + browser_gate,
            1,
        )

        with TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "ci.yml"
            workflow_path.write_text(drifted_workflow, encoding="utf-8")
            workflow = load_workflow(workflow_path)

        violations = check_ci_aggregate_gate(workflow)

        self.assertTrue(
            any("fork path must require browser smoke" in item.message for item in violations)
        )


if __name__ == "__main__":
    unittest.main()
