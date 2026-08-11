from __future__ import annotations

from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import Workflow
from tests.support.workflows import load_workflow


WRAPPER_PATH = Path(".github/workflows/detached-application-retirement.yml")
WORKER_REFERENCE = (
    "cbusillo/launchplane/.github/workflows/"
    "reusable-detached-application-retirement.yml@"
    "11d53d2840a6f1898785d7c8f1553c202caa3fbf"
)
INPUT_NAMES = (
    "mode",
    "project_name",
    "environment_name",
    "application_name",
    "candidate_target_sha256",
    "expected_protected_target_sha256_json",
    "operator_idempotency_key",
    "reason",
    "related_issue",
    "reviewed_plan_record_id",
    "reviewed_plan_sha256",
    "confirmation",
)


def _load_pinned_workflow(reference: str) -> Workflow:
    source, separator, revision = reference.partition("@")
    if not separator:
        raise AssertionError(f"pinned workflow reference is missing a revision: {reference}")
    relative_path = Path(source).relative_to("cbusillo/launchplane")
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path.as_posix()}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(
            f"could not read {relative_path} at pinned revision {revision}: {result.stderr}"
        )
    with TemporaryDirectory() as temporary_directory_name:
        workflow_path = Path(temporary_directory_name) / relative_path.name
        workflow_path.write_text(result.stdout, encoding="utf-8")
        return load_workflow(workflow_path)


class DetachedApplicationRetirementOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wrapper = load_workflow(WRAPPER_PATH)
        self.worker_reference = self.wrapper.job_uses("retire")
        self.worker = _load_pinned_workflow(self.worker_reference)

    def test_wrapper_is_exactly_pinned_and_thin(self) -> None:
        trigger = self.wrapper.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        dispatch_inputs = dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        self.assertEqual(tuple(dispatch_inputs), INPUT_NAMES)
        self.assertEqual(self.worker_reference, WORKER_REFERENCE)
        self.assertRegex(self.worker_reference.rsplit("@", maxsplit=1)[1], r"^[0-9a-f]{40}$")
        self.assertEqual(self.wrapper.permissions, {})
        self.assertNotIn("concurrency", self.wrapper.data)
        self.assertEqual(set(self.wrapper.jobs), {"retire"})
        job = self.wrapper.job("retire")
        self.assertEqual(set(job), {"uses", "permissions", "with"})
        self.assertEqual(
            self.wrapper.job_permissions("retire"),
            {"contents": "read", "id-token": "write"},
        )
        for forbidden_key in ("environment", "runs-on", "steps", "concurrency"):
            self.assertNotIn(forbidden_key, job)
        wrapper_text = WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(
            wrapper_text,
            re.compile(r"(?:@(?:main|master|refs/heads/|v\d)|uses:\s*[^\n]+@(?![0-9a-f]{40}))"),
        )

    def test_wrapper_mirrors_worker_inputs_and_passes_each_unchanged(self) -> None:
        worker_trigger = self.worker.data["on"]
        assert isinstance(worker_trigger, dict)
        worker_call = worker_trigger["workflow_call"]
        assert isinstance(worker_call, dict)
        worker_inputs = worker_call["inputs"]
        assert isinstance(worker_inputs, dict)
        wrapper_trigger = self.wrapper.data["on"]
        assert isinstance(wrapper_trigger, dict)
        wrapper_dispatch = wrapper_trigger["workflow_dispatch"]
        assert isinstance(wrapper_dispatch, dict)
        wrapper_inputs = wrapper_dispatch["inputs"]
        assert isinstance(wrapper_inputs, dict)
        self.assertEqual(tuple(worker_inputs), INPUT_NAMES)
        self.assertEqual(tuple(wrapper_inputs), INPUT_NAMES)
        for name in INPUT_NAMES:
            worker_input = worker_inputs[name]
            wrapper_input = wrapper_inputs[name]
            assert isinstance(worker_input, dict)
            assert isinstance(wrapper_input, dict)
            self.assertEqual(wrapper_input["description"], worker_input["description"])
            self.assertEqual(wrapper_input["required"], worker_input["required"])
            self.assertEqual(wrapper_input["type"], worker_input["type"])
            if "default" in worker_input:
                self.assertEqual(wrapper_input["default"], worker_input["default"])
        wrapper_values = self.wrapper.job("retire")["with"]
        assert isinstance(wrapper_values, dict)
        self.assertEqual(
            wrapper_values,
            {name: f"${{{{ inputs.{name} }}}}" for name in INPUT_NAMES},
        )

    def test_caller_and_worker_authz_contract_is_explicit(self) -> None:
        self.assertEqual(
            self.worker.permissions,
            {"contents": "read", "id-token": "write"},
        )
        worker_job = self.worker.job("retire")
        self.assertEqual(
            self.worker.job_permissions("retire"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(worker_job["environment"], "launchplane-authz-admin")
        validation = self.worker.step_named("retire", "Validate exact detached application intent")
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertIn("github.repository", validation.run)
        self.assertIn("cbusillo/launchplane", validation.run)
        self.assertIn("LAUNCHPLANE_URL", validation.run)
        self.assertIn("candidate_target_sha256", validation.run)
        self.assertIn("EXPECTED_PROTECTED_TARGET_SHA256_JSON", validation.run)

    def test_operations_documentation_binds_exact_caller_and_worker(self) -> None:
        operations = Path("docs/operations.md").read_text(encoding="utf-8")
        self.assertIn(
            "workflow_ref=cbusillo/launchplane/.github/workflows/"
            "detached-application-retirement.yml@refs/heads/main",
            operations,
        )
        self.assertIn(f"job_workflow_ref={WORKER_REFERENCE}", operations)


if __name__ == "__main__":
    unittest.main()
