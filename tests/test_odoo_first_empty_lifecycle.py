from collections.abc import Callable
from pathlib import Path

from typing import Any
from unittest.mock import patch

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthMonitoringPolicy,
    ProductOdooPrelaunchRebuildPolicy,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementRequest,
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.product_prelaunch_rebuild_policy import (
    ProductPrelaunchRebuildPolicyApplyRequest,
    build_product_prelaunch_rebuild_policy_plan,
    updated_product_prelaunch_rebuild_policy_profile,
)
from control_plane.workflows import odoo_stable_target_replacement as replacement
from control_plane import runtime_environments
from control_plane.dokploy import api, compose
from control_plane.runtime_environments import resolve_site_runtime_environment_from_store
from control_plane.product_config import apply_product_config_bundle
from control_plane.contracts.odoo_target_replacement_failures import (
    OdooProviderEffectUncertainError,
)
from tests import test_odoo_first_empty_inputs as inputs
from tests.test_odoo_stable_target_replacement import _artifact_manifest


class FirstEmptyLifecycleTests(inputs.FirstEmptyInputsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.runtime = self.runtime.model_copy(
            update={"env": {**self.runtime.env, "ODOO_DB_USER": "example"}}
        )
        self.store.write_runtime_environment_record(self.runtime)
        apply_product_config_bundle(
            record_store=self.store,
            payload={
                "product": self.profile.product,
                "context": self.context,
                "instance": "prod",
                "secrets": [
                    {
                        "binding_key": "ODOO_DB_PASSWORD",
                        "value": "fictional-database-password",
                        "secret_class": "prod_only",
                    }
                ],
            },
            mode="apply",
            actor="fixture",
            source_label="fixture",
        )
        self.policy = ProductOdooPrelaunchRebuildPolicy(
            enabled=True,
            confirmation="prepare original",
            approval_issue_url="https://github.com/example/product/issues/1",
            expected_target_name=self.target.target_name,
            expected_domains=("example.test",),
        )
        self.profile = self.profile.model_copy(
            update={
                "production_use": "prelaunch",
                "production_use_authority": "explicit",
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_prelaunch_rebuild": self.policy,
                            "health_monitoring": ProductLaneHealthMonitoringPolicy(),
                        }
                    )
                    if lane.instance == "prod"
                    else lane
                    for lane in self.profile.lanes
                ),
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.target = self.target.model_copy(update={"domains": ("example.test",)})
        self.store.write_dokploy_target_record(self.target)
        self.provider_payload["name"] = self.target.target_name
        self.manifest = _artifact_manifest(image_repository=self.profile.image.repository)
        self.store.write_artifact_manifest(self.manifest)
        self.request = OdooStableTargetReplacementRequest(
            product=self.profile.product,
            instance="prod",
            data_source_mode="empty",
            allow_empty_data=True,
            confirmation=self.policy.confirmation,
            artifact_id=self.manifest.artifact_id,
            source_git_ref=self.manifest.source_commit,
        )

    @staticmethod
    def provider_request(*, path: str, **_: Any) -> Any:
        if path == "/api/domain.byComposeId":
            return [{"host": "example.test", "domainId": "domain-original"}]
        return []

    def plan(self) -> replacement.OdooStableTargetReplacementPlan:
        return replacement.build_odoo_stable_target_replacement_plan(
            control_plane_root=Path("."),
            record_store=self.store,
            request=self.request,
            dokploy_request=self.provider_request,
        )

    def test_unknown_policy_can_record_intent_without_recording_empty_data(self) -> None:
        request = ProductPrelaunchRebuildPolicyApplyRequest(
            product=self.profile.product,
            context=self.context,
            instance="prod",
            reason="Prepare original",
            **self.policy.model_dump(),
        )
        before = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={"odoo_prelaunch_rebuild": ProductOdooPrelaunchRebuildPolicy()}
                    )
                    if lane.instance == "prod"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        plan = build_product_prelaunch_rebuild_policy_plan(profile=before, request=request)
        self.assertTrue(plan.changed)
        after = updated_product_prelaunch_rebuild_policy_profile(
            profile=before,
            request=request,
            updated_at=before.updated_at,
        )
        self.assertEqual(
            tuple(lane.odoo_data_policy for lane in after.lanes),
            tuple(lane.odoo_data_policy for lane in before.lanes),
        )
        self.assertTrue(after.odoo_replacement_proof_required(after.lanes[-1]))

    def test_current_empty_plan_does_not_change_profile_or_create_a_durable_exception(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.plan_status, "ready", plan.blockers)
        self.assertFalse(plan.destructive_backup_required)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.probe.side_effect = lambda **kwargs: kwargs["receive"](
            {"type": "complete", "status": "unknown"}
        )
        later = self.plan()
        self.assertEqual(later.plan_status, "blocked")
        self.assertTrue(later.destructive_backup_required)

    def apply_until_deployment(self, checkpoint: Callable[[str], None]) -> None:
        def write_source(**kwargs: Any) -> dict[str, str]:
            self.provider_payload["composeFile"] = kwargs["compose_file"]
            self.provider_payload["sourceType"] = "raw"
            return {}

        def write_environment(**kwargs: Any) -> None:
            self.provider_payload["env"] = kwargs["env_text"]

        with (
            patch.object(
                runtime_environments,
                "resolve_site_runtime_environment",
                side_effect=lambda **_: resolve_site_runtime_environment_from_store(
                    record_store=self.store,
                    context_name=self.context,
                    instance_name="prod",
                    target_environment={},
                ),
            ),
            patch.object(
                compose,
                "sync_dokploy_compose_raw_source",
                side_effect=write_source,
            ),
            patch.object(compose, "ensure_compose_web_domain_route"),
            patch.object(
                compose,
                "fetch_dokploy_converted_compose_file",
                side_effect=lambda **_: str(self.provider_payload["composeFile"]),
            ),
            patch.object(api, "update_dokploy_target_env", side_effect=write_environment),
            patch.object(api, "trigger_deployment", side_effect=ReachedDeployment) as deploy,
            patch.object(
                replacement,
                "execute_odoo_prod_backup_gate",
                side_effect=AssertionError("unexpected capture"),
            ),
            patch.object(
                replacement,
                "execute_odoo_post_deploy",
                side_effect=AssertionError("unexpected post deploy"),
            ),
        ):
            self.deploy = deploy
            result = replacement.execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=self.store,
                request=OdooStableTargetReplacementApplyRequest(**self.request.model_dump()),
                dokploy_request=self.provider_request,
                provider_effect_checkpoint=checkpoint,
                operation_id="original-operation",
                operation_lease_owner="original-worker",
                hold_uncertain_effects=True,
            )
            self.last_result = result
            self.assertEqual(result.deploy_status, "fail")

    def test_executor_requalifies_original_and_exact_staged_inputs_before_dispatch(self) -> None:
        checkpoints: list[str] = []
        with self.assertRaises(ReachedDeployment):
            self.apply_until_deployment(checkpoints.append)
            self.fail(self.last_result.error_message)
        self.assertEqual(len(checkpoints), 2)
        self.deploy.assert_called_once()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertEqual(self.store.list_runtime_environment_records(), (self.runtime,))
        later = self.plan()
        self.assertEqual(later.plan_status, "blocked")
        self.assertTrue(later.destructive_backup_required)

    def test_data_arriving_before_first_write_refuses_without_dispatch(self) -> None:
        def data_arrives(_: str) -> None:
            self.probe.side_effect = lambda **kwargs: kwargs["receive"](
                {"type": "complete", "status": "unknown"}
            )

        self.apply_until_deployment(data_arrives)
        self.deploy.assert_not_called()
        self.assertEqual(self.provider_payload["env"], "")
        self.assertEqual(self.provider_payload["composeFile"], "")

    def test_data_arriving_after_staging_holds_original_effect_without_dispatch(self) -> None:
        calls = 0

        def data_arrives(_: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.probe.side_effect = lambda **kwargs: kwargs["receive"](
                    {"type": "complete", "status": "unknown"}
                )

        with self.assertRaises(OdooProviderEffectUncertainError):
            self.apply_until_deployment(data_arrives)
        self.deploy.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    def test_live_authoritative_unknown_bootstrap_and_disabled_policy_stay_blocked(self) -> None:
        prod = self.profile.lanes[-1]
        for changed in (
            self.profile.model_copy(update={"production_use": "live"}),
            self.profile.model_copy(update={"production_use": "unknown"}),
            self.profile.model_copy(update={"production_use_authority": "director_agent"}),
            self.profile.model_copy(
                update={
                    "lanes": (
                        *self.profile.lanes[:-1],
                        prod.model_copy(
                            update={
                                "odoo_data_policy": prod.odoo_data_policy.model_copy(
                                    update={"data_authority": "authoritative"}
                                )
                            }
                        ),
                    )
                }
            ),
            self.profile.model_copy(
                update={
                    "lanes": (
                        *self.profile.lanes[:-1],
                        prod.model_copy(
                            update={"odoo_prelaunch_rebuild": ProductOdooPrelaunchRebuildPolicy()}
                        ),
                    )
                }
            ),
        ):
            with self.subTest(profile=changed.production_use):
                self.store.write_product_profile_record(changed)
                self.assertEqual(self.plan().plan_status, "blocked")
        self.store.write_product_profile_record(self.profile)
        self.store.write_dokploy_target_record(
            self.target.model_copy(
                update={
                    "policies": self.target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="Fictional retained use"
                            )
                        }
                    )
                }
            )
        )
        plan = self.plan()
        self.assertEqual(plan.plan_status, "blocked")
        self.assertTrue(plan.destructive_backup_required)

    def refuses_changed_authority(self, mutate: Callable[[], None]) -> None:
        self.apply_until_deployment(lambda _: mutate())
        self.deploy.assert_not_called()
        self.assertEqual(self.provider_payload["composeFile"], "")

    def test_whole_profile_drift_before_effect_refuses(self) -> None:
        self.refuses_changed_authority(
            lambda: self.store.write_product_profile_record(
                self.profile.model_copy(update={"display_name": "Changed unrelated field"})
            )
        )

    def test_original_target_drift_before_effect_refuses(self) -> None:
        self.refuses_changed_authority(
            lambda: self.store.write_dokploy_target_id_record(
                self.target_id.model_copy(update={"target_id": "foreign-compose"})
            )
        )

    def test_runtime_drift_before_effect_refuses(self) -> None:
        self.refuses_changed_authority(
            lambda: self.store.write_runtime_environment_record(
                self.runtime.model_copy(update={"env": {**self.runtime.env, "ODOO_WORKERS": "4"}})
            )
        )

    def test_binding_version_drift_before_effect_refuses(self) -> None:
        def rotate() -> None:
            apply_product_config_bundle(
                record_store=self.store,
                payload={
                    "product": self.profile.product,
                    "context": self.context,
                    "instance": "prod",
                    "secrets": [
                        {
                            "binding_key": "ODOO_DB_PASSWORD",
                            "value": "fictional-new-password",
                            "secret_class": "prod_only",
                        }
                    ],
                },
                mode="apply",
                actor="fixture",
                source_label="fixture",
            )

        self.refuses_changed_authority(rotate)

    def test_own_pending_record_never_excuses_foreign_deployment_history(self) -> None:
        def foreign_deployment() -> None:
            pending = self.store.list_deployment_records(
                context_name=self.context, instance_name="prod"
            )[0]
            self.store.write_deployment_record(
                pending.model_copy(update={"record_id": "foreign-deployment"})
            )

        self.refuses_changed_authority(foreign_deployment)

    def test_staged_provider_inputs_must_equal_the_executors_exact_values(self) -> None:
        calls = 0

        def change_after_readback(_: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.provider_payload["env"] = (
                    str(self.provider_payload["env"]) + "\nUNREVIEWED=value"
                )

        with self.assertRaises(OdooProviderEffectUncertainError):
            self.apply_until_deployment(change_after_readback)
        self.deploy.assert_not_called()


class ReachedDeployment(BaseException):
    pass
