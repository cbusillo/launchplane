import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


class AuditPolicyWorkflowTests(unittest.TestCase):
    def classify(
        self,
        paths: tuple[str, ...],
        *,
        event: str = "pull_request",
        ref: str = "refs/pull/1/merge",
        missing_base: bool = False,
        rename_image: bool = False,
    ) -> dict[str, str]:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "audit_policy", "Classify audit inputs"
        )
        assert step is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.check_output(
                    ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "--quiet")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (root / "Dockerfile").write_text("FROM scratch\n")
            git("add", ".")
            git("commit", "--quiet", "-m", "baseline")
            base = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", base)
            for path in paths:
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("changed fixture\n")
            if rename_image:
                git("mv", "Dockerfile", "archived-image-recipe")
            git("add", ".")
            git("commit", "--quiet", "--allow-empty", "-m", "candidate")
            output = root / "outputs"
            subprocess.run(
                ["bash", "-c", step.run],
                cwd=root,
                env=os.environ
                | {
                    "EVENT_NAME": event,
                    "REF": ref,
                    "BASE_SHA": "missing" if missing_base else base,
                    "RUNNER_TEMP": directory,
                    "GITHUB_OUTPUT": str(output),
                },
                capture_output=True,
                text=True,
                check=True,
            )
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_unrelated_source_and_docs_report_inherited_advisories(self) -> None:
        result = self.classify(("control_plane/example.py", "docs/example.md"))
        self.assertEqual(result, {"python_gate": "false", "image_gate": "false"})

    def test_python_dependency_or_scan_changes_keep_both_gates(self) -> None:
        for path in (
            "uv.lock",
            "pyproject.toml",
            "requirements-dev.txt",
            ".github/workflows/ci.yml",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.classify((path,)), {"python_gate": "true", "image_gate": "true"}
                )

    def test_image_recipe_and_frontend_dependencies_keep_the_image_gate(self) -> None:
        for path in ("Dockerfile", ".dockerignore", "frontend/pnpm-lock.yaml"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.classify((path,)), {"python_gate": "false", "image_gate": "true"}
                )

    def test_renaming_an_image_input_cannot_evade_the_gate(self) -> None:
        self.assertEqual(
            self.classify((), rename_image=True), {"python_gate": "false", "image_gate": "true"}
        )

    def test_schedule_and_unprovable_diff_keep_absolute_gates(self) -> None:
        for kwargs in ({"event": "schedule"}, {"missing_base": True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    self.classify(("docs/example.md",), **kwargs),
                    {"python_gate": "true", "image_gate": "true"},
                )

    def test_main_and_train_pushes_keep_the_same_input_classification(self) -> None:
        for ref in ("refs/heads/main", "refs/heads/launchplane/train/example"):
            with self.subTest(ref=ref):
                self.assertEqual(
                    self.classify(("uv.lock",), event="push", ref=ref),
                    {"python_gate": "true", "image_gate": "true"},
                )
                self.assertEqual(
                    self.classify(("docs/example.md",), event="push", ref=ref),
                    {"python_gate": "false", "image_gate": "false"},
                )

    def test_report_only_scanner_failures_stay_visible(self) -> None:
        workflow = load_workflow(".github/workflows/ci.yml")
        for job, report_step in (
            ("static_checks", "Report Python audit outcome"),
            ("static_checks_fork", "Report Python audit outcome"),
            ("container_scan", "Report image audit outcome"),
            ("container_scan_fork", "Report image audit outcome"),
        ):
            with self.subTest(job=job), TemporaryDirectory() as directory:
                step = workflow.step_named(job, report_step)
                assert step is not None
                summary = Path(directory) / "summary"
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=os.environ
                    | {
                        "AUDIT_OUTCOME": "failure",
                        "AUDIT_GATE": "false",
                        "GITHUB_STEP_SUMMARY": str(summary),
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("failure; blocking=false", summary.read_text())
                self.assertIn("::warning", result.stdout)

    def test_missing_classification_cannot_pass_using_cached_tree_proof(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "ci_gate", "Require successful CI path"
        )
        assert step is not None
        result = subprocess.run(
            ["bash", "-c", step.run],
            env=os.environ | {"AUDIT_POLICY_RESULT": "failure", "VERIFIED_TREE": "true"},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_verified_tree_still_requires_pin_verification(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "ci_gate", "Require successful CI path"
        )
        assert step is not None
        for pins, expected_exit in (("success", 0), ("failure", 1), ("skipped", 1)):
            with self.subTest(pins=pins):
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=os.environ
                    | {
                        "AUDIT_POLICY_RESULT": "success",
                        "VERIFIED_TREE": "true",
                        "REPOSITORY_PINS_RESULT": pins,
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected_exit, result.stderr)
