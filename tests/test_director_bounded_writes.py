from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app, idempotency_scope
from control_plane.odoo_preview_apply_http import (
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
    configured_local_operator_identity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import generate_preview_id
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.http import request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload, _PLAN_ID


class DirectorBoundedWritesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        self.config = _local_operator_bearer_config()
        self.app = self.make_app()
        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.preview = PreviewRecord(
            preview_id=generate_preview_id(
                context_name=self.profile.preview.context,
                anchor_repo=self.profile.repository.partition("/")[2],
                anchor_pr_number=42,
            ),
            context=self.profile.preview.context,
            anchor_repo=self.profile.repository.partition("/")[2],
            anchor_pr_number=42,
            anchor_pr_url=f"https://github.com/{self.profile.repository}/pull/42",
            preview_label="preview",
            canonical_url="https://pr-42.example.test",
            state="active",
            created_at=stamp,
            updated_at=stamp,
            eligible_at=stamp,
        )
        from control_plane.contracts.preview_generation_record import (
            PreviewGenerationRecord,
            PreviewPullRequestSummary,
        )

        generation_stamp = (datetime.fromisoformat(stamp) - timedelta(seconds=1)).isoformat()
        generation = PreviewGenerationRecord(
            generation_id="fixture-serving",
            preview_id=self.preview.preview_id,
            sequence=1,
            state="ready",
            requested_reason="Existing preview fixture",
            requested_at=generation_stamp,
            resolved_manifest_fingerprint="fixture-manifest",
            anchor_summary=PreviewPullRequestSummary(
                repo=self.preview.anchor_repo,
                pr_number=self.preview.anchor_pr_number,
                head_sha="fixture-head",
                pr_url=self.preview.anchor_pr_url,
            ),
        )
        self.store.write_preview_generation_record(generation)
        self.preview = self.preview.model_copy(
            update={
                "serving_generation_id": generation.generation_id,
                "latest_generation_id": generation.generation_id,
            }
        )
        self.store.write_preview_record(self.preview)
        self.payload = cast(dict[str, Any], _refresh_payload())
        self.payload["apply"]["dry_run_plan"]["compose_ref"] = "fixture-preview-target"
        self.plan_id = _PLAN_ID
        self.issue_preview()

    def make_app(
        self,
        config: BearerIdentityConfig | None = None,
        policy: LaunchplaneAuthzPolicy | None = None,
    ) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy or LaunchplaneAuthzPolicy(),
            bearer_identity_config=config or self.config,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    def issue_preview(self) -> None:
        identity = configured_local_operator_identity(self.config)
        assert identity is not None
        issued = issue_odoo_preview_apply_plan(
            result=_issued_plan(OdooPreviewApplyEnvelope.model_validate(self.payload)),
            plan_id=self.plan_id,
        )
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=f"fixture-issued-{self.plan_id}",
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply-inputs",
                idempotency_key=self.plan_id,
                request_fingerprint="fixture-plan",
                response_status_code=202,
                response_trace_id="fixture-plan",
                recorded_at=datetime.now(timezone.utc).isoformat(),
                response_payload={"result": issued.model_dump(mode="json")},
            )
        )

    async def post(self, path: str, payload: dict[str, Any], key: str = "") -> Any:
        return await request(
            self.app,
            "POST",
            path,
            payload=payload,
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": key,
            },
        )

    async def classify(self, *, idempotency_key: str = "", **changes: Any) -> Any:
        payload = {
            "production_use": "prelaunch",
            "reason": "Record reviewed never-live facts.",
            **changes,
        }
        route = f"/v1/product-profiles/{self.profile.product}/production-use"
        dry = await self.post(route, payload)
        self.assertEqual(dry.status_code, 202, dry.text)
        return await self.post(
            route,
            {
                **payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
            },
            idempotency_key or f"classification-{payload['production_use']}",
        )

    def safety(self, **changes: Any) -> None:
        record_safety(
            self.store,
            self.profile.product,
            self.preview.context,
            "pr-42",
            preview=self.preview,
            **changes,
        )

    async def preview_apply(self) -> Any:
        return await self.post("/v1/drivers/odoo/preview-apply", self.payload, self.plan_id)

    async def test_reviewed_never_live_correction_is_atomic_replayed_and_bounded(self) -> None:
        route = f"/v1/product-profiles/{self.profile.product}/production-use"
        payload = {"production_use": "prelaunch", "reason": "Reviewed product facts."}
        dry = await self.post(route, payload)
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        apply = {
            **payload,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        first = await self.post(route, apply, "classify-once")
        second = await self.post(route, apply, "classify-once")
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        actual = self.store.read_product_profile_record(self.profile.product)
        self.assertEqual(actual.production_use, payload["production_use"])
        self.assertEqual(actual.production_use_authority, "director_agent")
        self.assertFalse(actual.prelaunch_bootstrap_authorized)
        self.assertEqual(actual.release_on_acceptance, self.profile.release_on_acceptance)
        self.assertEqual(actual.lanes, self.profile.lanes)
        self.assertEqual(self.store.list_deployment_records(), ())
        self.assertEqual(self.store.list_promotion_records(), ())

    async def test_live_and_production_products_cannot_be_downgraded(self) -> None:
        for updated in (
            self.profile.model_copy(update={"production_use": "live"}),
            self.profile.model_copy(update={"historical_contexts": ("old-context",)}),
            *(
                self.profile.model_copy(
                    update={"lanes": (self.profile.lanes[0].model_copy(update={"instance": lane}),)}
                )
                for lane in ("prod", "production", "live", "main")
            ),
        ):
            with self.subTest(profile=updated):
                self.store.write_product_profile_record(updated)
                result = await self.classify()
                self.assertEqual(result.status_code, 403, result.text)
                self.assertEqual(
                    self.store.read_product_profile_record(self.profile.product), updated
                )

    async def test_agent_classification_cannot_retire_later_production_ownership(self) -> None:
        from control_plane.disposable_lanes import (
            DisposableLaneSafetyUnknown,
            require_disposable_lane,
        )

        response = await self.classify()
        self.assertEqual(response.status_code, 202, response.text)
        current = self.store.read_product_profile_record(self.profile.product)
        lane = current.lanes[0]
        self.store.write_product_profile_record(
            current.model_copy(
                update={
                    "lanes": (
                        lane,
                        lane.model_copy(update={"instance": "prod"}),
                    )
                }
            )
        )
        record_safety(self.store, current.product, lane.context, lane.instance)
        with self.assertRaises(DisposableLaneSafetyUnknown):
            require_disposable_lane(
                self.store,
                product=current.product,
                context=lane.context,
                instance=lane.instance,
                retirement=True,
            )

    async def test_classification_never_changes_release_or_accepts_stale_review(self) -> None:
        for release in ("held", "promote_with_rollback_drill"):
            result = await self.classify(release_on_acceptance=release)
            self.assertEqual(result.status_code, 403, result.text)
        route = f"/v1/product-profiles/{self.profile.product}/production-use"
        dry = await self.post(route, {"production_use": "prelaunch", "reason": "Reviewed facts."})
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"display_name": "Changed"})
        )
        result = await self.post(
            route,
            {
                "mode": "apply",
                "production_use": "prelaunch",
                "reason": "Reviewed facts.",
                "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
            },
            "stale",
        )
        self.assertEqual(result.status_code, 409, result.text)

    async def test_scoped_identity_cannot_inherit_either_write(self) -> None:
        self.safety()
        with (
            scoped_agent_transport(),
            patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute,
        ):
            self.assertEqual((await self.preview_apply()).status_code, 403)
            route = f"/v1/product-profiles/{self.profile.product}/production-use"
            result = await self.post(
                route,
                {
                    "production_use": "prelaunch",
                    "reason": "Assertion.",
                    "mode": "apply",
                    "reviewed_plan_sha256": "a" * 64,
                },
                "scoped",
            )
            self.assertEqual(result.status_code, 403)
            execute.assert_not_called()

    async def test_preview_runs_durable_driver_without_a_write_grant(self) -> None:
        self.safety()
        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result",
            return_value={
                "status": "pass",
                "operation": "refresh",
                "product": self.profile.product,
                "repository": self.profile.repository,
                "preview_slug": "pr-42",
                "preview_url": self.payload["apply"]["dry_run_plan"]["preview_url"],
                "domain_host": self.payload["apply"]["dry_run_plan"]["domain_host"],
                "compose_name": "cm-odoo-preview-pr-42",
            },
        ) as execute:
            result = await self.preview_apply()
        self.assertEqual(result.status_code, 202, result.text)
        execute.assert_called_once()
        self.assertEqual(self.store.list_promotion_records(), ())

    async def test_unknown_protected_and_expired_preview_refuse_before_execution(self) -> None:
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            self.assertEqual((await self.preview_apply()).status_code, 409)
            for changes in (
                {"usage": "in_use"},
                {"data": "only_copy"},
                {"production_resources": "shared"},
                {"production_resources": "unknown"},
                {
                    "observed_at": datetime.now(timezone.utc) - timedelta(minutes=10),
                    "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
                },
            ):
                self.safety(**changes)
                result = await self.preview_apply()
                self.assertEqual(result.status_code, 409, result.text)
            execute.assert_not_called()

    async def test_recorded_stable_lane_collision_cannot_be_used_as_a_preview(self) -> None:
        collided = self.profile.model_copy(
            update={
                "lanes": (
                    *self.profile.lanes,
                    self.profile.lanes[0].model_copy(update={"instance": "pr-42"}),
                )
            }
        )
        self.store.write_product_profile_record(collided)
        self.safety()
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            result = await self.preview_apply()
        self.assertEqual(result.status_code, 409, result.text)
        execute.assert_not_called()

    async def test_usage_change_after_provider_preparation_stops_before_effect(self) -> None:
        self.safety()

        def execute(**kwargs: Any) -> Any:
            self.safety(usage="in_use")
            kwargs["provider_effect_checkpoint"]("fixture-first-effect")
            self.fail("Changed usage must prevent the provider effect")

        with patch("control_plane.http_app.execute_odoo_preview_apply_result", side_effect=execute):
            result = await self.preview_apply()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "disposable_lane_safety_unknown")

    async def test_removed_production_lane_history_still_prevents_classification(self) -> None:
        from control_plane.contracts.deployment_record import DeploymentRecord
        from control_plane.contracts.promotion_record import DeploymentEvidence

        self.store.write_deployment_record(
            DeploymentRecord(
                record_id="historical-prod",
                context=self.profile.lanes[0].context,
                instance="prod",
                source_git_ref="historical",
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="compose",
                    target_type="compose",
                    target_name="historical-prod",
                ),
            )
        )
        response = await self.classify()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_live_product_preview_stays_separate_from_its_production_lane(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
        from control_plane.contracts.dokploy_target_record import (
            DokployTargetRecord,
            DokployTargetStaffTestingHold,
        )

        live = self.profile.model_copy(
            update={
                "production_use": "live",
                "lanes": (
                    *self.profile.lanes,
                    self.profile.lanes[0].model_copy(update={"instance": "prod"}),
                ),
            }
        )
        self.store.write_product_profile_record(live)
        for instance in ("pr-42", "prod"):
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=self.preview.context,
                    instance=instance,
                    target_id="shared-target",
                    updated_at=live.updated_at,
                )
            )
        self.payload["apply"]["dry_run_plan"]["compose_ref"] = "shared-target"
        self.issue_preview()
        self.safety()
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            response = await self.preview_apply()
            self.assertEqual(response.status_code, 409, response.text)
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=self.preview.context,
                    instance="pr-42",
                    target_id="isolated-target",
                    updated_at=live.updated_at,
                )
            )
            target = DokployTargetRecord(
                context=self.preview.context, instance="pr-42", updated_at=live.updated_at
            )
            self.store.write_dokploy_target_record(
                target.model_copy(
                    update={
                        "policies": target.policies.model_copy(
                            update={
                                "staff_testing_hold": DokployTargetStaffTestingHold(
                                    reason="Staff are using this preview"
                                ),
                            }
                        ),
                    }
                )
            )
            self.payload["apply"]["dry_run_plan"]["compose_ref"] = "isolated-target"
            self.issue_preview()
            self.safety()
            response = await self.preview_apply()
            self.assertEqual(response.status_code, 409, response.text)
            execute.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), live)

    async def test_explicit_profile_authority_keeps_live_classification_and_release_settings(
        self,
    ) -> None:
        self.app = self.make_app(
            policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=(self.config.local_operator_subject,),
                        token_labels=(self.config.local_operator_token_label,),
                        products=(self.profile.product,),
                        contexts=("launchplane",),
                        actions=("product_profile.write",),
                    ),
                ),
            )
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "production_use": "prelaunch",
                    "production_use_authority": "director_agent",
                }
            )
        )
        response = await self.classify(idempotency_key="explicit-reconfirmation")
        self.assertEqual(response.status_code, 202, response.text)
        current = self.store.read_product_profile_record(self.profile.product)
        self.assertTrue(current.prelaunch_bootstrap_authorized)
        response = await self.classify(production_use="live", release_on_acceptance="held")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).production_use, "live"
        )
        response = await self.classify()
        self.assertEqual(response.status_code, 403, response.text)

    async def test_preview_replay_survives_lifecycle_change_and_expired_safety(self) -> None:
        for operation in ("refresh", "destroy"):
            with self.subTest(operation=operation):
                self.store.write_preview_record(self.preview)
                self.payload["apply"]["dry_run_plan"]["operation"] = operation
                self.plan_id = f"replay-{operation}"
                self.issue_preview()
                self.safety()
                result_payload = {
                    "status": "pass",
                    "operation": operation,
                    "product": self.profile.product,
                    "repository": self.profile.repository,
                    "preview_slug": "pr-42",
                    "preview_url": self.payload["apply"]["dry_run_plan"]["preview_url"],
                    "domain_host": self.payload["apply"]["dry_run_plan"]["domain_host"],
                    "compose_name": "cm-odoo-preview-pr-42",
                }
                with patch(
                    "control_plane.http_app.execute_odoo_preview_apply_result",
                    return_value=result_payload,
                ) as execute:
                    first = await self.preview_apply()
                    self.assertEqual(first.status_code, 202, first.text)
                    self.safety(
                        observed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                    )
                    second = await self.preview_apply()
                    self.assertEqual(second.status_code, 202, second.text)
                    execute.assert_called_once()

    async def test_plan_target_must_match_the_exact_qualified_target(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.preview.context,
                instance="pr-42",
                target_id="qualified-target",
                updated_at=self.profile.updated_at,
            )
        )
        self.payload["apply"]["dry_run_plan"]["compose_ref"] = "different-target"
        self.issue_preview()
        self.safety()
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            result = await self.preview_apply()
        self.assertEqual(result.status_code, 409, result.text)
        execute.assert_not_called()

    async def test_history_inserted_after_review_is_refused_by_atomic_write(self) -> None:
        from control_plane.contracts.deployment_record import DeploymentRecord
        from control_plane.contracts.promotion_record import DeploymentEvidence

        original = self.store.compare_and_write_product_profile_record

        def insert_then_compare(**kwargs: Any) -> Any:
            self.store.write_deployment_record(
                DeploymentRecord(
                    record_id="race-production",
                    context=self.profile.lanes[0].context,
                    instance="prod",
                    source_git_ref="history",
                    deploy=DeploymentEvidence(
                        status="pass",
                        deploy_mode="compose",
                        target_type="compose",
                        target_name="production",
                    ),
                )
            )
            return original(**kwargs)

        with patch.object(
            self.store, "compare_and_write_product_profile_record", side_effect=insert_then_compare
        ):
            result = await self.classify()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)

    async def test_uncertain_preview_can_reconcile_after_safety_expires(self) -> None:
        self.safety()

        def uncertain(**kwargs: Any) -> Any:
            kwargs["provider_effect_checkpoint"]("fixture-first-effect")
            return {
                "status": "fail",
                "provider_effect_attempted": True,
                "error_message": "No acknowledgement",
            }

        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result", side_effect=uncertain
        ) as execute:
            first = await self.preview_apply()
            self.assertEqual(first.status_code, 409, first.text)
            self.safety(
                observed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            with patch(
                "control_plane.http_app.observe_odoo_preview_apply_result",
                return_value=("unknown", None, False),
            ) as observe:
                second = await self.preview_apply()
            self.assertEqual(second.status_code, 409, second.text)
            observe.assert_called_once()
            execute.assert_called_once()

    async def test_copied_template_settings_invalidate_preview_safety(self) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord

        self.safety()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.preview.context,
                instance=self.profile.preview.template_instance,
                env={"FIXTURE_OUTBOUND_ENDPOINT": "changed-connection"},
                updated_at=self.profile.updated_at,
                source_label="fixture",
            )
        )
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            response = await self.preview_apply()
        self.assertEqual(response.status_code, 409, response.text)
        execute.assert_not_called()

    async def test_key_safety_policy_change_invalidates_preview_safety(self) -> None:
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )

        self.safety()
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="changed-preview-key-policy",
                source="fixture",
                updated_at=self.profile.updated_at,
                rules=(
                    RuntimeSecretSafetyRule(binding_key="SMTP_URL", secret_class="shared_safe"),
                ),
            )
        )
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            response = await self.preview_apply()
        self.assertEqual(response.status_code, 409, response.text)
        execute.assert_not_called()

    async def test_bootstrap_override_change_invalidates_preview_safety(self) -> None:
        from control_plane.contracts.odoo_instance_override_record import (
            OdooInstanceOverrideRecord,
            OdooWebsiteBootstrapPayload,
        )

        self.safety()
        self.store.write_odoo_instance_override_record(
            OdooInstanceOverrideRecord(
                context=self.preview.context,
                instance=self.profile.preview.template_instance,
                website_bootstrap=OdooWebsiteBootstrapPayload(
                    name="Changed preview fixture", company_email="changed@example.test"
                ),
                updated_at=self.profile.updated_at,
                source_label="fixture",
            )
        )
        with patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute:
            response = await self.preview_apply()
        self.assertEqual(response.status_code, 409, response.text)
        execute.assert_not_called()

    async def test_product_identity_preserves_production_history_across_context_changes(
        self,
    ) -> None:
        from control_plane.contracts.deployment_record import DeploymentRecord
        from control_plane.contracts.promotion_record import DeploymentEvidence
        from control_plane.contracts.runtime_identity import RuntimeIdentity

        for product, expected in ((self.profile.product, 403), ("unrelated-product", 202)):
            history = DeploymentRecord(
                record_id="old-context-production",
                context="removed-context",
                instance="prod",
                source_git_ref="history",
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="compose",
                    target_type="compose",
                    target_name="old-production",
                ),
                runtime_identity=RuntimeIdentity(
                    product=product,
                    context="removed-context",
                    instance="prod",
                    deployment_record_id="old-context-production",
                    artifact_id="history-artifact",
                    source_git_ref="history",
                ),
            )
            self.store.write_deployment_record(history)
            response = await self.classify()
            self.assertEqual(response.status_code, expected, response.text)

    async def test_protected_new_plan_cannot_supersede_another_fenced_operation(self) -> None:
        from control_plane.workflows.odoo_preview_runtime import (
            ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS,
        )

        self.safety()

        def uncertain(**kwargs: Any) -> Any:
            kwargs["provider_effect_checkpoint"]("fixture-first-effect")
            return {"status": "fail", "provider_effect_attempted": True}

        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result", side_effect=uncertain
        ):
            self.assertEqual((await self.preview_apply()).status_code, 409)
        identity = configured_local_operator_identity(self.config)
        assert identity is not None
        route = "/v1/drivers/odoo/preview-apply"
        original = self.store.read_idempotency_record(
            scope=idempotency_scope(identity), route_path=route, idempotency_key=self.plan_id
        )
        assert original is not None
        later = datetime.fromisoformat(
            original.lease_expires_at.replace("Z", "+00:00")
        ) + timedelta(seconds=ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS + 30)
        self.plan_id = "protected-successor"
        self.issue_preview()
        self.safety(data="only_copy")
        with (
            patch(
                "control_plane.odoo_preview_apply_execution._original_supersession_is_quiescent",
                return_value=True,
            ),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=later.isoformat()
            ),
        ):
            response = await self.preview_apply()
        self.assertEqual(response.status_code, 409, response.text)
        retained = self.store.read_idempotency_record(
            scope=idempotency_scope(identity),
            route_path=route,
            idempotency_key=original.idempotency_key,
        )
        assert retained is not None
        self.assertEqual(retained.state, "reconcile_required")
