from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app, idempotency_scope
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    configured_local_operator_identity,
)
from control_plane.storage.postgres import PostgresRecordStore
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
        stamp = datetime.now(timezone.utc).isoformat()
        self.preview = PreviewRecord(
            preview_id="fixture-preview",
            context=self.profile.preview.context,
            anchor_repo=self.profile.repository,
            anchor_pr_number=42,
            anchor_pr_url=f"https://github.com/{self.profile.repository}/pull/42",
            preview_label="preview",
            canonical_url="https://pr-42.example.test",
            state="active",
            created_at=stamp,
            updated_at=stamp,
            eligible_at=stamp,
        )
        self.store.write_preview_record(self.preview)
        self.payload: dict[str, Any] = _refresh_payload()
        self.issue_preview()

    def make_app(self, config: BearerIdentityConfig | None = None) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=config or self.config,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    def issue_preview(self) -> None:
        identity = configured_local_operator_identity(self.config)
        assert identity is not None
        issued = _issued_plan(OdooPreviewApplyEnvelope.model_validate(self.payload))
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id="fixture-issued",
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply-inputs",
                idempotency_key=_PLAN_ID,
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

    async def classify(self, **changes: Any) -> Any:
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
            "classification",
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
        return await self.post("/v1/drivers/odoo/preview-apply", self.payload, _PLAN_ID)

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

    async def test_stable_lane_cannot_be_used_as_a_preview(self) -> None:
        from copy import deepcopy

        for instance in ("testing", "prod", "production", "live"):
            with (
                self.subTest(instance=instance),
                patch("control_plane.http_app.execute_odoo_preview_apply_result") as execute,
            ):
                self.payload = deepcopy(_refresh_payload())
                self.payload["apply"]["dry_run_plan"]["preview_slug"] = instance
                self.issue_preview()
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
            self.safety()
            response = await self.preview_apply()
            self.assertEqual(response.status_code, 409, response.text)
            execute.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), live)
