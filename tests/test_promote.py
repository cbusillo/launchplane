import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.promotion_record import CompatibilityPromotionRequest
from control_plane.contracts.ship_request import CompatibilityShipRequest
from control_plane.workflows.promote import build_promotion_record
from control_plane.workflows.promote import build_compatibility_promotion_record


class PromoteWorkflowTests(unittest.TestCase):
    def test_build_promotion_record_returns_pending_record(self) -> None:
        record = build_promotion_record(
            record_id="promotion-20260410-182231-opw-testing-prod",
            artifact_id="artifact-20260410-f45db648",
            context_name="opw",
            from_instance_name="testing",
            to_instance_name="prod",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="",
        )

        self.assertEqual(record.artifact_identity.artifact_id, "artifact-20260410-f45db648")
        self.assertEqual(record.deploy.status, "pending")
        self.assertEqual(record.deploy.target_name, "opw-prod")
        self.assertEqual(record.from_instance, "testing")

    def test_build_compatibility_promotion_record_marks_success_after_waited_ship(self) -> None:
        request = CompatibilityPromotionRequest(
            artifact_id="compatibility-opw-abc123",
            source_git_ref="abc123",
            context="opw",
            from_instance="testing",
            to_instance="prod",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            wait=True,
            verify_health=True,
            destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            },
            source_health={
                "verified": True,
                "urls": ["https://testing.example.com/web/health"],
                "timeout_seconds": 30,
                "status": "pass",
            },
            backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
        )

        record = build_compatibility_promotion_record(
            request=request,
            record_id="promotion-1",
            deployment_id="delegated-ship",
            deployment_status="pass",
        )

        self.assertEqual(record.deploy.status, "pass")
        self.assertTrue(record.destination_health.verified)
        self.assertEqual(record.destination_health.status, "pass")
        self.assertTrue(record.post_deploy_update.attempted)
        self.assertEqual(record.post_deploy_update.status, "pass")


class PromoteCliTests(unittest.TestCase):
    def test_compatibility_execute_persists_record_and_delegates_ship(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                CompatibilityPromotionRequest(
                    artifact_id="compatibility-opw-abc123",
                    source_git_ref="abc123",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=True,
                    health_timeout_seconds=45,
                    source_health={
                        "verified": True,
                        "urls": ["https://testing.example.com/web/health"],
                        "timeout_seconds": 30,
                        "status": "pass",
                    },
                    backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            captured_commands: list[list[str]] = []

            with patch("control_plane.cli._run_command", side_effect=lambda command: captured_commands.append(command)):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(captured_commands), 1)
            self.assertIn("platform", captured_commands[0])
            self.assertIn("ship", captured_commands[0])
            self.assertIn("--skip-gate", captured_commands[0])
            promotion_files = sorted((state_dir / "promotions").glob("*.json"))
            self.assertEqual(len(promotion_files), 1)
            persisted_payload = promotion_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "pass"', persisted_payload)
            self.assertIn('"artifact_id": "compatibility-opw-abc123"', persisted_payload)

    def test_ship_compatibility_plan_validates_request(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "ship",
                    "compatibility-plan",
                    "--input-file",
                    str(input_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('"source_git_ref": "abc123"', result.output)


if __name__ == "__main__":
    unittest.main()
