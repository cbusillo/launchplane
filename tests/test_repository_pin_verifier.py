from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.verify_repository_pins import WRAPPER_PATHS, verify_worker_pins
from tests.test_first_party_action_pins import (
    _initialize_repository,
    _commit,
    _git,
    _write_workflow,
)
from control_plane.first_party_action_pins import build_action_pin_report


class RepositoryPinVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        repository_environment = patch.dict(os.environ, {"GITHUB_REPOSITORY": ""})
        repository_environment.start()
        self.addCleanup(repository_environment.stop)

    def test_worker_agreement_uses_requested_revision_and_repository(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_repository(root)
            worker = root / ".github/workflows/worker.yml"
            worker.write_text(
                '"on":\n  workflow_call:\n    inputs:\n      target:\n        type: string\n        required: true\n'
            )
            pin = _commit(root, "worker")
            for path in WRAPPER_PATHS:
                (root / path).write_text(
                    '"on":\n  workflow_dispatch:\n    inputs:\n      target:\n        type: string\n        required: true\n'
                    "jobs:\n  retire:\n"
                    f"    uses: example/launchplane/.github/workflows/worker.yml@{pin}\n"
                    "    with:\n      target: fixture\n"
                )
            revision = _commit(root, "wrappers")
            _git(root, "remote", "set-url", "origin", "git@github.com:fork/other.git")
            worker.write_text("uncommitted unrelated worker\n")
            self.assertEqual(verify_worker_pins(root, revision, "example/launchplane"), [])
            wrapper = root / WRAPPER_PATHS[0]
            wrapper.write_text(wrapper.read_text().replace("required: true", "required: false"))
            _commit(root, "mismatched required flag")
            self.assertTrue(verify_worker_pins(root, "HEAD", "example/launchplane"))
            self.assertEqual(verify_worker_pins(root, revision, "example/launchplane"), [])
            with self.assertRaises(ValueError):
                verify_worker_pins(root, "HEAD", "fork/other")

    def test_action_verification_ignores_dirty_consumers_and_fork_remote(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_repository(root)
            revision = _git(root, "rev-parse", "HEAD").stdout.strip()
            _write_workflow(root, "f" * 40)
            _git(root, "remote", "set-url", "origin", "git@github.com:fork/other.git")
            report = build_action_pin_report(
                root, target_revision=revision, repository="example/launchplane"
            )
            self.assertEqual(report.violations, ())
            _commit(root, "unavailable pin")
            report = build_action_pin_report(
                root, target_revision="HEAD", repository="example/launchplane"
            )
            self.assertIn(
                "action_pin_object_unavailable", {item.code for item in report.violations}
            )
            self.assertEqual(
                build_action_pin_report(
                    root, target_revision=revision, repository="example/launchplane"
                ).violations,
                (),
            )
