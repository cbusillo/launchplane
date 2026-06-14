from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.workflows.odoo_prod_backup_gate import OdooProdBackupGateResult
from control_plane.workflows.odoo_prod_promotion import OdooProdPromotionResult
from control_plane.workflows.odoo_prod_promotion_inputs import OdooProdPromotionInputsResult
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunStore,
    OdooProdPromotionRunRequest,
    execute_odoo_prod_promotion_run,
)


class OdooProdPromotionRunTests(unittest.TestCase):
    def test_run_request_requires_testing_to_prod(self) -> None:
        with self.assertRaises(ValidationError):
            OdooProdPromotionRunRequest(
                context="cm",
                from_instance="prod",
                to_instance="testing",
                request_id="run-123",
            )

    def test_run_executes_inputs_backup_gate_and_promotion(self) -> None:
        record_store = cast(OdooProdPromotionRunStore, cast(object, Mock()))
        inputs_result = _inputs_result()
        backup_result = _backup_result()
        promotion_result = _promotion_result()

        with (
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=inputs_result,
            ) as inputs_mock,
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                return_value=backup_result,
            ) as backup_mock,
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_promotion",
                return_value=promotion_result,
            ) as promotion_mock,
        ):
            result = execute_odoo_prod_promotion_run(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooProdPromotionRunRequest(
                    context="CM",
                    product="odoo-tenant-cm-website",
                    request_id="run-123-attempt-1",
                    backup_timeout_seconds=300,
                    promotion_timeout_seconds=600,
                    health_timeout_seconds=180,
                    no_cache=True,
                ),
            )

        self.assertEqual(result.run_status, "pass")
        self.assertEqual(result.input_status, "ready")
        self.assertEqual(result.backup_status, "pass")
        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.destination_health_status, "pass")
        self.assertEqual(result.artifact_id, "artifact-cm-new")
        self.assertEqual(result.backup_record_id, "backup-gate-cm-prod-run-123-attempt-1")
        self.assertEqual(result.promotion_record_id, "promotion-cm-testing-to-prod")
        inputs_mock.assert_called_once()
        self.assertEqual(inputs_mock.call_args.kwargs["request"].request_id, "run-123-attempt-1")
        backup_mock.assert_called_once()
        self.assertEqual(backup_mock.call_args.kwargs["request"].timeout_seconds, 300)
        promotion_mock.assert_called_once()
        promotion_request = promotion_mock.call_args.kwargs["request"]
        self.assertEqual(promotion_request.product, "odoo-tenant-cm-website")
        self.assertEqual(promotion_request.artifact_id, "artifact-cm-new")
        self.assertEqual(promotion_request.timeout_seconds, 600)
        self.assertEqual(promotion_request.health_timeout_seconds, 180)
        self.assertTrue(promotion_request.no_cache)

    def test_run_treats_skipped_health_as_success_when_verification_disabled(
        self,
    ) -> None:
        promotion_result = _promotion_result().model_copy(
            update={"destination_health_status": "skipped"}
        )

        with (
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=_inputs_result(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                return_value=_backup_result(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_promotion",
                return_value=promotion_result,
            ) as promotion_mock,
        ):
            result = execute_odoo_prod_promotion_run(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url=None,
                record_store=cast(OdooProdPromotionRunStore, cast(object, Mock())),
                request=OdooProdPromotionRunRequest(
                    context="cm",
                    request_id="run-123",
                    verify_health=False,
                ),
            )

        self.assertEqual(result.run_status, "pass")
        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.destination_health_status, "skipped")
        self.assertEqual(result.error_message, "")
        promotion_request = promotion_mock.call_args.kwargs["request"]
        self.assertFalse(promotion_request.verify_health)

    def test_run_blocks_without_ready_inputs(self) -> None:
        with patch(
            "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
            return_value=OdooProdPromotionInputsResult(
                context="cm",
                from_instance="testing",
                to_instance="prod",
                request_id="run-123",
                input_status="blocked",
                error_message="missing testing tuple",
            ),
        ) as inputs_mock:
            with patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate"
            ) as backup_mock:
                result = execute_odoo_prod_promotion_run(
                    control_plane_root=Path("/control-plane"),
                    state_dir=Path("/state"),
                    database_url=None,
                    record_store=cast(OdooProdPromotionRunStore, cast(object, Mock())),
                    request=OdooProdPromotionRunRequest(context="cm", request_id="run-123"),
                )

        self.assertEqual(result.run_status, "blocked")
        self.assertEqual(result.input_status, "blocked")
        self.assertEqual(result.backup_status, "skipped")
        self.assertIn("missing testing tuple", result.error_message)
        inputs_mock.assert_called_once()
        backup_mock.assert_not_called()

    def test_run_stops_after_failed_backup_gate(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=_inputs_result(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate",
                return_value=OdooProdBackupGateResult(
                    context="cm",
                    instance="prod",
                    backup_record_id="backup-gate-cm-prod-run-123-attempt-1",
                    backup_status="fail",
                    error_message="backup failed",
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_promotion"
            ) as promotion_mock,
        ):
            result = execute_odoo_prod_promotion_run(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url=None,
                record_store=cast(OdooProdPromotionRunStore, cast(object, Mock())),
                request=OdooProdPromotionRunRequest(context="cm", request_id="run-123"),
            )

        self.assertEqual(result.run_status, "fail")
        self.assertEqual(result.backup_status, "fail")
        self.assertEqual(result.promotion_status, "skipped")
        self.assertIn("backup failed", result.error_message)
        promotion_mock.assert_not_called()


def _inputs_result() -> OdooProdPromotionInputsResult:
    return OdooProdPromotionInputsResult(
        context="cm",
        from_instance="testing",
        to_instance="prod",
        request_id="run-123-attempt-1",
        input_status="ready",
        artifact_id="artifact-cm-new",
        source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
        backup_record_id="backup-gate-cm-prod-run-123-attempt-1",
        release_tuple_id="cm-testing-artifact-cm-new",
        image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
        image_digest="sha256:new",
    )


def _backup_result() -> OdooProdBackupGateResult:
    return OdooProdBackupGateResult(
        context="cm",
        instance="prod",
        backup_record_id="backup-gate-cm-prod-run-123-attempt-1",
        backup_status="pass",
        backup_root="/volumes/data/backups/launchplane",
    )


def _promotion_result() -> OdooProdPromotionResult:
    return OdooProdPromotionResult(
        context="cm",
        from_instance="testing",
        to_instance="prod",
        artifact_id="artifact-cm-new",
        backup_record_id="backup-gate-cm-prod-run-123-attempt-1",
        promotion_record_id="promotion-cm-testing-to-prod",
        deployment_record_id="deployment-cm-prod",
        release_tuple_id="cm-prod-artifact-cm-new",
        promotion_status="pass",
        deployment_status="pass",
        post_deploy_status="pass",
        destination_health_status="pass",
    )


if __name__ == "__main__":
    unittest.main()
