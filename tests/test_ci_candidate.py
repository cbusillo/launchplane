import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest

from tests.support.workflows import load_workflow


class CandidateCiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/ci.yml")
        self.repository = "example/control-plane"
        self.sha = "a" * 40
        self.branch = "launchplane/train/example/batch"
        prefix = f"repos/{self.repository}/actions"
        self.current_path = f"{prefix}/runs/99"
        self.runs_path = f"{prefix}/workflows/7/runs?event=push&head_sha={self.sha}&per_page=100"
        self.jobs_path = f"{prefix}/runs/100/attempts/2/jobs?per_page=100"
        self.source_url = "https://example.invalid/run/100"
        self.source: dict[str, object] = {
            "id": 100,
            "workflow_id": 7,
            "event": "push",
            "head_sha": self.sha,
            "head_branch": self.branch,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
            "html_url": self.source_url,
        }
        self.jobs: list[dict[str, object]] = [
            {"jobs": [{"name": "ci-gate", "status": "completed", "conclusion": "success"}]}
        ]

    def route(self, *, event: str = "pull_request", **head: str) -> dict[str, str]:
        step = self.workflow.step_named("candidate_ci", "Route candidate CI")
        assert step is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "event.json"
            payload.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "head": {
                                "repo": {"full_name": head.get("repository", self.repository)},
                                "ref": head.get("branch", self.branch),
                                "sha": head.get("sha", self.sha),
                            }
                        }
                    }
                )
            )
            output = root / "output"
            subprocess.run(
                ["bash", "-c", step.run],
                env=os.environ
                | {
                    "EVENT_NAME": event,
                    "REPOSITORY": self.repository,
                    "GITHUB_EVENT_PATH": str(payload),
                    "GITHUB_OUTPUT": str(output),
                },
                capture_output=True,
                text=True,
                check=True,
            )
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def proof(
        self,
        runs: list[list[dict[str, object]]] | None = None,
        *,
        jobs: object | None = None,
        missing_api: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        step = self.workflow.step_named("ci_gate", "Require the exact candidate push CI")
        assert step is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            responses = root / "responses.json"
            responses.write_text(
                json.dumps(
                    {}
                    if missing_api
                    else {
                        self.current_path: [{"workflow_id": 7}],
                        self.runs_path: [
                            {"workflow_runs": entries} for entries in (runs or [[self.source]])
                        ],
                        self.jobs_path: [self.jobs if jobs is None else jobs],
                    }
                )
            )
            gh = root / "gh"
            gh.write_text(
                f"#!{sys.executable}\n"
                + textwrap.dedent("""\
                import json
                import os
                from pathlib import Path
                import subprocess
                import sys

                file = Path(os.environ['RESPONSES'])
                data = json.loads(file.read_text())
                endpoint = next(arg for arg in sys.argv if arg.startswith('repos/'))
                replies = data.get(endpoint)
                if not replies:
                    sys.exit(1)
                payload = replies[0]
                if len(replies) > 1:
                    replies.pop(0)
                    file.write_text(json.dumps(data))
                value = json.dumps(payload)
                if '--jq' in sys.argv:
                    query = sys.argv[sys.argv.index('--jq') + 1]
                    sys.exit(subprocess.run(['jq', '-r', query], input=value, text=True).returncode)
                print(value)
                """)
            )
            gh.chmod(0o700)
            # An unresolved wait stops immediately in the fixture; a queued->passed
            # fixture permits exactly one poll, without sleeping on wall time.
            sleep = root / "sleep"
            sleep.write_text('#!/bin/sh\n[ ! -f "$POLL_MARKER" ] || exit 1\ntouch "$POLL_MARKER"\n')
            sleep.chmod(0o700)
            summary = root / "summary"
            result = subprocess.run(
                ["bash", "-c", step.run],
                env=os.environ
                | {
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "RESPONSES": str(responses),
                    "POLL_MARKER": str(root / "polled"),
                    "GITHUB_REPOSITORY": self.repository,
                    "GITHUB_RUN_ID": "99",
                    "CANDIDATE_SHA": self.sha,
                    "CANDIDATE_BRANCH": self.branch,
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                self.assertIn(self.sha, summary.read_text())
                self.assertIn(self.source_url, summary.read_text())
            return result

    def test_only_same_repository_train_pr_delegates(self) -> None:
        self.assertEqual(
            self.route(), {"sha": self.sha, "branch": self.branch, "delegated": "true"}
        )
        for parameters in (
            {"event": "push"},
            {"event": "schedule"},
            {"repository": "fork/control-plane"},
            {"branch": "feature"},
            {"branch": "launchplane/construct/batch"},
            {"sha": "invalid"},
        ):
            with self.subTest(parameters=parameters):
                self.assertEqual(self.route(**parameters), {"delegated": "false"})

    def test_exact_push_and_successful_gate_pass(self) -> None:
        result = self.proof()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_queued_source_waits_until_success(self) -> None:
        queued = self.source | {"status": "queued", "conclusion": None}
        result = self.proof([[queued], [self.source]])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unrelated_evidence_and_missing_source_cannot_pass(self) -> None:
        for mismatch in (
            {"event": "pull_request"},
            {"head_sha": "b" * 40},
            {"head_branch": "main"},
            {"workflow_id": 8},
        ):
            with self.subTest(mismatch=mismatch):
                self.assertNotEqual(self.proof([[self.source | mismatch]]).returncode, 0)
        self.assertNotEqual(self.proof([[]]).returncode, 0)
        self.assertNotEqual(self.proof(missing_api=True).returncode, 0)

    def test_latest_failed_or_pending_run_vetoes_old_success(self) -> None:
        for status, conclusion in (("completed", "failure"), ("queued", None)):
            with self.subTest(status=status):
                newer = self.source | {"id": 101, "status": status, "conclusion": conclusion}
                self.assertNotEqual(self.proof([[self.source, newer]]).returncode, 0)

    def test_source_and_gate_must_both_complete_successfully(self) -> None:
        for conclusion in ("failure", "cancelled", "skipped", None):
            with self.subTest(conclusion=conclusion):
                self.assertNotEqual(
                    self.proof([[self.source | {"conclusion": conclusion}]]).returncode, 0
                )
                jobs = [
                    {"jobs": [{"name": "ci-gate", "status": "completed", "conclusion": conclusion}]}
                ]
                self.assertNotEqual(self.proof(jobs=jobs).returncode, 0)
        invalid_jobs: tuple[object, ...] = (
            [{"jobs": []}],
            [{"jobs": [{"name": "ci-gate", "status": "queued"}]}],
            {},
        )
        for invalid in invalid_jobs:
            with self.subTest(jobs=invalid):
                self.assertNotEqual(self.proof(jobs=invalid).returncode, 0)

    def test_failed_routing_cannot_fall_through_to_tree_reuse(self) -> None:
        step = self.workflow.step_named("ci_gate", "Require successful CI path")
        assert step is not None
        result = subprocess.run(
            ["bash", "-c", step.run],
            env=os.environ
            | {
                "CANDIDATE_CI_RESULT": "failure",
                "VERIFIED_TREE": "true",
                "AUDIT_POLICY_RESULT": "success",
                "REPOSITORY_PINS_RESULT": "success",
            },
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
