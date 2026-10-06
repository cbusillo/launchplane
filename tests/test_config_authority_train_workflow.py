from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_ref
from tests.support.workflows import load_workflow


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ConfigAuthorityTrainWorkflowTests(unittest.TestCase):
    @staticmethod
    def _exercise(
        root: Path,
        *,
        batch: bool = False,
        rejected: bool = False,
        target_movement: str = "",
        repository: str = "example/product",
        overrides: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, str, str]:
        product = root / "product-repo"
        product.mkdir()
        _git(product, "init", "-b", "trunk")
        _git(product, "config", "user.name", "Workflow Fixture")
        _git(product, "config", "user.email", "fixture@example.invalid")
        (product / "app.py").write_text("VALUE = 1\n")
        _git(product, "add", ".")
        _git(product, "commit", "-m", "base")
        base = _git(product, "rev-parse", "HEAD")
        _git(product, "checkout", "-b", "feature-one")
        (product / "app.py").write_text(
            'PRODUCT_DOMAIN = "shop.example.invalid"\n' if rejected else "VALUE = 2\n"
        )
        _git(product, "commit", "-am", "first change")
        first_head = _git(product, "rev-parse", "HEAD")
        _git(product, "checkout", "-b", "candidate", base)
        _git(product, "merge", "--no-ff", "-m", "candidate first entry", first_head)
        if batch:
            _git(product, "checkout", "-b", "feature-two", base)
            (product / "other.py").write_text("VALUE = 3\n")
            _git(product, "add", ".")
            _git(product, "commit", "-m", "second change")
            second_head = _git(product, "rev-parse", "HEAD")
            _git(product, "checkout", "candidate")
            _git(product, "merge", "--no-ff", "-m", "candidate second entry", second_head)
        head = _git(product, "rev-parse", "HEAD")
        if target_movement and target_movement != "shallow":
            _git(product, "checkout", "trunk")
            if target_movement == "deleted":
                _git(product, "checkout", "candidate")
                _git(product, "branch", "-D", "trunk")
            elif target_movement == "unrelated":
                _git(product, "checkout", "--orphan", "unrelated")
                _git(product, "rm", "-rf", ".")
                (product / "fresh.py").write_text("VALUE = 5\n")
                _git(product, "add", ".")
                _git(product, "commit", "-m", "unrelated root")
                _git(product, "branch", "-D", "trunk")
                _git(product, "branch", "-m", "trunk")
            elif target_movement == "fast_forward":
                _git(product, "merge", "--ff-only", head)
            elif target_movement == "landed":
                _git(product, "merge", "--no-ff", "-m", "land candidate", head)
            else:
                (product / "later.py").write_text("VALUE = 4\n")
                _git(product, "add", ".")
                _git(product, "commit", "-m", "target advanced")
            _git(product, "checkout", "candidate")
        remote = root / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(product), str(remote)],
            check=True,
            capture_output=True,
        )
        _git(product, "remote", "add", "origin", str(remote))
        if target_movement == "shallow":
            (product / ".git/shallow").write_text(head + "\n")
        tool = root / "launchplane"
        tool.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        capture = root / "audit-args"
        uv = bin_dir / "uv"
        uv.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE"\nshift 4\n'
            'exec "$AUDIT_PYTHON" -c "from control_plane.cli import main; main()" "$@"\n'
        )
        uv.chmod(0o755)
        workflow = load_workflow(".github/workflows/reusable-product-repo-config-authority.yml")
        step = workflow.step_named(
            "launchplane-config-authority", "Run Launchplane config authority gate"
        )
        assert step is not None
        env = os.environ | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "AUDIT_PYTHON": sys.executable,
            "EVENT_NAME": "push",
            "EVENT_REF": build_merge_train_batch_candidate_ref(
                repository=repository, base_branch="trunk", batch_id="fixture-batch"
            ),
            "GITHUB_REPOSITORY": repository,
            "DEFAULT_BRANCH": "trunk",
            "BASE_SHA": "0" * 40,
            "HEAD_SHA": head,
            "FAIL_ON_FINDINGS": "true",
            "AUDIT_PYTHON_VERSION": "3.13",
        }
        result = subprocess.run(
            ["bash", "-c", step.run],
            cwd=tool,
            env=env | (overrides or {}),
            capture_output=True,
            text=True,
        )
        return result, capture, base, head

    def _assert_pair(self, capture: Path, base: str, head: str) -> None:
        arguments = capture.read_text().splitlines()
        self.assertEqual(arguments[arguments.index("--base-sha") + 1], base)
        self.assertEqual(arguments[arguments.index("--head-sha") + 1], head)
        self.assertIn("--fail-on-findings", arguments)

    def test_new_train_ref_executes_the_audit_against_its_original_base(self) -> None:
        for batch, repository in (
            (False, "example/product"),
            (True, "example/product"),
            (True, "Example/Mixed_Case"),
        ):
            with (
                self.subTest(batch=batch, repository=repository),
                TemporaryDirectory() as directory,
            ):
                result, capture, base, head = self._exercise(
                    Path(directory), batch=batch, repository=repository
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self._assert_pair(capture, base, head)
                self.assertFalse(json.loads(result.stdout)["gate"]["rejected_findings"])

    def test_authority_from_an_earlier_batch_entry_still_fails(self) -> None:
        with TemporaryDirectory() as directory:
            result, capture, base, head = self._exercise(Path(directory), batch=True, rejected=True)
            self.assertNotEqual(result.returncode, 0)
            self._assert_pair(capture, base, head)
            self.assertTrue(json.loads(result.stdout)["gate"]["rejected_findings"])

    def test_target_advancement_or_landing_does_not_hide_candidate_changes(self) -> None:
        for movement in ("advanced", "landed"):
            with self.subTest(movement=movement), TemporaryDirectory() as directory:
                result, capture, base, head = self._exercise(
                    Path(directory), rejected=True, target_movement=movement
                )
                self.assertNotEqual(result.returncode, 0)
                self._assert_pair(capture, base, head)
                self.assertTrue(json.loads(result.stdout)["gate"]["rejected_findings"])

    def test_missing_unrelated_or_same_head_target_refuses_before_audit(self) -> None:
        for movement in ("deleted", "unrelated", "fast_forward", "shallow"):
            with self.subTest(movement=movement), TemporaryDirectory() as directory:
                result, capture, _, _ = self._exercise(Path(directory), target_movement=movement)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture.exists())

    def test_unsupported_or_unavailable_creation_evidence_refuses_before_audit(self) -> None:
        for overrides in (
            {"EVENT_REF": "refs/heads/feature"},
            {"EVENT_REF": "refs/tags/launchplane/train/example/product/trunk/fixture-batch"},
            {"DEFAULT_BRANCH": "missing"},
            {"HEAD_SHA": "0" * 40},
            {"HEAD_SHA": "f" * 40},
            {"EVENT_NAME": "pull_request"},
        ):
            with self.subTest(overrides=overrides), TemporaryDirectory() as directory:
                result, capture, _, _ = self._exercise(Path(directory), overrides=overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture.exists())
