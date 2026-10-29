from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    LaunchplaneProductProfileRecord,
)
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    apply_odoo_preview_lifecycle_evidence,
    build_odoo_preview_runtime_identity,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_disposable_lane_authority import _DisposableRuntimeAuthorityFixture
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload


class PreviewQualificationTests(_DisposableRuntimeAuthorityFixture):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.temporary.name)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        )
        self.store.write_product_profile_record(self.profile)
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        self.issued = _issued_plan(request)
        assert self.issued.plan_provenance is not None
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=request,
            issued_plan=self.issued,
            reservation_scope="fixture",
            idempotency_key=self.issued.plan_provenance.plan_id,
            request_fingerprint="fixture",
            trace_id="fixture",
            execute_apply=Mock(),
        )
        reserved = self.store.reserve_mutation(
            scope=operation.reservation_scope,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=operation.idempotency_key,
            request_fingerprint="fixture",
            lease_owner="fixture",
            reconciliation_key=operation.adapter.reconciliation_key(),
            provider_target_key=operation.adapter.target_key(),
            original_intent=operation.adapter.original_intent(),
        ).record
        assert reserved is not None
        self.identity = build_odoo_preview_runtime_identity(
            profile=self.profile, issued_plan=self.issued, deployment_record_id=reserved.record_id
        )
        plan = self.issued.dry_run_plan
        result: dict[str, object] = dict(
            status="pass",
            operation="refresh",
            product=self.profile.product,
            repository=self.profile.repository,
            preview_slug=self.issued.preview_slug,
            preview_url=self.issued.preview_url,
            domain_host=plan.domain_host,
            compose_id="preview-target",
            compose_name=plan.compose_name,
        )
        records = apply_odoo_preview_lifecycle_evidence(
            control_plane_root_path=self.root,
            record_store=self.store,
            profile=self.profile,
            issued_plan=self.issued,
            driver_result=result,
            runtime_identity=self.identity,
        )
        self.store.complete_mutation_reservation(
            completion=reserved.model_copy(
                update={
                    "state": "completed",
                    "response_status_code": 202,
                    "response_trace_id": "fixture-complete",
                    "response_payload": {"records": records, "result": result},
                }
            )
        )
        original = self.store.read_idempotency_record_by_id(reserved.record_id)
        assert original is not None
        self.original = original
        self.preview = self.store.read_preview_record(self.identity.preview_id)
        self.generation = self.store.read_preview_generation_record(
            self.identity.preview_generation_id
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance="prod",
                target_name="production",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="prod",
                target_id="production-target",
                updated_at=self.profile.updated_at,
            )
        )
        self.production = {
            key: "private-production-" + key for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
        }
        self.production.update(
            ODOO_DATA_VOLUME="prod-data", ODOO_DB_VOLUME="prod-db", ODOO_LOG_VOLUME="prod-logs"
        )
        self.enterContext(
            patch(
                "control_plane.odoo_preview_qualification.secrets.resolve_secret_values_for_integration_from_store",
                return_value={
                    key: self.production[key] for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
                },
            )
        )
        self.enterContext(
            patch(
                "control_plane.odoo_testing_usage.source.read_dokploy_config",
                return_value=("https://provider.invalid", "private-provider-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.odoo_testing_usage.api.fetch_dokploy_target_payload",
                side_effect=self.provider,
            )
        )
        self.remote = self.enterContext(
            patch("control_plane.odoo_testing_usage._read_remote", side_effect=self.read_frame)
        )
        self.frame: dict[str, object] = {
            "type": "complete",
            "recent_signins": False,
            "authenticated_sessions": False,
            "resources_qualified": True,
        }

    def provider(self, **kwargs: Any) -> dict[str, object]:
        if kwargs["target_id"] == "production-target":
            return {
                "name": "production",
                "env": "\n".join(f"{key}={value}" for key, value in self.production.items()),
            }
        return {
            "name": self.identity.instance,
            "appName": "preview-project",
            "serverId": "fixture-server",
        }

    def read_frame(self, **kwargs: Any) -> None:
        kwargs["receive"](self.frame)

    async def collect(self) -> Any:
        scope = dict(
            product=self.profile.product,
            context="cm",
            instance=self.issued.preview_slug,
            preview_id=self.preview.preview_id,
        )
        inspected = await self.observe(**scope)
        self.assertEqual(inspected.status_code, 202, inspected.text)
        return await self.observe(
            **scope,
            mode="collect",
            expected_binding_sha256=inspected.json()["result"]["binding_sha256"],
        )

    async def test_native_preview_collection_uses_completed_serving_target_and_preserves_data_history(
        self,
    ) -> None:
        for recent, sessions in ((False, False), (True, False), (False, True)):
            with self.subTest(recent=recent, sessions=sessions):
                self.frame.update(recent_signins=recent, authenticated_sessions=sessions)
                response = await self.collect()
                self.assertEqual(response.status_code, 202, response.text)
                result = response.json()["result"]
                self.assertEqual(result["usage"], "in_use" if recent or sessions else "unused")
                self.assertEqual(
                    (result["data"], result["production_resources"]), ("unknown", "isolated")
                )
                self.assertEqual(result["odoo_usage"]["authenticated_sessions"], sessions)
                self.assertEqual(
                    self.store.read_preview_record(self.preview.preview_id), self.preview
                )
                self.assertEqual(
                    self.store.read_preview_generation_record(self.generation.generation_id),
                    self.generation,
                )
                self.assertEqual(
                    self.store.read_idempotency_record_by_id(self.original.record_id), self.original
                )
                self.assertNotIn("private-production", response.text)
                self.assertNotIn("private-provider", response.text)
                spec = self.remote.call_args.kwargs["payload"]["preview_spec"]
                self.assertEqual(spec["runtime_identity"], self.identity.model_dump(mode="json"))
                self.assertNotIn("private-production", str(spec))

    async def test_missing_serving_generation_original_or_completion_refuses(self) -> None:
        for missing, code in (
            ("generation", "preview_serving_generation_missing"),
            ("completion", "preview_completed_deployment_unavailable"),
            ("intent", "preview_original_intent_unavailable"),
        ):
            with self.subTest(missing=missing):
                self.store.write_preview_record(
                    self.preview.model_copy(update={"serving_generation_id": ""})
                    if missing == "generation"
                    else self.preview
                )
                from contextlib import nullcontext

                guard = (
                    patch.object(self.store, "read_idempotency_record_by_id", return_value=None)
                    if missing == "completion"
                    else patch.object(self.store, "read_original_mutation_intent", return_value={})
                    if missing == "intent"
                    else nullcontext()
                )
                with guard:
                    response = await self.collect()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn(code, response.text)
                self.remote.assert_not_called()
                self.assertIsNone(
                    self.store.read_disposable_lane_safety(
                        self.profile.product, "cm", self.issued.preview_slug
                    )
                )

    async def test_missing_and_unreadable_facts_refuse_without_publishing_safety(self) -> None:
        for frame, code in (
            (
                {"type": "complete", "unavailable": True, "reason_code": "session_file_unreadable"},
                "session_file_unreadable",
            ),
            (
                {"type": "complete", "recent_signins": False, "authenticated_sessions": False},
                "probe_result_invalid",
            ),
        ):
            with self.subTest(code=code):
                self.frame = frame
                response = await self.collect()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("preview_qualification_missing:" + code, response.text)
                self.assertIsNone(
                    self.store.read_disposable_lane_safety(
                        self.profile.product, "cm", self.issued.preview_slug
                    )
                )

    async def test_generation_drift_and_provider_denial_cannot_publish(self) -> None:
        def changed(**kwargs: Any) -> None:
            self.store.write_preview_generation_record(
                self.generation.model_copy(
                    update={
                        "runtime_identity": self.identity.model_copy(
                            update={"deployed_at": "2026-10-29T00:00:00Z"}
                        )
                    }
                )
            )
            self.read_frame(**kwargs)

        for effect, expected in (
            (changed, 409),
            (NativeSourceAuthorizationDenied("private-provider-error"), 403),
        ):
            with self.subTest(expected=expected):
                self.store.write_preview_generation_record(self.generation)
                self.remote.side_effect = effect
                response = await self.collect()
                self.assertEqual(response.status_code, expected, response.text)
                self.assertNotIn("private-provider-error", response.text)
                self.assertIsNone(
                    self.store.read_disposable_lane_safety(
                        self.profile.product, "cm", self.issued.preview_slug
                    )
                )

    async def test_production_comparison_drift_and_missing_versions_refuse(self) -> None:
        def rotated(**kwargs: Any) -> None:
            self.production["ODOO_DB_PASSWORD"] = "rotated-private-production-password"
            self.read_frame(**kwargs)

        self.remote.side_effect = rotated
        response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview_qualification_authority_changed", response.text)
        with patch(
            "control_plane.odoo_preview_qualification.secrets.resolve_secret_values_for_integration_from_store",
            return_value={},
        ):
            response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview_production_credential_versions_unavailable", response.text)
