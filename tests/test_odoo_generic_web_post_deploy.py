from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    GenericWebPostDeployContext,
)
from control_plane.workflows.odoo_generic_web_post_deploy import (
    execute_odoo_generic_web_post_deploy,
    generic_web_post_deploy_executor_for_driver_id,
)
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult


class OdooGenericWebPostDeployTests(unittest.TestCase):
    def test_selects_odoo_driver_executor(self) -> None:
        self.assertIs(
            generic_web_post_deploy_executor_for_driver_id(" odoo "),
            execute_odoo_generic_web_post_deploy,
        )
        self.assertIsNone(generic_web_post_deploy_executor_for_driver_id("generic-web"))

    def test_returns_terminal_evidence(self) -> None:
        context = _context()

        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            with patch(
                "control_plane.workflows.odoo_generic_web_post_deploy.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="prod",
                    phase="deploy",
                    post_deploy_status="pass",
                    override_status="pass",
                    override_record_found=True,
                    override_payload_rendered=True,
                    override_payload_sha256="a" * 64,
                    override_payload_schema_version=1,
                    override_count=2,
                    override_evidence={
                        "payload_sha256": "a" * 64,
                        "workflow_intent": "deploy",
                        "config_parameter_count": "1",
                    },
                ),
            ) as post_deploy:
                evidence = execute_odoo_generic_web_post_deploy(
                    Path("."),
                    cast(GenericWebDeployStore, store),
                    context,
                )

        self.assertTrue(evidence.attempted)
        self.assertEqual(evidence.status, "pass")
        self.assertIn("generic-web extension hook", evidence.detail)
        self.assertEqual(evidence.evidence["driver_id"], "odoo")
        self.assertEqual(evidence.evidence["override_status"], "pass")
        self.assertEqual(evidence.evidence["override_record_found"], "true")
        self.assertEqual(evidence.evidence["override_payload_rendered"], "true")
        self.assertEqual(evidence.evidence["payload_sha256"], "a" * 64)
        self.assertEqual(evidence.evidence["workflow_intent"], "deploy")
        post_deploy.assert_called_once()
        request = post_deploy.call_args.kwargs["request"]
        self.assertEqual(request.context, "cm")
        self.assertEqual(request.instance, "prod")

    def test_preserves_failure_detail(self) -> None:
        context = _context()

        with TemporaryDirectory() as temporary_directory_name:
            store = _store(Path(temporary_directory_name))
            with patch(
                "control_plane.workflows.odoo_generic_web_post_deploy.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="prod",
                    phase="deploy",
                    post_deploy_status="fail",
                    override_status="fail",
                    error_message="override failed",
                ),
            ):
                evidence = execute_odoo_generic_web_post_deploy(
                    Path("."),
                    cast(GenericWebDeployStore, store),
                    context,
                )

        self.assertTrue(evidence.attempted)
        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.detail, "override failed")


def _context() -> GenericWebPostDeployContext:
    return GenericWebPostDeployContext(
        product="odoo-tenant-cm",
        context="cm",
        instance="prod",
        deployment_record_id="deployment-cm-prod",
        target_name="cm-prod",
        target_type="compose",
        target_id="compose-cm-prod",
        artifact_id="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
        source_git_ref="abc123",
    )


def _store(root: Path) -> FilesystemRecordStore:
    store = FilesystemRecordStore(state_dir=root / "state")
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord(
            product="odoo-tenant-cm",
            display_name="Odoo Tenant CM",
            repository="cbusillo/odoo-tenant-cm",
            driver_id="odoo",
            image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
            runtime_port=8069,
            health_path="/web/health",
            lanes=(ProductLaneProfile(instance="prod", context="cm"),),
            updated_at="2026-05-26T00:00:00Z",
            source="test",
        )
    )
    return store


if __name__ == "__main__":
    unittest.main()
