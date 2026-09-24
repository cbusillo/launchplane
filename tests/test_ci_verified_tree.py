import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest

from tests.support.workflows import load_workflow


class VerifiedTreeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "verified_tree", "Prove the pushed tree already passed CI"
        )
        assert step is not None
        self.program = step.run
        self.repository = "example/control-plane"
        self.base, self.head, self.candidate, self.tree = (letter * 40 for letter in "abcd")
        self.prefix = f"repos/{self.repository}"
        self.current_checks = self.checks_path(self.candidate)
        self.parent_checks = self.checks_path(self.head)
        self.compare = f"{self.prefix}/compare/{self.base}...{self.head}"
        self.responses: dict[str, object] = {
            f"{self.prefix}/git/commits/{self.candidate}": {
                "tree": {"sha": self.tree},
                "parents": [{"sha": self.base}, {"sha": self.head}],
            },
            f"{self.prefix}/git/commits/{self.head}": {"tree": {"sha": self.tree}},
            self.current_checks: {"check_runs": []},
            self.parent_checks: {"check_runs": [self.gate()]},
            self.compare: {"status": "ahead"},
            f"{self.prefix}/check-suites/17": {
                "head_sha": self.candidate,
                "head_branch": "main",
                "pull_requests": [],
            },
        }

    def checks_path(self, sha: str) -> str:
        return f"{self.prefix}/commits/{sha}/check-runs?check_name=ci-gate&per_page=100"

    @staticmethod
    def gate(
        *, status: str = "completed", conclusion: str = "success", app: str = "github-actions"
    ) -> dict[str, object]:
        return {
            "status": status,
            "conclusion": conclusion,
            "app": {"slug": app},
            "check_suite": {"id": 17},
        }

    def proof(self, *, event: str = "push", ref: str = "refs/heads/launchplane/train/test") -> bool:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            responses = root / "responses.json"
            responses.write_text(json.dumps(self.responses))
            output = root / "output"
            gh = root / "gh"
            gh.write_text(
                f"#!{sys.executable}\n"
                + textwrap.dedent(
                    """\
                    import json
                    import os
                    from pathlib import Path
                    import subprocess
                    import sys

                    responses = json.loads(Path(os.environ["GH_FIXTURES"]).read_text())
                    endpoint = sys.argv[2]
                    if endpoint not in responses:
                        sys.exit(1)
                    payload = json.dumps(responses[endpoint])
                    if "--jq" in sys.argv:
                        query = sys.argv[sys.argv.index("--jq") + 1]
                        sys.exit(subprocess.run(["jq", "-r", query], input=payload, text=True).returncode)
                    print(payload)
                    """
                )
            )
            gh.chmod(0o700)
            subprocess.run(
                ["bash", "-c", self.program],
                env=os.environ
                | {
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "GH_TOKEN": "fixture",
                    "GH_FIXTURES": str(responses),
                    "GITHUB_OUTPUT": str(output),
                    "EVENT_NAME": event,
                    "REF": ref,
                    "REPOSITORY": self.repository,
                    "SHA": self.candidate,
                },
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            value = output.read_text().strip()
            self.assertIn(value, ("verified=true", "verified=false"))
            return value == "verified=true"

    def test_main_reuses_an_up_to_date_pr_tree_but_new_candidates_run_full_ci(self) -> None:
        self.assertTrue(self.proof(ref="refs/heads/main"))
        self.assertFalse(self.proof())

    def test_base_creation_reset_and_all_no_op_can_reuse_the_tested_commit(self) -> None:
        self.responses[self.current_checks] = {"check_runs": [self.gate()]}
        self.assertTrue(self.proof())

    def test_pr_or_fork_check_cannot_stand_in_for_main_commit_proof(self) -> None:
        self.responses[self.current_checks] = {"check_runs": [self.gate()]}
        for branch, pull_requests in ((None, []), ("feature", []), ("main", [{"number": 1}])):
            with self.subTest(branch=branch, pull_requests=pull_requests):
                self.responses[f"{self.prefix}/check-suites/17"] = {
                    "head_sha": self.candidate,
                    "head_branch": branch,
                    "pull_requests": pull_requests,
                }
                self.assertFalse(self.proof())

    def test_combined_multi_pr_tree_requires_full_ci(self) -> None:
        self.responses[f"{self.prefix}/git/commits/{self.head}"] = {"tree": {"sha": "e" * 40}}
        self.assertFalse(self.proof(ref="refs/heads/main"))

    def test_equal_tree_does_not_prove_a_behind_pr_tested_the_candidate(self) -> None:
        for status in ("behind", "diverged"):
            with self.subTest(status=status):
                self.responses[self.compare] = {"status": status}
                self.assertFalse(self.proof(ref="refs/heads/main"))

    def test_only_completed_success_from_actions_is_reusable(self) -> None:
        for gate in (
            self.gate(status="in_progress", conclusion=""),
            self.gate(conclusion="failure"),
            self.gate(conclusion="cancelled"),
            self.gate(app="another-app"),
        ):
            with self.subTest(gate=gate):
                for endpoint in (self.current_checks, self.parent_checks):
                    self.responses[endpoint] = {"check_runs": [gate]}
                self.assertFalse(self.proof(ref="refs/heads/main"))

    def test_missing_api_evidence_requires_full_ci(self) -> None:
        self.responses = {}
        self.assertFalse(self.proof())

    def test_conflicting_actions_gates_veto_reuse(self) -> None:
        for conflict in (self.gate(conclusion="failure"), self.gate(status="in_progress")):
            with self.subTest(conflict=conflict):
                for endpoint in (self.current_checks, self.parent_checks):
                    self.responses[endpoint] = {"check_runs": [self.gate(), conflict]}
                self.assertFalse(self.proof(ref="refs/heads/main"))

    def test_prs_and_unrelated_branches_cannot_reuse_checks(self) -> None:
        self.responses[self.current_checks] = {"check_runs": [self.gate()]}
        for event, ref in (
            ("pull_request", "refs/heads/main"),
            ("push", "refs/heads/feature"),
        ):
            with self.subTest(event=event, ref=ref):
                self.assertFalse(self.proof(event=event, ref=ref))
