import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


class SetupAllocationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "outputs"

    def caller_env(self, control: object = None, *, mode: str = "dry_run") -> dict[str, str]:
        return {
            **dict.fromkeys(
                ("TESTING_CONTEXT", "PREVIEW_CONTEXT", "TARGET_NAME", "PROJECT_NAME", "SERVER_ID"),
                "",
            ),
            "MODE": mode,
            "CONFIRMATION": "APPLY GENERIC WEB ONBOARDING" if mode == "apply" else "",
            "ALLOCATION_CONTROL": "" if control is None else json.dumps(control),
            "PRODUCT": "fixture",
            "DISPLAY_NAME": "Fixture",
            "REPOSITORY": "fixture-owner/fixture",
            "IMAGE_REPOSITORY": "registry.example/fixture",
            "RUNTIME_PORT": "8080",
            "HEALTH_PATH": "/healthz",
            "PREVIEW_BASE_URL": "https://preview.example.com",
            "ENVIRONMENT_NAME": "testing",
            "DOMAIN_CERTIFICATE_TYPE": "none",
            "REASON": "fixture recovery",
            "RELATED_ISSUE": "fixture issue",
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
        }

    def caller_step(self, name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return self.run_step("product-onboarding.yml", "plan", name, env)

    def output_values(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def test_caller_classifies_recovery_and_requires_apply_confirmation(self) -> None:
        for control, mode, recovery in (
            ({"mode": "readback"}, "dry_run", "true"),
            ({"mode": "apply"}, "apply", "true"),
            (
                {
                    "mode": "apply",
                    "retain_allocation": True,
                    "expected_allocation_custody_sha256": "a" * 64,
                },
                "apply",
                "true",
            ),
        ):
            with self.subTest(control=control, mode=mode):
                result = self.caller_step(
                    "Validate and normalize typed inputs", self.caller_env(control, mode=mode)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.output_values()["recovery"], recovery)
        invalid_controls: tuple[object, ...] = (
            {"mode": "apply"},
            {"mode": "other"},
            {"mode": None},
            {
                "mode": "readback",
                "retain_allocation": True,
                "expected_allocation_custody_sha256": "a" * 64,
            },
            {"idempotency_kye": "key"},
            {"idempotency_key": 5},
            {"idempotency_key": "key\n"},
            {"retain_allocation": "true"},
            [],
            "readback",
        )
        for invalid_control in invalid_controls:
            with self.subTest(control=invalid_control):
                result = self.caller_step(
                    "Validate and normalize typed inputs", self.caller_env(invalid_control)
                )
                self.assertNotEqual(result.returncode, 0)
        for control in (
            {"mode": "apply", "retain_allocation": True},
            {"mode": "apply", "expected_allocation_custody_sha256": "a" * 64},
        ):
            with self.subTest(control=control):
                result = self.caller_step(
                    "Validate and normalize typed inputs", self.caller_env(control, mode="apply")
                )
                self.assertNotEqual(result.returncode, 0)
        env = self.caller_env({"mode": "apply"}, mode="apply")
        env["CONFIRMATION"] = ""
        self.assertNotEqual(
            self.caller_step("Validate and normalize typed inputs", env).returncode, 0
        )

    def render_caller(self, control: object = None) -> dict[str, str]:
        env = self.caller_env(control, mode="apply")
        result = self.caller_step("Validate and normalize typed inputs", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = self.output_values()
        env.update(
            NORMALIZED_PRODUCT=outputs["product"],
            NORMALIZED_PROJECT_NAME=outputs["project_name"],
            NORMALIZED_TARGET_NAME=outputs["target_name"],
            NORMALIZED_TESTING_CONTEXT=outputs["testing_context"],
        )
        result = self.caller_step("Render plan requests", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return env

    def test_caller_recovery_finalizes_only_original_target_artifact(self) -> None:
        target = self.root / "target-plan-request.json"
        original_bytes = Path("tests/fixtures/onboarding-target-plan.json").read_bytes()
        for mode in ("readback", "apply"):
            with self.subTest(mode=mode):
                env = self.render_caller({"mode": mode, "idempotency_key": "original-key"})
                self.assertEqual(target.read_bytes(), original_bytes)
                self.assertEqual({path.name for path in self.root.glob("*.json")}, {target.name})
                result = self.caller_step("Finalize onboarding plan", env)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs = self.output_values()
                self.assertEqual(
                    outputs["target_plan_sha256"], hashlib.sha256(original_bytes).hexdigest()
                )

    def test_caller_refuses_new_onboarding_before_any_request(self) -> None:
        for control in (None, {}, {"idempotency_key": "fresh-reviewed-key"}):
            for mode in ("dry_run", "apply"):
                with self.subTest(control=control, mode=mode):
                    result = self.caller_step(
                        "Validate and normalize typed inputs", self.caller_env(control, mode=mode)
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(self.output.exists())
                    self.assertEqual(list(self.root.glob("*.json")), [])

    def run_step(
        self, workflow: str, job: str, name: str, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        step = load_workflow(Path(".github/workflows") / workflow).step_named(job, name)
        assert step is not None
        return subprocess.run(
            ["bash", "-c", step.run],
            cwd=self.root,
            env={**os.environ, "GITHUB_OUTPUT": str(self.output), **env},
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def setup_request(
        self, control: object, *, mode: str = "apply", attempt: str = "1"
    ) -> subprocess.CompletedProcess[str]:
        env = dict.fromkeys(
            (
                "TARGET_ID",
                "TARGET_NAME",
                "PROJECT_ID",
                "PROJECT_NAME",
                "ENVIRONMENT_ID",
                "ENVIRONMENT_NAME",
                "SERVER_ID",
                "APP_NAME",
                "DESCRIPTION",
                "SOURCE_GIT_REF",
                "SOURCE_TYPE",
                "COMPOSE_PATH",
                "DOMAIN",
                "RUNTIME_PORT",
                "HEALTHCHECK_PATH",
                "DEPLOY_TIMEOUT_SECONDS",
                "EXPECTED_CURRENT_PROVIDER_TARGET_JSON",
                "CONFIRMATION",
                "REASON",
            ),
            "",
        )
        env.update(
            MODE=mode,
            OPERATION="create-application",
            CONTEXT="fixture-testing",
            INSTANCE="testing",
            TARGET_TYPE="application",
            GITHUB_RUN_ID="101",
            GITHUB_RUN_ATTEMPT=attempt,
            ALLOCATION_CONTROL=json.dumps(control),
        )
        return self.run_step(
            "dokploy-target-setup.yml", "setup", "Prepare target setup request", env
        )

    def test_setup_reuses_explicit_key_and_renders_readback_and_retention(self) -> None:
        control = {"idempotency_key": "original-key"}
        first = self.setup_request(control)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_output = self.output.read_text()
        self.output.unlink()
        rerun = self.setup_request(control, attempt="2")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(self.output.read_text(), first_output)
        readback = self.setup_request(control, mode="readback")
        self.assertEqual(readback.returncode, 0, readback.stderr)
        self.assertEqual(
            json.loads((self.root / "dokploy-target-setup-payload.json").read_text())["mode"],
            "readback",
        )
        retained = self.setup_request(
            {**control, "retain_allocation": True, "expected_allocation_custody_sha256": "a" * 64}
        )
        self.assertEqual(retained.returncode, 0, retained.stderr)
        self.assertTrue(
            json.loads((self.root / "dokploy-target-setup-payload.json").read_text())[
                "retain_allocation"
            ]
        )

    def test_setup_refuses_missing_key_or_open_control_before_request(self) -> None:
        for control in (
            {},
            {"idempotency_key": "key", "context": "other"},
            {"idempotency_key": "key\ninjected=value"},
            {"idempotency_key": "key\n"},
            {"idempotency_key": "key", "retain_allocation": "true"},
            {"idempotency_key": "key", "retain_allocation": True},
        ):
            with self.subTest(control=control):
                self.assertNotEqual(self.setup_request(control).returncode, 0)
                self.assertFalse((self.root / "dokploy-target-setup-payload.json").exists())

    def onboarding_request(
        self, control: object, *, context: str = "fixture-testing"
    ) -> subprocess.CompletedProcess[str]:
        target = self.root / "target-plan-request.json"
        target.write_text(
            json.dumps(
                {
                    "mode": "dry-run",
                    "operation": "create-application",
                    "context": context,
                    "instance": "testing",
                }
            )
        )
        (self.root / "onboarding-plan.json").write_text(
            json.dumps({"result": {"plan_sha256": "records-plan"}})
        )
        if isinstance(control, dict) and "mode" in control:
            (self.root / "onboarding-plan.json").unlink()
        return self.run_step(
            "reusable-generic-web-onboarding-apply.yml",
            "apply",
            "Render apply requests",
            {
                "PRODUCT": "fixture",
                "TESTING_CONTEXT": context,
                "REASON": "fixture recovery",
                "ONBOARDING_PLAN_SHA256": "records-plan",
                "TARGET_PLAN_SHA256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "ALLOCATION_CONTROL": "" if control is None else json.dumps(control),
            },
        )

    def test_onboarding_recovery_only_renders_original_target_request(self) -> None:
        normal = self.onboarding_request(None)
        self.assertEqual(normal.returncode, 0, normal.stderr)
        original_outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        original_request = json.loads((self.root / "target-apply-request.json").read_text())
        for mode in ("readback", "apply"):
            with self.subTest(mode=mode):
                result = self.onboarding_request({"mode": mode})
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads((self.root / "target-apply-request.json").read_text())
                self.assertEqual(payload["mode"], mode)
                self.assertFalse((self.root / "authz-apply-request.json").exists())
                outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
                self.assertEqual(
                    outputs["idempotency_key"],
                    original_outputs["idempotency_key"],
                )
                self.assertEqual(outputs["recovery"], "true")
                if mode == "apply":
                    self.assertEqual(payload, original_request)

    def test_existing_printable_keys_remain_available_for_setup_and_onboarding_recovery(
        self,
    ) -> None:
        for context in ("a" * 300, "fixture context", "fixture-é"):
            with self.subTest(context=context):
                normal = self.onboarding_request(None, context=context)
                self.assertEqual(normal.returncode, 0, normal.stderr)
                outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
                key = outputs["idempotency_key"]
                recovered = self.onboarding_request({"mode": "readback"}, context=context)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
                self.assertEqual(outputs["idempotency_key"], key)
                self.assertFalse((self.root / "authz-apply-request.json").exists())
                setup = self.setup_request({"idempotency_key": key}, mode="readback")
                self.assertEqual(setup.returncode, 0, setup.stderr)
                outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
                self.assertEqual(outputs["idempotency_key"], key)

    def test_normal_onboarding_keeps_records_digest_and_target_request(self) -> None:
        result = self.onboarding_request(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((self.root / "target-apply-request.json").read_text())
        self.assertNotIn("retain_allocation", payload)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        self.assertEqual(outputs["recovery"], "false")

    def test_onboarding_retention_requires_reviewed_custody_and_only_targets_allocation(
        self,
    ) -> None:
        result = self.onboarding_request(
            {
                "mode": "apply",
                "retain_allocation": True,
                "expected_allocation_custody_sha256": "b" * 64,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((self.root / "target-apply-request.json").read_text())
        self.assertTrue(payload["retain_allocation"])
        self.assertFalse((self.root / "authz-apply-request.json").exists())

    def test_onboarding_can_use_a_fresh_explicit_key_after_custody_resolution(self) -> None:
        normal = self.onboarding_request(None)
        self.assertEqual(normal.returncode, 0, normal.stderr)
        original = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        for control, recovery in (
            ({"idempotency_key": "fresh-reviewed-key"}, "false"),
            ({"mode": "readback", "idempotency_key": "fresh-reviewed-key"}, "true"),
        ):
            result = self.onboarding_request(control)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
            self.assertEqual(outputs["idempotency_key"], control["idempotency_key"])
            self.assertNotEqual(outputs["idempotency_key"], original["idempotency_key"])
            self.assertEqual(outputs["recovery"], recovery)

    def test_onboarding_refuses_control_that_changes_scope_or_has_invalid_key(self) -> None:
        for control in (
            {"mode": "readback", "context": "other"},
            {"mode": "readback", "idempotency_key": "key\ninjected=value"},
            {"mode": "readback", "idempotency_key": "key\n"},
            {"mode": "readback", "retain_allocation": True},
        ):
            with self.subTest(control=control):
                result = self.onboarding_request(control)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "target-apply-request.json").exists())
