"""Retained-generation override cleanup through reviewed service records."""

import unittest

from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooWebsiteBootstrapPayload,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.testing_override_sanitization import (
    ROUTE,
    TestingOverrideSanitizationRequest,
    TestingOverrideSanitizationPlan,
)
from control_plane.storage.postgres import DbOnlyMutationRequest
from tests.support.http import request
from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario


class TestingOverrideSanitizationScenario(FreshTestingBootstrapScenario):
    database_url = ""

    def setUp(self) -> None:
        self.seed(self.database_url, initial_generation=True)
        self.patches()
        from control_plane.integration_readback import INTEGRATION_FAMILIES

        self.integration_key = next(
            key for family in INTEGRATION_FAMILIES for key in family.config_parameters
        )
        self.original_overrides = OdooInstanceOverrideRecord(
            context=self.fixture.context,
            instance="testing",
            apply_on=("restore", "deploy"),
            updated_at=self.profile.updated_at,
            config_parameters=(
                OdooConfigParameterOverride(
                    key="fixture.toggle", value=OdooOverrideValue(source="literal", value=False)
                ),
                OdooConfigParameterOverride(
                    key=self.integration_key,
                    value=OdooOverrideValue(source="literal", value="fixture connection"),
                ),
                OdooConfigParameterOverride(
                    key="google_analytics.fixture",
                    value=OdooOverrideValue(source="literal", value="fixture analytics"),
                ),
                OdooConfigParameterOverride(
                    key="private.fixture.token",
                    value=OdooOverrideValue(
                        source="secret_binding", secret_binding_id="old-config-fixture-binding"
                    ),
                ),
            ),
            website_bootstrap=OdooWebsiteBootstrapPayload(
                name="Fixture website",
                canonical_url="https://old.example.invalid",
                homepage_url="/",
            ),
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="action",
                    value=OdooOverrideValue(source="literal", value="apply"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(
                        source="secret_binding", secret_binding_id="old-fixture-binding"
                    ),
                ),
            ),
        )
        self.store.write_odoo_instance_override_record(self.original_overrides)
        from control_plane.odoo_testing_restore import FreshOdooTestingGeneration
        from control_plane.odoo_instance_overrides import addon_setting_secret_env_key

        pointer = addon_setting_secret_env_key(addon_name="shopify", setting_name="api_token")
        self.generation = FreshOdooTestingGeneration(
            product=self.profile.product,
            context=self.fixture.context,
            operation_id=self.request.operation_id,
            target_id=self.resources.target_id,
            namespace=self.resources.namespace,
            domain_host=self.request.domain_host,
            resources_binding_sha256=self.resources.binding_sha256,
            bootstrap_plan_sha256="a" * 64,
            request_sha256="b" * 64,
            prepared_at=self.profile.updated_at,
            omitted_runtime_keys=(pointer,),
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(
                            update={
                                "base_url": "https://" + self.generation.domain_host,
                                "health_url": "https://"
                                + self.generation.domain_host
                                + "/launchplane/health",
                            }
                        )
                        if lane.instance == "testing"
                        else lane
                        for lane in self.profile.lanes
                    )
                }
            )
        )
        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "domains": (self.generation.domain_host,),
                    "env": {
                        **target.env,
                        "ODOO_PROJECT_NAME": self.generation.namespace,
                        "ODOO_STACK_NAME": self.generation.namespace,
                    },
                }
            )
        )
        for record in self.store.list_runtime_environment_records(
            context_name=self.fixture.context
        ):
            if record.scope == "instance" and record.instance == "testing":
                self.store.write_runtime_environment_record(
                    record.model_copy(
                        update={
                            "schema_version": 2,
                            "retired_provider_keys": (pointer,),
                        }
                    )
                )
        self.store.write_fresh_odoo_testing_generation(self.generation)
        self.store.request_fresh_testing_reconcile(self.generation)
        self.sanitize_payload = {
            "context": self.fixture.context,
            "operation_id": self.request.operation_id,
            "reason": "Sanitize the retained testing restore intent",
        }


class TestingOverrideSanitizationHttpTests(
    TestingOverrideSanitizationScenario, unittest.IsolatedAsyncioTestCase
):
    async def test_dry_run_reviews_existing_generation_without_rewriting_it(self) -> None:
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        response = await request(
            self.fixture.app,
            "POST",
            ROUTE.format(product=self.profile.product),
            headers={"Authorization": "Bearer local-operator-token"},
            payload=self.sanitize_payload,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(response.json()["result"]["applied"])
        for item in self.original_overrides.addon_settings:
            if item.value.secret_binding_id:
                self.assertNotIn(item.value.secret_binding_id, response.text)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.original_overrides,
        )
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            ),
            generation,
        )

    async def test_scoped_identity_production_and_foreign_generation_refuse(self) -> None:
        from tests.support.auth import scoped_agent_transport

        path = ROUTE.format(product=self.profile.product)
        with scoped_agent_transport():
            denied = await request(
                self.fixture.app,
                "POST",
                path,
                headers={"Authorization": "Bearer local-operator-token"},
                payload=self.sanitize_payload,
            )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
        for changes, status, code in (
            ({"instance": "prod"}, 400, "invalid_request"),
            ({"context": "other"}, 409, "testing_override_sanitization_unverified"),
            ({"operation_id": "other-generation"}, 409, "testing_override_sanitization_unverified"),
            ({"mode": "readback"}, 400, "review_required"),
        ):
            with self.subTest(changes=changes):
                response = await request(
                    self.fixture.app,
                    "POST",
                    path,
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload={**self.sanitize_payload, **changes},
                )
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.original_overrides,
        )


class TestingOverrideSanitizationTests(TestingOverrideSanitizationScenario):
    def reviewed(
        self,
    ) -> tuple[TestingOverrideSanitizationRequest, TestingOverrideSanitizationPlan]:
        from control_plane.testing_override_sanitization import (
            TestingOverrideSanitizationRequest,
            sanitization_snapshot,
        )

        body = TestingOverrideSanitizationRequest.model_validate(self.sanitize_payload)
        plan = sanitization_snapshot(self.store, self.profile.product, body).plan
        return body.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        ), plan

    def mutation(
        self, body: TestingOverrideSanitizationRequest, plan: TestingOverrideSanitizationPlan
    ) -> DbOnlyMutationRequest:
        from control_plane.contracts.canonical_json import canonical_json_sha256
        from control_plane.storage.postgres import DbOnlyMutationRequest

        return DbOnlyMutationRequest(
            scope="fixture-director",
            route_path=ROUTE,
            idempotency_key="fixture-sanitize-key",
            request_fingerprint=canonical_json_sha256(body.model_dump(mode="json")),
            lease_owner="fixture",
            response_status_code=202,
            response_trace_id="fixture-trace",
            response_payload={
                "result": plan.model_copy(update={"applied": True}).model_dump(mode="json")
            },
        )

    def test_apply_retains_preimage_and_preserves_copy_resources_and_production(self) -> None:
        from control_plane.testing_override_sanitization import (
            read_sanitization_audit,
            sanitization_readback,
        )
        from control_plane.odoo_instance_overrides import (
            build_post_deploy_environment,
            ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
        )
        from control_plane.odoo_testing_restore import preflight_fresh_testing_intent

        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        profiles = self.store.list_product_profile_records()
        runtimes = self.store.list_runtime_environment_records()
        targets = self.store.list_dokploy_target_records()
        allocation = self.store.read_disposable_resource_preparation(self.request.operation_id)
        body, plan = self.reviewed()
        outcome, _ = self.store.sanitize_testing_overrides(
            product=self.profile.product,
            request=body,
            mutation=self.mutation(body, plan),
        )
        self.assertEqual(outcome, "written")
        audit = read_sanitization_audit(self.store, self.profile.product, body)
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit.before, self.original_overrides)
        self.assertFalse(audit.after.addon_settings)
        from control_plane.odoo_testing_restore import fresh_testing_override_record

        assert generation is not None
        effective = fresh_testing_override_record(audit.after, generation)
        assert effective is not None
        rendered = build_post_deploy_environment(effective, workflow_intent="restore")
        kept = {item.key: item.value for item in audit.after.config_parameters}
        self.assertIs(kept["fixture.toggle"].value, False)
        self.assertNotIn(self.integration_key, kept)
        self.assertNotIn("google_analytics.fixture", kept)
        self.assertNotIn("private.fixture.token", kept)
        assert audit.after.website_bootstrap is not None
        assert self.original_overrides.website_bootstrap is not None
        self.assertEqual(
            audit.after.website_bootstrap.name, self.original_overrides.website_bootstrap.name
        )
        self.assertEqual(
            audit.after.website_bootstrap.canonical_url, "https://" + generation.domain_host
        )
        self.assertFalse(rendered.required_container_environment_keys)
        preflight_fresh_testing_intent(
            modules="website",
            encoded_payload=rendered.inline_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY],
        )
        from control_plane.live_target_runtime import validate_provider_key_retirement

        validate_provider_key_retirement(
            retired_keys=set(self.generation.omitted_runtime_keys),
            application_keys=set(),
            record_store=self.store,
            product_name=self.profile.product,
            context_name=self.fixture.context,
            instance_name="testing",
        )
        self.assertTrue(sanitization_readback(self.store, self.profile.product, body).applied)
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            ),
            generation,
        )
        self.assertEqual(self.store.list_product_profile_records(), profiles)
        self.assertEqual(self.store.list_runtime_environment_records(), runtimes)
        self.assertEqual(self.store.list_dokploy_target_records(), targets)
        self.assertEqual(
            self.store.read_disposable_resource_preparation(self.request.operation_id), allocation
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )

    def test_original_key_replays_and_changed_request_or_new_key_cannot_replace_audit(self) -> None:
        from dataclasses import replace
        from control_plane.testing_override_sanitization import read_sanitization_audit

        body, plan = self.reviewed()
        mutation = self.mutation(body, plan)
        first = self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=mutation
        )
        replay = self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=mutation
        )
        self.assertEqual(replay[0], "replayed")
        self.assertEqual(first[1].response_payload, replay[1].response_payload)
        self.assertEqual(
            self.store.sanitize_testing_overrides(
                product=self.profile.product,
                request=body,
                mutation=replace(mutation, request_fingerprint="changed"),
            )[0],
            "idempotency_conflict",
        )
        original = read_sanitization_audit(self.store, self.profile.product, body)
        with self.assertRaises(ValueError):
            self.store.sanitize_testing_overrides(
                product=self.profile.product,
                request=body,
                mutation=replace(mutation, idempotency_key="different-key"),
            )
        self.assertEqual(read_sanitization_audit(self.store, self.profile.product, body), original)

    def test_failed_commit_retains_original_and_leaves_no_audit_or_success(self) -> None:
        from unittest.mock import patch
        from control_plane.testing_override_sanitization import read_sanitization_audit

        body, plan = self.reviewed()
        mutation = self.mutation(body, plan)
        with patch.object(
            self.store,
            "_after_product_authority_bundle_step",
            side_effect=RuntimeError("fixture interrupted commit"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.sanitize_testing_overrides(
                    product=self.profile.product, request=body, mutation=mutation
                )
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.original_overrides,
        )
        self.assertIsNone(read_sanitization_audit(self.store, self.profile.product, body))
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
        )

    def test_override_drift_or_lost_executor_exclusion_refuses_before_write(self) -> None:
        from control_plane.testing_override_sanitization import read_sanitization_audit

        body, plan = self.reviewed()
        with self.store.testing_replacement_execution_lock(body.operation_id) as held:
            self.assertIsNotNone(held)
            with self.assertRaises(ValueError):
                self.store.sanitize_testing_overrides(
                    product=self.profile.product, request=body, mutation=self.mutation(body, plan)
                )
        self.store.write_odoo_instance_override_record(
            self.original_overrides.model_copy(
                update={
                    "config_parameters": (
                        *self.original_overrides.config_parameters,
                        OdooConfigParameterOverride(
                            key="fixture.changed",
                            value=OdooOverrideValue(source="literal", value=True),
                        ),
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            self.store.sanitize_testing_overrides(
                product=self.profile.product, request=body, mutation=self.mutation(body, plan)
            )
        self.assertIsNone(read_sanitization_audit(self.store, self.profile.product, body))

    def assert_planning_refused(self) -> None:
        from control_plane.testing_override_sanitization import sanitization_snapshot

        with self.assertRaises(ValueError):
            sanitization_snapshot(
                self.store,
                self.profile.product,
                TestingOverrideSanitizationRequest.model_validate(self.sanitize_payload),
            )

    def test_used_generation_refuses_planning(self) -> None:
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None
        self.store.write_fresh_odoo_testing_generation(
            generation.model_copy(update={"non_use_invalidated": True})
        )
        self.assert_planning_refused()

    def test_completed_generation_refuses_planning(self) -> None:
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None
        self.store.write_fresh_odoo_testing_generation(
            generation.model_copy(update={"completed_at": "2026-10-19T00:00:00Z"})
        )
        self.assert_planning_refused()

    def test_wrong_target_refuses_planning(self) -> None:
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": "other-target"})
        )
        self.assert_planning_refused()

    def test_staff_hold_refuses_planning(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="fixture staff hold"
                            ),
                        }
                    )
                }
            )
        )
        self.assert_planning_refused()

    def test_an_active_lane_operation_refuses_before_record_changes(self) -> None:
        from control_plane.testing_override_sanitization import read_sanitization_audit

        body, plan = self.reviewed()
        self.store.write_odoo_stable_target_replacement_operation_record(
            self.legacy_operation.model_copy(
                update={
                    "operation_id": "other-active-operation",
                    "status": "pending",
                    "phase": "created",
                    "finished_at": "",
                }
            )
        )
        with self.assertRaises(ValueError):
            self.store.sanitize_testing_overrides(
                product=self.profile.product, request=body, mutation=self.mutation(body, plan)
            )
        self.assertIsNone(read_sanitization_audit(self.store, self.profile.product, body))
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.original_overrides,
        )

    def test_unverifiable_credential_separation_refuses_planning(self) -> None:
        from unittest.mock import patch
        from control_plane.odoo_testing_restore import TestingCredentialsOverlap
        from control_plane.testing_override_sanitization import sanitization_snapshot

        with patch(
            "control_plane.odoo_testing_restore.require_distinct_testing_credentials",
            side_effect=TestingCredentialsOverlap("fixture overlap"),
        ):
            with self.assertRaises(TestingCredentialsOverlap):
                sanitization_snapshot(
                    self.store,
                    self.profile.product,
                    TestingOverrideSanitizationRequest.model_validate(self.sanitize_payload),
                )
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.original_overrides,
        )

    def test_readback_tolerates_normal_post_deploy_observations(self) -> None:
        from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyResult
        from control_plane.testing_override_sanitization import sanitization_readback

        body, plan = self.reviewed()
        self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=self.mutation(body, plan)
        )
        self.store.update_odoo_instance_override_apply_result(
            context_name=self.fixture.context,
            instance_name="testing",
            last_apply=OdooOverrideApplyResult(
                attempted=True, status="pass", applied_at="2026-10-19T06:43:00Z"
            ),
            updated_at="2026-10-19T06:43:00Z",
            source_label="odoo-post-deploy-driver",
        )
        self.assertTrue(sanitization_readback(self.store, self.profile.product, body).applied)

    def test_readback_still_protects_website_verification_intent(self) -> None:
        from control_plane.testing_override_sanitization import sanitization_readback

        body, plan = self.reviewed()
        self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=self.mutation(body, plan)
        )
        record = self.store.read_odoo_instance_override_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_odoo_instance_override_record(
            record.model_copy(
                update={
                    "last_apply": record.last_apply.model_copy(
                        update={"website_settings_verified": False}
                    ),
                }
            )
        )
        with self.assertRaises(ValueError):
            sanitization_readback(self.store, self.profile.product, body)

    def test_readback_distinguishes_boolean_and_integer_setting_values(self) -> None:
        from control_plane.testing_override_sanitization import sanitization_readback

        body, plan = self.reviewed()
        self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=self.mutation(body, plan)
        )
        record = self.store.read_odoo_instance_override_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_odoo_instance_override_record(
            record.model_copy(
                update={
                    "config_parameters": tuple(
                        item.model_copy(
                            update={"value": OdooOverrideValue(source="literal", value=0)}
                        )
                        if item.key == "fixture.toggle"
                        else item
                        for item in record.config_parameters
                    ),
                }
            )
        )
        with self.assertRaises(ValueError):
            sanitization_readback(self.store, self.profile.product, body)

    def test_completed_key_replays_while_executor_lock_is_held(self) -> None:
        body, plan = self.reviewed()
        mutation = self.mutation(body, plan)
        self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=mutation
        )
        with self.store.testing_replacement_execution_lock(body.operation_id) as held:
            self.assertIsNotNone(held)
            result = self.store.sanitize_testing_overrides(
                product=self.profile.product, request=body, mutation=mutation
            )
            self.assertEqual(result[0], "replayed")

    def test_readback_detects_subsequent_intent_drift(self) -> None:
        from control_plane.testing_override_sanitization import sanitization_readback

        body, plan = self.reviewed()
        self.store.sanitize_testing_overrides(
            product=self.profile.product, request=body, mutation=self.mutation(body, plan)
        )
        self.store.write_odoo_instance_override_record(self.original_overrides)
        with self.assertRaises(ValueError):
            sanitization_readback(self.store, self.profile.product, body)
