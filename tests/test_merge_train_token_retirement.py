import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.privileged_operation import ManagedMergeTrainPolicyImportProposalInput
from control_plane.http_app import MergeTrainPolicyImportEnvelope
from control_plane.privileged_operation_registry import (
    PrivilegedOperationPlannerError,
    plan_managed_merge_train_policy_import,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.workflows import load_workflow
from control_plane.contracts.merge_train_policy import parse_merge_train_policy_toml


class MergeTrainTokenRetirementTests(unittest.TestCase):
    def test_imports_refuse_historical_env_source_before_storage(self) -> None:
        payload = build_test_merge_train_policy_record().model_dump(mode="json")
        payload["policy"]["policies"][0]["github_token"] = {"env_var": "GH_TOKEN"}
        payload["policy_sha256"] = ""
        historical = MergeTrainPolicyRecord.model_validate(payload)
        for mode in ("dry_run", "apply"):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValidationError, "env_var token source is retired"),
            ):
                MergeTrainPolicyImportEnvelope(
                    mode=mode, reason="Review replacement", record=historical
                )
        store = Mock()
        with self.assertRaisesRegex(
            PrivilegedOperationPlannerError, "env_var token source is retired"
        ):
            plan_managed_merge_train_policy_import(
                store,
                ManagedMergeTrainPolicyImportProposalInput(
                    record=historical,
                    reason="Review replacement",
                    related_issue="example/repo#1",
                ),
            )
        self.assertEqual(store.mock_calls, [])

    def build_workflow_policy(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        workflow = load_workflow(Path(".github/workflows/merge-train-policy-import.yml"))
        step = workflow.step_named("import", "Build policy payload")
        assert step is not None
        program = step.run.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\nPY", 1)[0]
        values = {
            "REPOSITORY": "example/repo",
            "BASE_BRANCH": "main",
            "ENQUEUE_LABEL": "ready",
            "BLOCKED_LABEL": "blocked",
            "STACK_CHILD_DISPOSITION_LABEL": "landed",
            "MERGE_METHOD": "merge",
            "FAILURE_POLICY": "pause_train",
            "TRUSTED_AUTOMATION_GITHUB_USER_IDS": "",
            "GITHUB_APP_ID": "42",
            "GITHUB_REPOSITORY_ID": "123",
            "PRIVATE_KEY_CONTEXT": "example_context",
        }
        values.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", program],
            env=os.environ | {f"POLICY_{key}": value for key, value in values.items()},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_workflow_builds_valid_explicit_app_policy(self) -> None:
        result = self.build_workflow_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = parse_merge_train_policy_toml(result.stdout).policies[0]
        self.assertEqual(target.merge_identity.kind, "github_app")
        self.assertEqual(target.github_token.env_var, "")
        app = target.github_token.github_app
        assert app is not None
        self.assertEqual(
            (app.app_id, app.repository_id, app.private_key_context), (42, 123, "example_context")
        )

    def test_workflow_rejects_invalid_app_scope_metadata(self) -> None:
        for inputs in (
            {"GITHUB_APP_ID": ""},
            {"GITHUB_APP_ID": "0"},
            {"GITHUB_REPOSITORY_ID": "-1"},
            {"PRIVATE_KEY_CONTEXT": ""},
            {"PRIVATE_KEY_CONTEXT": "context\ninjected"},
        ):
            with self.subTest(inputs=inputs):
                result = self.build_workflow_policy(**inputs)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
