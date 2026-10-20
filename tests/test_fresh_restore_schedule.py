"""Fresh-copy transport is bounded before native delivery and schedule writes."""

import unittest
from unittest.mock import Mock, patch
from control_plane.contracts.provider_schedule import ScheduleMaterializationTooLarge
from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from tests.test_odoo_testing_restore import generation


class FreshRestoreScheduleTests(unittest.TestCase):
    def test_native_delivery_is_preflighted_and_final_schedule_is_rechecked(self) -> None:
        record = generation().model_copy(
            update={
                "source_mode": "container-export",
                "source_binding_sha256": "e" * 64,
            }
        )
        target = DokployTargetDefinition(
            context=record.context, instance="testing", target_id=record.target_id
        )
        environment = {
            "ODOO_DB_NAME": "fixture",
            "ODOO_INSTALL_MODULES": "website",
            "DOCKER_IMAGE_REFERENCE": "registry.invalid/odoo@sha256:" + "f" * 64,
        }
        oversize = "x" * api.DOKPLOY_SCHEDULE_MATERIALIZATION_LIMIT_BYTES
        for rendered, delivered in (([oversize], False), (["echo preflight", oversize], True)):
            with (
                self.subTest(delivered=delivered),
                patch.object(
                    api,
                    "fetch_dokploy_target_payload",
                    return_value={
                        "env": api.serialize_dokploy_env_text(environment),
                        "appName": record.namespace,
                        "serverId": "registered-server",
                    },
                ),
                patch.object(api, "find_matching_dokploy_schedule", return_value=None),
                patch.object(api, "upsert_dokploy_schedule") as upsert,
                patch.object(
                    post_deploy, "build_fresh_testing_restore_script", side_effect=rendered
                ),
            ):
                native = Mock(return_value={})
                with self.assertRaises(ScheduleMaterializationTooLarge):
                    post_deploy.run_compose_post_deploy_update(
                        host="https://provider.invalid",
                        token="fixture",
                        target_definition=target,
                        env_file=None,
                        run_destructive_restore=True,
                        fresh_testing_generation=record,
                        prepare_native_source=native,
                    )
                self.assertEqual(native.called, delivered)
                upsert.assert_not_called()
