import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import check_ci_aggregate_gate
from tests.support.workflows import check_fork_runner_isolation
from tests.support.workflows import check_frontend_browser_smoke
from tests.support.workflows import check_launchplane_request_contract
from tests.support.workflows import load_workflow


class WorkflowInvariantCheckerTests(unittest.TestCase):
    def test_required_workflows_run_for_merge_train_candidate_refs(self) -> None:
        for workflow_path in (
            ".github/workflows/ci.yml",
            ".github/workflows/security.yml",
            ".github/workflows/codeql.yml",
        ):
            with self.subTest(workflow=workflow_path):
                workflow_on = load_workflow(workflow_path).data.get("on")
                self.assertIsInstance(workflow_on, dict)
                assert isinstance(workflow_on, dict)
                push = workflow_on.get("push")
                self.assertIsInstance(push, dict)
                assert isinstance(push, dict)
                branches = push.get("branches")
                self.assertIsInstance(branches, list)
                assert isinstance(branches, list)
                self.assertIn("launchplane/train/**", branches)

        for workflow_path in (
            ".github/workflows/ci.yml",
            ".github/workflows/security.yml",
            ".github/workflows/codeql.yml",
        ):
            with self.subTest(concurrency=workflow_path):
                concurrency = load_workflow(workflow_path).data.get("concurrency")
                self.assertIsInstance(concurrency, dict)
                assert isinstance(concurrency, dict)
                group = concurrency.get("group")
                cancel_in_progress = concurrency.get("cancel-in-progress")
                self.assertIsInstance(group, str)
                self.assertIsInstance(cancel_in_progress, str)
                assert isinstance(group, str)
                assert isinstance(cancel_in_progress, str)
                for required in (
                    "github.event.created",
                    "github.event.forced",
                    "github.sha",
                    "github.ref",
                ):
                    self.assertIn(required, group)
                self.assertIn("github.event.created", cancel_in_progress)
                self.assertIn("github.event.forced", cancel_in_progress)

    def test_candidate_concurrency_does_not_cancel_base_pushes(self) -> None:
        for workflow_path in (
            ".github/workflows/ci.yml",
            ".github/workflows/security.yml",
            ".github/workflows/codeql.yml",
        ):
            with self.subTest(workflow=workflow_path):
                concurrency = load_workflow(workflow_path).data.get("concurrency")
                self.assertIsInstance(concurrency, dict)
                assert isinstance(concurrency, dict)
                group = concurrency.get("group")
                cancel_in_progress = concurrency.get("cancel-in-progress")
                self.assertIsInstance(group, str)
                self.assertIsInstance(cancel_in_progress, str)
                assert isinstance(group, str)
                assert isinstance(cancel_in_progress, str)
                candidate_ref_guard = "refs/heads/launchplane/train/"
                self.assertIn(candidate_ref_guard, group)
                self.assertIn(candidate_ref_guard, cancel_in_progress)
                self.assertIn("!github.event.created", group)
                self.assertIn("!github.event.forced", group)
                self.assertIn("&& github.sha ||", group)

    def test_container_scans_refresh_runtime_security_packages(self) -> None:
        workflow = load_workflow(".github/workflows/ci.yml")
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM mirror.gcr.io/library/python:3.13-slim AS runtime", dockerfile)
        for job_id in ("container_scan", "container_scan_fork"):
            with self.subTest(job_id=job_id):
                step = workflow.step_named(job_id, "Build runtime image")
                self.assertIsNotNone(step)
                assert step is not None
                build_inputs = step.data.get("with")
                self.assertIsInstance(build_inputs, dict)
                assert isinstance(build_inputs, dict)
                self.assertTrue(build_inputs.get("pull"))
                self.assertEqual(build_inputs.get("no-cache-filters"), "runtime")

    def test_postgres_service_uses_ipv4_only_dynamic_host_port(self) -> None:
        workflow_text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn('          - "127.0.0.1::5432"\n', workflow_text)

    def test_postgres_service_uses_ephemeral_data_storage(self) -> None:
        workflow = load_workflow(".github/workflows/ci.yml")
        services = workflow.job("postgres_integration").get("services")

        self.assertIsInstance(services, dict)
        postgres = services.get("postgres") if isinstance(services, dict) else None
        self.assertIsInstance(postgres, dict)
        options = postgres.get("options") if isinstance(postgres, dict) else None
        self.assertIsInstance(options, str)
        assert isinstance(options, str)
        self.assertIn(
            "--tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=1g,mode=0700",
            options,
        )

    def test_runner_hygiene_dispatch_inputs_are_passed_through_environment(self) -> None:
        workflow = load_workflow(".github/workflows/runner-host-hygiene.yml")
        step = workflow.step_named(
            "runner-host-hygiene", "Run approved runner host hygiene executor"
        )

        self.assertIsNotNone(step)
        assert step is not None
        self.assertNotIn("${{ inputs.", step.run)
        self.assertNotIn("${{ github.event", step.run)
        env = step.data.get("env")
        self.assertIsInstance(env, dict)
        if isinstance(env, dict):
            self.assertIn("INPUT_TARGET_BUILDKIT_BUILDER", env)

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
