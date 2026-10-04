import subprocess
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.support.workflows import Workflow
from tests.support.workflows import load_workflow


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


class ProductRetirementOperatorWorkflowTests(unittest.TestCase):
    def test_no_target_worker_validates_and_builds_the_exact_service_intent(self) -> None:
        worker = load_workflow(".github/workflows/reusable-product-retirement.yml")
        validate = worker.step_named("retire", "Validate exact retirement intent")
        build = worker.step_named("retire", "Build retirement request")
        assert validate is not None and build is not None
        env = {
            **os.environ,
            "LAUNCHPLANE_URL": "https://service.invalid",
            "PRODUCT": "example-site",
            "INSTANCE": "testing",
            "MODE": "apply",
            "NO_TARGET": "true",
            "EXPECTED_TARGET_SHA256": "",
            "OPERATOR_IDEMPOTENCY_KEY": "retire-example",
            "REASON": "Retire unused records.",
            "RELATED_ISSUE": "example/repo#1",
            "REVIEWED_PLAN_RECORD_ID": "reviewed-plan",
            "REVIEWED_PLAN_SHA256": "a" * 64,
            "CONFIRMATION": "retire product example-site instance testing with no target",
        }
        with TemporaryDirectory() as directory:
            checked = subprocess.run(
                ["bash", "-c", validate.run], cwd=directory, env=env, capture_output=True, text=True
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            built = subprocess.run(
                ["bash", "-c", build.run], cwd=directory, env=env, capture_output=True, text=True
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            payload = json.loads((Path(directory) / "product-retirement-request.json").read_text())
            from control_plane.contracts.product_retirement import ProductRetirementRequest

            intent = ProductRetirementRequest.model_validate(payload)
            self.assertTrue(intent.no_target)
            self.assertEqual(intent.confirmation, intent.expected_confirmation)
            for changes in (
                {"CONFIRMATION": "retire an application"},
                {"EXPECTED_TARGET_SHA256": "b" * 64},
                {"NO_TARGET": "false"},
            ):
                rejected = subprocess.run(
                    ["bash", "-c", validate.run],
                    cwd=directory,
                    env={**env, **changes},
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(rejected.returncode, 0)

    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/product-retirement.yml")
        self.worker_reference = self.workflow.job_uses("retire")
        self.reusable_workflow = _load_pinned_workflow(self.worker_reference)

    def test_wrapper_dispatch_inputs_agree_with_pinned_worker(self) -> None:
        reusable_trigger = self.reusable_workflow.data["on"]
        assert isinstance(reusable_trigger, dict)
        reusable_call = reusable_trigger["workflow_call"]
        assert isinstance(reusable_call, dict)
        reusable_inputs = reusable_call["inputs"]
        assert isinstance(reusable_inputs, dict)

        wrapper_trigger = self.workflow.data["on"]
        assert isinstance(wrapper_trigger, dict)
        dispatch = wrapper_trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        dispatch_inputs = dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        forwarded = self.workflow.job("retire")["with"]
        assert isinstance(forwarded, dict)

        self.assertEqual(set(dispatch_inputs), set(reusable_inputs))
        self.assertEqual(set(forwarded), set(reusable_inputs))
        for name, dispatch_input in dispatch_inputs.items():
            assert isinstance(dispatch_input, dict)
            reusable_input = reusable_inputs[name]
            assert isinstance(reusable_input, dict)
            self.assertEqual(reusable_input["required"], dispatch_input["required"])
            if name != "mode" and "default" in dispatch_input:
                self.assertEqual(reusable_input["default"], dispatch_input["default"])


if __name__ == "__main__":
    unittest.main()
