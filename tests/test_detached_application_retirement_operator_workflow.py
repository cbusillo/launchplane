from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import Workflow
from tests.support.workflows import load_workflow


WRAPPER_PATH = Path(".github/workflows/detached-application-retirement.yml")


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

    def test_wrapper_mirrors_pinned_worker_inputs(self) -> None:
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
        wrapper_values = self.wrapper.job("retire")["with"]
        assert isinstance(wrapper_values, dict)

        self.assertEqual(set(wrapper_inputs), set(worker_inputs))
        self.assertEqual(set(wrapper_values), set(worker_inputs))
        for name, worker_input in worker_inputs.items():
            wrapper_input = wrapper_inputs[name]
            assert isinstance(worker_input, dict)
            assert isinstance(wrapper_input, dict)
            self.assertEqual(wrapper_input["required"], worker_input["required"])
            self.assertEqual(wrapper_input["type"], worker_input["type"])
            if "default" in worker_input:
                self.assertEqual(wrapper_input["default"], worker_input["default"])

    def test_operations_documentation_binds_exact_caller_and_worker(self) -> None:
        operations = Path("docs/operations.md").read_text(encoding="utf-8")
        self.assertIn(
            f"workflow_ref=cbusillo/launchplane/{WRAPPER_PATH.as_posix()}@refs/heads/main",
            operations,
        )
        self.assertIn(f"job_workflow_ref={self.worker_reference}", operations)


if __name__ == "__main__":
    unittest.main()
