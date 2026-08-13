from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.first_party_action_pins import (
    ActionPinError,
    FIRST_PARTY_ACTION_PROVENANCE,
    PrivilegedReferenceRefused,
    build_action_pin_report,
    launchplane_request_action_source,
    update_action_pins,
)


class FirstPartyActionPinTests(unittest.TestCase):
    def setUp(self) -> None:
        github_repository_patch = patch.dict(os.environ, {"GITHUB_REPOSITORY": ""})
        github_repository_patch.start()
        self.addCleanup(github_repository_patch.stop)

    def test_content_current_pin_passes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            release_sha = _initialize_repository(repo_root)

            report = build_action_pin_report(repo_root)

        self.assertEqual(report.status, "pass")
        self.assertEqual(report.violations, ())
        self.assertEqual({site.revision for site in report.references}, {release_sha})

    def test_unrelated_commit_does_not_make_pin_stale(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)
            (repo_root / "README.md").write_text("unrelated\n", encoding="utf-8")
            _commit(repo_root, "unrelated change")

            report = build_action_pin_report(repo_root)

        self.assertEqual(report.violations, ())

    def test_action_change_without_pin_sweep_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)
            _write_action(repo_root, "console.log('v2');\n")
            _commit(repo_root, "change action")

            report = build_action_pin_report(repo_root)

        self.assertIn(
            "action_pin_content_stale",
            {violation.code for violation in report.violations},
        )

    def test_missing_git_object_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)
            _write_workflow(repo_root, "f" * 40)
            _commit(repo_root, "point at missing action commit")

            report = build_action_pin_report(repo_root)

        self.assertIn(
            "action_pin_object_unavailable",
            {violation.code for violation in report.violations},
        )

    def test_wrong_provenance_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            release_sha = _initialize_repository(repo_root)
            _write_workflow(repo_root, release_sha, provenance="main")

            report = build_action_pin_report(repo_root)

        self.assertIn(
            "action_pin_invalid_provenance",
            {violation.code for violation in report.violations},
        )

    def test_dirty_action_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)
            _write_action(repo_root, "console.log('dirty');\n")

            report = build_action_pin_report(repo_root)

        self.assertIn(
            "action_worktree_dirty",
            {violation.code for violation in report.violations},
        )

    def test_yaml_workflow_is_discovered(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            release_sha = _initialize_repository(repo_root)
            workflow_path = repo_root / ".github/workflows/example.yml"
            workflow_path.rename(workflow_path.with_suffix(".yaml"))
            _commit(repo_root, "rename workflow extension")

            report = build_action_pin_report(repo_root)

        self.assertEqual(report.violations, ())
        self.assertEqual(report.references[0].revision, release_sha)

    def test_unreachable_content_equivalent_pin_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)
            main_sha = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
            _git(repo_root, "checkout", "--orphan", "side")
            _git(repo_root, "rm", "-rf", ".")
            _write_action(repo_root, "console.log('v1');\n")
            orphan_sha = _commit(repo_root, "orphan action identity")
            _git(repo_root, "checkout", "main")
            self.assertEqual(_git(repo_root, "rev-parse", "HEAD").stdout.strip(), main_sha)
            _write_workflow(repo_root, orphan_sha)
            _commit(repo_root, "point at unreachable action commit")

            report = build_action_pin_report(repo_root)

        self.assertIn(
            "action_pin_unreachable",
            {violation.code for violation in report.violations},
        )

    def test_update_rewrites_only_action_reference_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            stale_sha = _initialize_repository(repo_root)
            _write_action(repo_root, "console.log('v2');\n")
            release_sha = _commit(repo_root, "release v2 action")
            workflow_path = repo_root / ".github/workflows/example.yml"
            action_source = launchplane_request_action_source(repo_root)
            workflow_path.write_text(
                "---\n"
                "jobs:\n"
                "  call:\n"
                f"    uses: {action_source}@{stale_sha} # main\n"
                "  privileged:\n"
                "    uses: cbusillo/launchplane/.github/workflows/reusable-worker.yml@"
                f"{stale_sha} # main\n",
                encoding="utf-8",
            )
            privileged_line = workflow_path.read_text(encoding="utf-8").splitlines()[-1]

            changed_paths = update_action_pins(repo_root, release_sha=release_sha)
            second_changed_paths = update_action_pins(repo_root, release_sha=release_sha)
            updated_lines = workflow_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(changed_paths, (Path(".github/workflows/example.yml"),))
        self.assertEqual(second_changed_paths, ())
        self.assertTrue(
            any(
                line.strip()
                == (f"uses: {action_source}@{release_sha} # {FIRST_PARTY_ACTION_PROVENANCE}")
                for line in updated_lines
            )
        )
        self.assertEqual(updated_lines[-1], privileged_line)

    def test_update_rejects_content_mismatched_release(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            stale_sha = _initialize_repository(repo_root)
            _write_action(repo_root, "console.log('v2');\n")
            _commit(repo_root, "release v2 action")

            with self.assertRaisesRegex(ActionPinError, "does not match current action tree"):
                update_action_pins(repo_root, release_sha=stale_sha)

    def test_update_rejects_dirty_action(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            release_sha = _initialize_repository(repo_root)
            _write_action(repo_root, "console.log('dirty');\n")

            with self.assertRaisesRegex(ActionPinError, "Commit the launchplane-request"):
                update_action_pins(repo_root, release_sha=release_sha)

    def test_update_refuses_privileged_workflow_source(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            release_sha = _initialize_repository(repo_root)

            with self.assertRaises(PrivilegedReferenceRefused):
                update_action_pins(
                    repo_root,
                    release_sha=release_sha,
                    source=(
                        "cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml"
                    ),
                )

    def test_report_payload_is_deterministic(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)

            first = build_action_pin_report(repo_root).as_dict()
            second = build_action_pin_report(repo_root).as_dict()

        self.assertEqual(first, second)

    def test_github_repository_identifies_base_repo_for_fork_checkout(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            _initialize_repository(repo_root)

            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/base"}):
                action_source = launchplane_request_action_source(repo_root)

        self.assertEqual(
            action_source,
            "example/base/.github/actions/launchplane-request",
        )


def _initialize_repository(repo_root: Path) -> str:
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.name", "Launchplane Tests")
    _git(repo_root, "config", "user.email", "launchplane-tests@example.invalid")
    _git(repo_root, "remote", "add", "origin", "git@github.com:example/launchplane.git")
    _write_action(repo_root, "console.log('v1');\n")
    release_sha = _commit(repo_root, "release v1 action")
    _write_workflow(repo_root, release_sha)
    _commit(repo_root, "add action consumer")
    return release_sha


def _write_action(repo_root: Path, javascript: str) -> None:
    action_root = repo_root / ".github/actions/launchplane-request"
    (action_root / "dist").mkdir(parents=True, exist_ok=True)
    (action_root / "action.yml").write_text(
        "name: Launchplane Request\nruns:\n  using: node20\n  main: dist/index.js\n",
        encoding="utf-8",
    )
    (action_root / "dist/index.js").write_text(javascript, encoding="utf-8")


def _write_workflow(
    repo_root: Path,
    release_sha: str,
    *,
    provenance: str = FIRST_PARTY_ACTION_PROVENANCE,
) -> None:
    action_source = launchplane_request_action_source(repo_root)
    workflow_path = repo_root / ".github/workflows/example.yml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        f"---\njobs:\n  call:\n    uses: {action_source}@{release_sha} # {provenance}\n",
        encoding="utf-8",
    )


def _commit(repo_root: Path, message: str) -> str:
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", message)
    return _git(repo_root, "rev-parse", "HEAD").stdout.strip()


def _git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_DATE": "2026-08-13T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-08-13T00:00:00Z",
        },
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result
