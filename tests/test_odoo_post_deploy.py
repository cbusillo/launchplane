import base64
import json
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import Mock, patch

from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
    OdooWebsiteBootstrapRoute,
)
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.dokploy import api as dokploy_api, post_deploy as dokploy_post_deploy
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.odoo_instance_overrides import build_post_deploy_environment
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.odoo_generic_web_post_deploy import (
    post_deploy_evidence_from_odoo_result,
)
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    execute_odoo_post_deploy,
)


def _module_update_evidence(**extra: str) -> dict[str, str]:
    return {
        "log_available": "true",
        "odoo_module_update_completed": "true",
        "odoo_module_update_image_match": "true",
        "odoo_module_update_modules_configured": "true",
        **extra,
    }


def _capture_module_update_runs(
    captured_runs: list[dict[str, object]],
) -> Callable[..., dict[str, str]]:
    def capture(**kwargs: object) -> dict[str, str]:
        captured_runs.append(kwargs)
        return _module_update_evidence()

    return capture


class OdooPostDeployWorkflowTests(unittest.TestCase):
    def _source_of_truth(self) -> DokploySourceOfTruth:
        return DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="opw",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-123",
                    target_name="opw-testing",
                ),
            ),
        )

    def test_execute_applies_deploy_phase_overrides_through_post_deploy_runner(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="testing",
                    apply_on=("deploy",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal",
                                value="https://opw-testing.example.com",
                            ),
                        ),
                    ),
                    updated_at="2026-04-26T12:00:00Z",
                    source_label="test",
                )
            )

            def capture_post_deploy_run(**kwargs: object) -> dict[str, str]:
                captured_runs.append(kwargs)
                return _module_update_evidence(
                    odoo_instance_overrides_payload_present="true",
                    website_bootstrap_domain_matches_canonical="true",
                )

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=capture_post_deploy_run,
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.utc_now_timestamp",
                    return_value="2026-04-26T12:05:00Z",
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.override_status, "pass")
            self.assertTrue(result.override_payload_rendered)
            self.assertEqual(result.workflow_intent, "deploy")
            self.assertEqual(result.override_payload_schema_version, 1)
            # web.base.url plus the explicit Shopify clear for a non-production lane.
            self.assertEqual(result.override_count, 2)
            self.assertFalse(result.website_bootstrap_included)
            self.assertRegex(result.override_payload_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual(
                result.override_evidence["payload_sha256"],
                result.override_payload_sha256,
            )
            self.assertEqual(result.override_evidence["workflow_intent"], "deploy")
            self.assertEqual(result.override_evidence["config_parameter_count"], "1")
            self.assertEqual(
                result.override_evidence[
                    "post_deploy_readback_odoo_instance_overrides_payload_present"
                ],
                "true",
            )
            self.assertEqual(
                result.override_evidence[
                    "post_deploy_readback_website_bootstrap_domain_matches_canonical"
                ],
                "true",
            )
            self.assertNotIn("website_bootstrap_domain_matches_canonical", result.override_evidence)
            self.assertEqual(len(captured_runs), 1)
            workflow_environment = cast(
                "dict[str, str]", captured_runs[0]["workflow_environment_overrides"]
            )
            self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", workflow_environment)
            self.assertNotIn("ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL", workflow_environment)
            self.assertFalse(captured_runs[0]["run_destructive_restore"])
            updated_record = store.read_odoo_instance_override_record(
                context_name="opw",
                instance_name="testing",
            )
            self.assertEqual(updated_record.last_apply.status, "pass")
            self.assertEqual(updated_record.last_apply.applied_at, "2026-04-26T12:05:00Z")
            self.assertEqual(updated_record.source_label, "odoo-post-deploy-driver")

    def test_execute_runs_post_deploy_without_override_record(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=_capture_module_update_runs(captured_runs),
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.override_status, "skipped")
            self.assertFalse(result.override_record_found)
            self.assertEqual(len(captured_runs), 1)
            self.assertEqual(captured_runs[0]["workflow_environment_overrides"], {})

    def test_execute_fails_when_module_update_evidence_is_incomplete(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    return_value={"log_available": "true"},
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                )

            self.assertEqual(result.post_deploy_status, "fail")
            self.assertIn("did not prove", result.error_message)

    def test_provider_done_module_failure_precedes_sender_and_persists_safe_evidence(self) -> None:
        override = OdooInstanceOverrideRecord(
            context="opw",
            instance="testing",
            apply_on=("deploy",),
            website_bootstrap=OdooWebsiteBootstrapPayload(
                tenant="example",
                name="Example",
                company_email="support@example.test",
            ),
            updated_at="2026-09-26T12:00:00Z",
            source_label="test",
        )
        environment = build_post_deploy_environment(override, workflow_intent="deploy")
        target_payload = {
            "env": dokploy_api.serialize_dokploy_env_text(
                {
                    "ODOO_DB_NAME": "example",
                    **environment.inline_environment,
                }
            ),
            "appName": "example-app",
            "serverId": "server-example",
            # Runtime startup proof cannot replace the maintenance schedule's proof.
            "logs": "website_bootstrap_company_email_matches=true",
        }
        for completed in (None, "false"):
            for sender in (None, "false", "true"):
                logs = [
                    "odoo_module_update_image_match=true",
                    "odoo_module_update_modules_configured=true",
                    "odoo.tools.convert.ParseError: required res.partner.group_rfq is missing",
                    "DETAIL: Failing row contains (private-row-value)",
                    "ODOO_DB_PASSWORD=private-password",
                    "Module update exited with status 255",
                ]
                if completed is not None:
                    logs.append(f"odoo_module_update_completed={completed}")
                if sender is not None:
                    logs.append(f"website_bootstrap_company_email_matches={sender}")
                with (
                    self.subTest(completed=completed, sender=sender),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    store = FilesystemRecordStore(state_dir=root / "state")
                    store.write_odoo_instance_override_record(override)
                    with (
                        patch(
                            "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                            return_value=self._source_of_truth(),
                        ),
                        patch(
                            "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                            return_value=("https://dokploy.example.test", "private-provider-token"),
                        ),
                        patch.multiple(
                            dokploy_api,
                            fetch_dokploy_target_payload=Mock(return_value=target_payload),
                            find_matching_dokploy_schedule=Mock(return_value=None),
                            upsert_dokploy_schedule=Mock(
                                return_value={"scheduleId": "maintenance-schedule"}
                            ),
                            latest_deployment_for_schedule=Mock(
                                side_effect=[
                                    {"deploymentId": "previous-maintenance"},
                                    {
                                        "deploymentId": "failed-maintenance",
                                        "status": "done",
                                        "logs": logs,
                                    },
                                ]
                            ),
                            wait_for_dokploy_schedule_deployment=Mock(
                                return_value="deployment=failed-maintenance status=done"
                            ),
                            dokploy_request=Mock(return_value={"ok": True}),
                        ),
                    ):
                        result = execute_odoo_post_deploy(
                            control_plane_root=root,
                            record_store=store,
                            request=OdooPostDeployRequest(context="opw", instance="testing"),
                        )
                    self.assertEqual(result.post_deploy_status, "fail")
                    self.assertEqual(result.override_status, "fail")
                    self.assertIn("odoo_module_update_completed", result.error_message)
                    self.assertNotIn("company sender", result.error_message)
                    store.write_deployment_record(
                        DeploymentRecord(
                            record_id="failed-deploy",
                            context="opw",
                            instance="testing",
                            source_git_ref="a" * 40,
                            deploy=DeploymentEvidence(
                                deploy_mode="compose",
                                target_name="example-testing",
                                target_type="compose",
                                status="pass",
                            ),
                            post_deploy_update=post_deploy_evidence_from_odoo_result(result),
                        )
                    )
                    persisted = store.read_deployment_record("failed-deploy").post_deploy_update
                    self.assertEqual(persisted.status, "fail")
                    self.assertEqual(
                        persisted.evidence["post_deploy_readback_schedule_id"],
                        "maintenance-schedule",
                    )
                    self.assertEqual(
                        persisted.evidence["post_deploy_readback_schedule_deployment_id"],
                        "failed-maintenance",
                    )
                    self.assertEqual(
                        persisted.evidence["post_deploy_readback_odoo_module_update_image_match"],
                        "true",
                    )
                    self.assertEqual(
                        persisted.evidence.get("post_deploy_readback_odoo_module_update_completed"),
                        completed,
                    )
                    self.assertEqual(
                        persisted.evidence.get(
                            "post_deploy_readback_website_bootstrap_company_email_matches"
                        ),
                        sender,
                    )
                    self.assertNotIn("private-", persisted.model_dump_json())
                    self.assertNotIn("support@example.test", persisted.model_dump_json())
                    self.assertEqual(
                        store.read_odoo_instance_override_record(
                            context_name="opw", instance_name="testing"
                        ).last_apply.status,
                        "fail",
                    )

    def test_readback_failure_keeps_only_bounded_allowlisted_evidence(self) -> None:
        failure = dokploy_post_deploy.OdooPostDeployReadbackFailure(
            "Module proof missing",
            evidence={
                "schedule_id": "schedule-example",
                "schedule_deployment_id": "id\nprivate-token",
                "schedule_deployment_key": "x" * 201,
                "log_available": "true",
                "odoo_module_update_completed": "false",
                "website_bootstrap_website_id": "1" * 21,
                "website_bootstrap_company_email_matches": "private-email@example.test",
                "raw_log": "private-token",
                "ODOO_DB_PASSWORD": "private-password",
            },
        )
        self.assertEqual(
            failure.evidence,
            {
                "schedule_id": "schedule-example",
                "log_available": "true",
                "odoo_module_update_completed": "false",
            },
        )

    def test_execute_can_request_destructive_restore_for_prelaunch_rebuild(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=_capture_module_update_runs(captured_runs),
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                    run_destructive_restore=True,
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(len(captured_runs), 1)
            self.assertTrue(captured_runs[0]["run_destructive_restore"])

    def test_execute_applies_deploy_phase_overrides_during_destructive_restore(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="testing",
                    apply_on=("deploy",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal",
                                value="https://opw-testing.example.com",
                            ),
                        ),
                    ),
                    website_bootstrap=OdooWebsiteBootstrapPayload(
                        tenant="opw",
                        name="OPW Testing",
                        canonical_url="https://opw-testing.example.com",
                        routes=(
                            OdooWebsiteBootstrapRoute(
                                name="Shop",
                                url="/shop",
                                homepage=True,
                            ),
                        ),
                    ),
                    updated_at="2026-04-26T12:00:00Z",
                    source_label="test",
                )
            )

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=_capture_module_update_runs(captured_runs),
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                    run_destructive_restore=True,
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.override_status, "pass")
            self.assertTrue(result.override_payload_rendered)
            self.assertEqual(result.workflow_intent, "restore")
            # web.base.url plus the explicit Shopify clear for a non-production lane.
            self.assertEqual(result.override_count, 2)
            self.assertFalse(result.website_bootstrap_included)
            self.assertEqual(result.override_evidence["website_bootstrap_included"], "false")
            self.assertEqual(len(captured_runs), 1)
            workflow_environment = cast(
                "dict[str, str]", captured_runs[0]["workflow_environment_overrides"]
            )
            encoded_payload = workflow_environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"]
            decoded_payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))
            self.assertEqual(
                decoded_payload["config_parameters"],
                [
                    {
                        "key": "web.base.url",
                        "value": {
                            "source": "literal",
                            "value": "https://opw-testing.example.com",
                        },
                    }
                ],
            )
            self.assertNotIn("website_bootstrap", decoded_payload)
            self.assertTrue(captured_runs[0]["run_destructive_restore"])

    def test_execute_applies_deploy_phase_addon_overrides_during_restore_phase(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="testing",
                    apply_on=("deploy",),
                    addon_settings=(
                        OdooAddonSettingOverride(
                            addon="openai",
                            setting="api_key",
                            value=OdooOverrideValue(
                                source="secret_binding",
                                secret_binding_id="secret-opw-testing-openai",
                            ),
                        ),
                    ),
                    updated_at="2026-04-26T12:00:00Z",
                    source_label="test",
                )
            )

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update",
                    side_effect=_capture_module_update_runs(captured_runs),
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(
                        context="opw", instance="testing", phase="restore"
                    ),
                    run_destructive_restore=True,
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.override_status, "pass")
            self.assertEqual(
                result.required_container_environment_keys,
                ("ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY",),
            )
            self.assertEqual(
                result.override_evidence["required_container_environment_keys"],
                "ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY",
            )
            workflow_environment = cast(
                "dict[str, str]", captured_runs[0]["workflow_environment_overrides"]
            )
            decoded_payload = json.loads(
                base64.b64decode(
                    workflow_environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"]
                ).decode("utf-8")
            )
            self.assertEqual(
                decoded_payload["addon_settings"],
                [
                    {
                        "addon": "shopify",
                        "setting": "action",
                        "value": {"source": "literal", "value": "clear"},
                    },
                    {
                        "addon": "openai",
                        "setting": "api_key",
                        "value": {
                            "source": "secret_binding",
                            "secret_binding_id": "secret-opw-testing-openai",
                            "environment_variable": "ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY",
                        },
                    },
                ],
            )
            self.assertEqual(
                captured_runs[0]["required_workflow_environment_keys"],
                ("ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY",),
            )


if __name__ == "__main__":
    unittest.main()
