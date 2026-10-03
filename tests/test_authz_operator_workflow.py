from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.authz_grant_service import AuthzManagedPolicyReconcileEnvelope
from tests.support.workflows import load_workflow


class AuthzOperatorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/reusable-authz-policy-reconcile.yml")
        self.dispatch_workflow = load_workflow(".github/workflows/authz-policy-reconcile.yml")

    def test_dispatch_managed_set_options_each_select_one_reconcile_job(self) -> None:
        trigger = self.dispatch_workflow.data["on"]
        assert isinstance(trigger, dict)
        dispatch = trigger["workflow_dispatch"]
        assert isinstance(dispatch, dict)
        dispatch_inputs = dispatch["inputs"]
        assert isinstance(dispatch_inputs, dict)
        managed_set_input = dispatch_inputs["managed_set"]
        assert isinstance(managed_set_input, dict)
        raw_options = managed_set_input["options"]
        assert isinstance(raw_options, list)
        options = [str(option) for option in raw_options]

        self.assertEqual(
            set(self.dispatch_workflow.jobs),
            {f"reconcile-{option}" for option in options},
        )
        for option in options:
            with self.subTest(managed_set=option):
                job = self.dispatch_workflow.job(f"reconcile-{option}")
                self.assertEqual(job["if"], f"${{{{ inputs.managed_set == '{option}' }}}}")
                job_inputs = job["with"]
                assert isinstance(job_inputs, dict)
                self.assertEqual(job_inputs["expected_managed_set_id"], f"operator.{option}")

    def test_privileged_operation_bootstrap_is_exact_and_fails_closed(self) -> None:
        job = self.dispatch_workflow.job("reconcile-privileged-operation-bootstrap")
        secrets = job["secrets"]
        assert isinstance(secrets, dict)
        managed_set_json = secrets["managed_set_json"]
        assert isinstance(managed_set_json, str)
        match = re.search(
            r"format\(\s*'(.*)',\s*github\.event\.repository\.owner\.type",
            managed_set_json,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        assert match is not None
        template = match.group(1)

        def render(owner_id: str, subject: str, token_label: str) -> dict[str, object]:
            payload = template.replace("{{", "\x00").replace("}}", "\x01")
            payload = payload.replace("{0}", owner_id)
            payload = payload.replace("{1}", subject)
            payload = payload.replace("{2}", token_label)
            payload = payload.replace("\x00", "{").replace("\x01", "}")
            parsed = json.loads(payload)
            assert isinstance(parsed, dict)
            return parsed

        configuration = render("123", json.dumps("terminal-agent"), json.dumps("owner"))
        envelope = AuthzManagedPolicyReconcileEnvelope.model_validate(
            {
                **configuration,
                "mode": "dry_run",
                "reason": "Review the explicit bootstrap canary.",
                "related_issue": "cbusillo/launchplane#2204",
            }
        )
        self.assertEqual(
            set(envelope.desired_policy.github_humans[0].actions),
            {
                "authz_policy_operation.read",
                "authz_policy_operation.cancel",
                "authz_policy_operation.approve",
                "authz_policy_operation.revoke",
                "authz_policy_grant.write",
            },
        )
        self.assertEqual(envelope.desired_policy.github_humans[0].github_ids, (123,))
        self.assertEqual(
            set(envelope.desired_policy.terminal_agents[0].actions),
            {"authz_policy_operation.propose"},
        )
        self.assertEqual(
            envelope.desired_policy.terminal_agents[0].subjects,
            ("terminal-agent",),
        )
        self.assertEqual(envelope.desired_policy.terminal_agents[0].token_labels, ("owner",))

        for values in (
            ("null", json.dumps("terminal-agent"), json.dumps("owner")),
            ("123", "null", json.dumps("owner")),
            ("123", json.dumps("terminal-agent"), "null"),
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AuthzManagedPolicyReconcileEnvelope.model_validate(
                    {
                        **render(*values),
                        "mode": "dry_run",
                        "reason": "Reject incomplete bootstrap selectors.",
                        "related_issue": "cbusillo/launchplane#2204",
                    }
                )

    def test_render_step_builds_review_bound_requests(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "schema_migration": "migrate_v1_to_v2",
            "unmanaged_adoption": "adopt_matching",
            "desired_policy": {"schema_version": 2},
        }
        with TemporaryDirectory() as temporary_directory:
            output_file = Path(temporary_directory) / "github-output"
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(output_file),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "EXPECTED_MANAGED_SET_ID": "operator.launchplane",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "dry_run",
                    "REASON": "Review the managed Launchplane authority set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1774",
                    "REVIEWED_PLAN_SHA256": "",
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = dict(
                line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines()
            )
            request = json.loads(Path(outputs["request_file"]).read_text(encoding="utf-8"))
            self.assertEqual(request["mode"], "dry_run")
            self.assertEqual(request["reason"], "Review the managed Launchplane authority set.")
            self.assertEqual(request["related_issue"], "cbusillo/launchplane#1774")
            self.assertEqual(request["reviewed_plan_sha256"], "")
            self.assertEqual(outputs["idempotency_key"], "")
            self.assertRegex(outputs["configuration_sha256"], r"^[0-9a-f]{64}$")
            evidence_directory = Path(outputs["evidence_directory"])
            self.assertNotEqual(Path(outputs["request_file"]).parent, evidence_directory)
            request_summary = json.loads(
                (evidence_directory / "request-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request_summary["managed_set_id"], "operator.launchplane")
            self.assertNotIn("desired_policy", request_summary)

    def test_render_step_rejects_unexpected_managed_set_id(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.primary",
            "desired_policy": {"schema_version": 2},
        }
        with TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "EXPECTED_MANAGED_SET_ID": "operator.unrelated-test-set",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(Path(temporary_directory) / "github-output"),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "dry_run",
                    "REASON": "Review an unrelated authorization set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1919",
                    "REVIEWED_PLAN_SHA256": "",
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "managed_set_id does not match the selected managed set",
            result.stderr,
        )

    def test_render_step_rejects_unreviewed_apply(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "desired_policy": {"schema_version": 2},
        }
        with TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(Path(temporary_directory) / "github-output"),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "apply",
                    "REASON": "Apply the reviewed managed Launchplane authority set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1774",
                    "REVIEWED_PLAN_SHA256": "",
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("apply requires the reviewed dry-run plan SHA-256", result.stderr)

    def test_render_step_rejects_operator_fields_in_managed_config(self) -> None:
        render_step = self.workflow.step_named(
            "reconcile", "Validate and render managed authz request"
        )
        self.assertIsNotNone(render_step)
        assert render_step is not None
        configuration = {
            "schema_version": 2,
            "product": "launchplane",
            "managed_set_id": "operator.launchplane",
            "desired_policy": {"schema_version": 2},
            "mode": "apply",
        }
        with TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                ["bash", "-ceu", render_step.run],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_OUTPUT": str(Path(temporary_directory) / "github-output"),
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": "1234",
                    "LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON": json.dumps(configuration),
                    "MODE": "apply",
                    "REASON": "Apply the reviewed managed Launchplane authority set.",
                    "RELATED_ISSUE": "cbusillo/launchplane#1774",
                    "REVIEWED_PLAN_SHA256": "a" * 64,
                    "RUNNER_TEMP": temporary_directory,
                },
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("contains unsupported fields: mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
