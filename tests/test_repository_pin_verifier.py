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
                '"on":\n  workflow_call:\n    inputs:\n      target:\n        type: string\n        required: True\n'
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
            wrapper.write_text(
                wrapper.read_text().replace(
                    "required: false", "required: true\n        default: unforwarded"
                )
            )
            _commit(root, "wrapper-only default")
            self.assertTrue(verify_worker_pins(root, "HEAD", "example/launchplane"))
            wrapper.write_text(wrapper.read_text().replace(pin, "f" * 40))
            _commit(root, "missing pinned worker")
            with self.assertRaises(ValueError):
                verify_worker_pins(root, "HEAD", "example/launchplane")

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

    def test_reusable_gate_uses_exact_event_pair_and_refuses_unmapped_events(self) -> None:
        from tests.support.workflows import load_workflow
        import subprocess

        step = load_workflow(
            ".github/workflows/reusable-product-repo-config-authority.yml"
        ).step_named("launchplane-config-authority", "Run Launchplane config authority gate")
        assert step is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "uv"
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
            stub.chmod(0o755)
            git_stub = root / "git"
            git_stub.write_text(
                "#!/bin/sh\n"
                'if [ "$3" = cat-file ]; then exit "${GIT_MISSING:-0}"; fi\n'
                'if [ "$3" = fetch ]; then printf "%s\\n" "$@" >> "$FETCH_CAPTURE"; exit "${FETCH_FAILURE:-0}"; fi\n'
                "exit 2\n"
            )
            git_stub.chmod(0o755)
            capture = root / "capture"
            base, head = "a" * 40, "b" * 40
            env = os.environ | {
                "PATH": f"{root}:{os.environ['PATH']}",
                "CAPTURE": str(capture),
                "EVENT_NAME": "pull_request",
                "BASE_SHA": base,
                "HEAD_SHA": head,
                "FAIL_ON_FINDINGS": "true",
            }
            for event in ("pull_request", "push", "merge_group"):
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=env | {"EVENT_NAME": event},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = capture.read_text().splitlines()
                self.assertEqual(arguments[arguments.index("--base-sha") + 1], base)
                self.assertEqual(arguments[arguments.index("--head-sha") + 1], head)
                capture.unlink()
            for event in ("workflow_dispatch", "pull_request_target", "schedule"):
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=env | {"EVENT_NAME": event},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture.exists())
            fetch_capture = root / "fetch-capture"
            result = subprocess.run(
                ["bash", "-c", step.run],
                env=env | {"GIT_MISSING": "1", "FETCH_CAPTURE": str(fetch_capture)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(base, fetch_capture.read_text().splitlines())
            self.assertIn(head, fetch_capture.read_text().splitlines())
            capture.unlink()
            result = subprocess.run(
                ["bash", "-c", step.run],
                env=env
                | {"GIT_MISSING": "1", "FETCH_CAPTURE": str(fetch_capture), "FETCH_FAILURE": "1"},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(capture.exists())
            for key in ("BASE_SHA", "HEAD_SHA"):
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=env | {key: "0" * 40},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture.exists())

    def test_detached_type_agreement_and_product_mode_exception_are_preserved(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _initialize_repository(root)
            worker = root / ".github/workflows/worker.yml"
            worker.write_text(
                '"on":\n  workflow_call:\n    inputs:\n      mode:\n        type: string\n        required: false\n        default: plan\n'
            )
            pin = _commit(root, "mode worker")
            for path in WRAPPER_PATHS:
                mode_type, default = (
                    ("string", "plan") if "detached" in path else ("choice", "apply")
                )
                (root / path).write_text(
                    '"on":\n  workflow_dispatch:\n    inputs:\n      mode:\n'
                    f"        type: {mode_type}\n        required: false\n        default: {default}\n"
                    "jobs:\n  retire:\n"
                    f"    uses: example/launchplane/.github/workflows/worker.yml@{pin}\n"
                    "    with:\n      mode: fixture\n"
                )
            _commit(root, "distinct wrapper mode contracts")
            self.assertEqual(verify_worker_pins(root, "HEAD", "example/launchplane"), [])
            detached = root / WRAPPER_PATHS[1]
            detached.write_text(detached.read_text().replace("type: string", "type: choice"))
            _commit(root, "wrong detached input type")
            self.assertTrue(verify_worker_pins(root, "HEAD", "example/launchplane"))
