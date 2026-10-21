from dataclasses import dataclass, replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_ref
from tests.support.workflows import load_workflow


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass(frozen=True)
class CommentRangeHistory:
    root: Path
    base: str
    head: str
    before: str
    capture: Path


class CiCommentRangeTests(unittest.TestCase):
    def _history(
        self,
        root: Path,
        *,
        movement: str = "",
        reconstructed: bool = False,
        merge_new_default_into_source: bool = False,
        batch: bool = True,
    ) -> CommentRangeHistory:
        repository = root / "repository"
        repository.mkdir()
        _git(repository, "init", "-b", "trunk")
        _git(repository, "config", "user.name", "Workflow Fixture")
        _git(repository, "config", "user.email", "fixture@example.invalid")
        _git(repository, "config", "commit.gpgsign", "false")
        _git(repository, "config", "core.hooksPath", "/dev/null")
        (repository / "historical.py").write_text("# Historical prose\nVALUE = 1\n")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "base")
        base = _git(repository, "rev-parse", "HEAD")
        _git(repository, "checkout", "-b", "first")
        (repository / "first.py").write_text("# New prose\nVALUE = 2\n")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "first member")
        if merge_new_default_into_source:
            _git(repository, "checkout", "trunk")
            (repository / "new-default.py").write_text("VALUE = 3\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "new default")
            _git(repository, "checkout", "first")
            _git(repository, "merge", "--no-ff", "-m", "refresh source", "trunk")
        _git(repository, "checkout", "-b", "candidate", base)
        _git(repository, "merge", "--no-ff", "-m", "first rolling member", "first")
        if batch:
            _git(repository, "checkout", "-b", "second", base)
            (repository / "second.py").write_text("VALUE = 4\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "second member")
            _git(repository, "checkout", "candidate")
            _git(repository, "merge", "--no-ff", "-m", "second rolling member", "second")
        head = _git(repository, "rev-parse", "HEAD")
        before = "0" * 40
        if reconstructed:
            before = head
            parents = _git(repository, "show", "-s", "--format=%P", head).split()
            head = _git(
                repository,
                "commit-tree",
                f"{head}^{{tree}}",
                "-p",
                parents[0],
                "-p",
                parents[1],
                "-m",
                "reconstructed candidate",
            )
            _git(repository, "reset", "--hard", head)
        if movement in {"landed", "landed_then_advanced", "fast_forward"}:
            _git(repository, "checkout", "trunk")
            if movement == "fast_forward":
                _git(repository, "merge", "--ff-only", head)
            else:
                _git(repository, "merge", "--no-ff", "-m", "land candidate", head)
        if movement in {"advanced", "landed_then_advanced"}:
            _git(repository, "checkout", "trunk")
            (repository / "later.py").write_text("VALUE = 5\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "later default")
        if movement == "unrelated":
            _git(repository, "checkout", "--orphan", "unrelated")
            _git(repository, "rm", "-rf", ".")
            (repository / "unrelated.py").write_text("VALUE = 6\n")
            _git(repository, "add", ".")
            _git(repository, "commit", "-m", "unrelated root")
            _git(repository, "branch", "-D", "trunk")
            _git(repository, "branch", "-m", "trunk")
        _git(repository, "checkout", "candidate")
        if movement == "deleted":
            _git(repository, "branch", "-D", "trunk")
        remote = root / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(repository), str(remote)],
            check=True,
            capture_output=True,
        )
        _git(repository, "remote", "add", "origin", str(remote))
        if movement == "shallow":
            (repository / ".git/shallow").write_text(head + "\n")
        return CommentRangeHistory(repository, base, head, before, root / "scanner-arguments")

    def _exercise(
        self, history: CommentRangeHistory, **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        binaries = history.root.parent / "bin"
        binaries.mkdir(exist_ok=True)
        uv = binaries / "uv"
        uv.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE"\nexit "${SCANNER_EXIT:-0}"\n')
        uv.chmod(0o755)
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "repository_pins", "Check new comments and docstrings"
        )
        assert step is not None
        environment = os.environ | {
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "CAPTURE": str(history.capture),
            "EVENT_NAME": "push",
            "EVENT_REF": build_merge_train_batch_candidate_ref(
                repository="example/product", base_branch="trunk", batch_id="fixture"
            ),
            "GITHUB_REPOSITORY": "example/product",
            "DEFAULT_BRANCH": "trunk",
            "BASE_SHA": history.before,
            "HEAD_SHA": history.head,
        }
        return subprocess.run(
            ["bash", "-c", step.run],
            cwd=history.root,
            env=environment | overrides,
            capture_output=True,
            text=True,
        )

    def _assert_range(self, history: CommentRangeHistory, base: str) -> set[str]:
        arguments = history.capture.read_text().splitlines()
        selected_base = _git(history.root, "rev-parse", arguments[arguments.index("--base") + 1])
        self.assertEqual(selected_base, base)
        return set(_git(history.root, "diff", "--name-only", base, history.head).splitlines())

    def test_first_member_remains_in_range_before_and_after_landing(self) -> None:
        for movement in ("", "advanced", "landed", "landed_then_advanced"):
            with self.subTest(movement=movement), TemporaryDirectory() as directory:
                history = self._history(Path(directory), movement=movement)
                result = self._exercise(history)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self._assert_range(history, history.base), {"first.py", "second.py"}
                )
                self.assertEqual(
                    _git(history.root, "diff", "--name-only", "HEAD^1", "HEAD"), "second.py"
                )

    def test_reconstructed_and_singleton_candidates_retain_the_original_base(self) -> None:
        for batch in (False, True):
            with self.subTest(batch=batch), TemporaryDirectory() as directory:
                history = self._history(Path(directory), batch=batch, reconstructed=True)
                self.assertEqual(
                    _git(history.root, "rev-parse", f"{history.before}^{{tree}}"),
                    _git(history.root, "rev-parse", f"{history.head}^{{tree}}"),
                )
                result = self._exercise(history)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("first.py", self._assert_range(history, history.base))

    def test_source_side_default_merge_cannot_move_the_construction_base(self) -> None:
        with TemporaryDirectory() as directory:
            history = self._history(Path(directory), merge_new_default_into_source=True)
            result = self._exercise(history)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self._assert_range(history, history.base),
                {"first.py", "second.py", "new-default.py"},
            )

    def test_pr_and_multi_commit_push_cover_the_whole_contribution(self) -> None:
        for event in ("pull_request", "push"):
            with self.subTest(event=event), TemporaryDirectory() as directory:
                history = self._history(Path(directory))
                if event == "pull_request":
                    merge_head = _git(
                        history.root,
                        "commit-tree",
                        f"{history.head}^{{tree}}",
                        "-p",
                        history.base,
                        "-p",
                        history.head,
                        "-m",
                        "pull request test merge",
                    )
                    _git(history.root, "checkout", "--detach", merge_head)
                    history = replace(history, head=merge_head)
                result = self._exercise(
                    history,
                    EVENT_NAME=event,
                    EVENT_REF="refs/heads/trunk",
                    BASE_SHA=history.base,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self._assert_range(history, history.base), {"first.py", "second.py"}
                )

    def test_schedule_scans_previous_first_parent_and_scanner_failure_propagates(self) -> None:
        with TemporaryDirectory() as directory:
            history = self._history(Path(directory))
            result = self._exercise(history, EVENT_NAME="schedule", SCANNER_EXIT="17")
            self.assertEqual(result.returncode, 17, result.stderr)
            parent = _git(history.root, "rev-parse", "HEAD^1")
            self.assertEqual(self._assert_range(history, parent), {"second.py"})

    def test_missing_or_unsupported_history_refuses_before_scanning(self) -> None:
        for movement in ("deleted", "unrelated", "fast_forward", "shallow"):
            with self.subTest(movement=movement), TemporaryDirectory() as directory:
                history = self._history(Path(directory), movement=movement)
                result = self._exercise(history)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(history.capture.exists())

    def test_invalid_event_metadata_refuses_before_scanning(self) -> None:
        for overrides in (
            {"DEFAULT_BRANCH": ""},
            {"GITHUB_REPOSITORY": ""},
            {"EVENT_REF": "refs/heads/launchplane/train/example/product/release/fixture"},
            {"EVENT_REF": "refs/heads/launchplane/train/ordinary/fixture"},
            {"HEAD_SHA": "f" * 40},
            {"HEAD_SHA": "not-a-commit"},
            {"EVENT_NAME": "push", "EVENT_REF": "refs/heads/trunk", "BASE_SHA": "0" * 40},
            {"EVENT_NAME": "push", "EVENT_REF": "refs/heads/trunk", "BASE_SHA": "f" * 40},
            {"EVENT_NAME": "pull_request", "BASE_SHA": ""},
            {"EVENT_NAME": "workflow_dispatch"},
        ):
            with self.subTest(overrides=overrides), TemporaryDirectory() as directory:
                history = self._history(Path(directory))
                result = self._exercise(history, **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(history.capture.exists())
