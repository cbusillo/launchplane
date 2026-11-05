from contextlib import nullcontext
from pathlib import Path
import base64
import click
import json
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from control_plane.contracts.odoo_post_deploy_payload import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.disposable_lane_collection import collect_disposable_lane_facts
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    LaunchplaneProductProfileRecord,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.http_app import LaunchplaneErrorDetail
from control_plane.odoo_testing_usage import (
    MAX_CREDENTIAL_CANDIDATES,
    ODOO_USAGE_REASON_CODES,
    OdooTestingUsage,
    credential_digest,
)
from control_plane.integration_readback import INTEGRATION_FAMILIES
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    apply_odoo_preview_lifecycle_evidence,
    build_odoo_preview_runtime_identity,
)
from control_plane.odoo_preview_qualification import PreviewQualificationMissing
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_disposable_lane_authority import _DisposableRuntimeAuthorityFixture
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload
from tests.support.http import get
from tests.support.auth import scoped_agent_transport


class PreviewQualificationTests(_DisposableRuntimeAuthorityFixture):
    async def evidence(self, *, product: str = "", **changes: object) -> Any:
        query = {
            "pr_number": self.preview.anchor_pr_number,
            "source_git_ref": self.identity.source_git_ref,
            "image_digest": self.identity.image_reference.partition("@")[2],
            "original_reservation_id": self.original.record_id,
            "preview_id": self.preview.preview_id,
            "generation_id": self.generation.generation_id,
            **changes,
        }
        return await get(
            self.app,
            f"/v1/products/{product or self.profile.product}/preview-evidence?{urlencode(query)}",
            headers={"Authorization": "Bearer local-operator-token"},
        )

    async def test_inert_evidence_reads_completed_original_and_probes_without_publication(
        self,
    ) -> None:
        with (
            patch.object(self.store, "reserve_mutation", side_effect=AssertionError("mutation")),
            patch.object(
                self.store,
                "publish_collected_disposable_lane_safety",
                side_effect=AssertionError("publication"),
            ),
        ):
            response = await self.evidence()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["original"]["reservation_id"], self.original.record_id)
        self.assertEqual(result["original"]["state"], "completed")
        self.assertEqual(result["binding"]["generation_id"], self.generation.generation_id)
        self.assertEqual(result["current"]["usage"], "unused")
        self.assertEqual(result["current"]["production_resources"], "isolated")
        self.assertEqual(result["current"]["runtime_identity"], "verified")
        self.assertIsNone(
            self.store.read_disposable_lane_safety(
                self.profile.product,
                "cm",
                self.issued.preview_slug,
            )
        )
        self.assertEqual(
            self.store.read_idempotency_record_by_id(self.original.record_id), self.original
        )
        for secret in (
            *self.production.values(),
            "private-provider-token",
            "fixture-local-password",
        ):
            self.assertNotIn(secret, response.text)

    async def test_inert_evidence_refuses_wrong_original_tuple_before_probes(self) -> None:
        for change in (
            {"source_git_ref": "f" * 40},
            {"image_digest": "sha256:" + "f" * 64},
            {"pr_number": 999},
            {"generation_id": "different-generation"},
        ):
            with self.subTest(change=change):
                self.remote.reset_mock()
                response = await self.evidence(**change)
                self.assertEqual(response.status_code, 409, response.text)
                self.remote.assert_not_called()

    async def test_inert_evidence_refuses_other_product_and_unauthorized_lookup(self) -> None:
        response = await self.evidence(product="example-site")
        self.assertEqual(response.status_code, 409, response.text)
        self.remote.assert_not_called()
        with (
            scoped_agent_transport(),
            patch.object(self.store, "read_idempotency_record_by_id") as original,
        ):
            response = await self.evidence()
        self.assertEqual(response.status_code, 403, response.text)
        original.assert_not_called()
        self.remote.assert_not_called()

    async def test_foreign_preview_selection_refuses_before_preview_lookup(self) -> None:
        with patch.object(self.store, "read_preview_record") as preview:
            response = await self.evidence(preview_id="preview-other-product-pr-1")
        self.assertEqual(response.status_code, 409, response.text)
        preview.assert_not_called()
        self.remote.assert_not_called()

    async def test_inert_evidence_preserves_shared_resource_and_credential_findings(self) -> None:
        from control_plane.odoo_preview_resources import preview_resource_environment

        self.production["ODOO_DATA_VOLUME"] = preview_resource_environment(self.identity.instance)[
            "ODOO_DATA_VOLUME"
        ]
        response = await self.evidence()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["current"]["production_resources"], "shared")
        self.production["ODOO_DATA_VOLUME"] = "prod-data"
        self.preview_candidates = [self.production["ODOO_ADMIN_PASSWORD"]]
        response = await self.evidence()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["current"]["credential_isolation"], "shared")
        self.assertNotIn(self.production["ODOO_ADMIN_PASSWORD"], response.text)

    async def test_inert_evidence_refuses_provider_denial_without_private_error(self) -> None:
        self.remote.side_effect = NativeSourceAuthorizationDenied("private-provider-detail")
        response = await self.evidence()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn("private-provider-detail", response.text)

    async def test_inert_evidence_rechecks_generation_after_history(self) -> None:
        from control_plane import preview_evidence

        def change_generation(*_args: Any, **_kwargs: Any) -> dict[str, object]:
            self.store.write_preview_record(
                self.preview.model_copy(update={"serving_generation_id": "changed"})
            )
            return {}

        with patch.object(
            preview_evidence, "read_preview_provider_history", side_effect=change_generation
        ):
            response = await self.evidence()
        self.assertEqual(response.status_code, 409, response.text)

    async def test_inert_evidence_keeps_unreadable_probe_unknown(self) -> None:
        self.frame.clear()
        self.frame.update(
            type="complete", unavailable=True, reason_code="resource_copy_database_readable"
        )
        response = await self.evidence()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["current"]["usage"], "unknown")
        self.assertEqual(result["current"]["production_resources"], "unknown")

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
        self.preview_candidates = ["fixture-local-password"]
        self.production_status = "done"

    def provider(self, **kwargs: Any) -> dict[str, object]:
        if kwargs["target_id"] == "production-target":
            return {
                "name": "production",
                "composeStatus": self.production_status,
                "env": "\n".join(f"{key}={value}" for key, value in self.production.items()),
            }
        return {
            "name": self.identity.instance,
            "appName": "preview-project",
            "serverId": "fixture-server",
        }

    def read_frame(self, **kwargs: Any) -> None:
        frame = dict(self.frame)
        if frame.get("resources_qualified"):
            nonce = kwargs["payload"]["preview_spec"]["credential_nonce"]
            frame["credential_hashes"] = [
                credential_digest(nonce, value) for value in self.preview_candidates
            ]
        kwargs["receive"](frame)

    async def assert_secret_separation_refused(
        self, production: str, candidate: str, *, key: str = "SERVICE_SECRET"
    ) -> None:
        self.production[key] = production
        self.preview_candidates = [candidate]
        response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(response.json()["error"]["message"].endswith(":" + key), response.text)
        self.assertNotIn("private-production", response.text)
        self.assertIsNone(
            self.store.read_disposable_lane_safety(
                self.profile.product, "cm", self.issued.preview_slug
            )
        )

    async def test_production_url_password_in_preview_standalone_value_refuses(self) -> None:
        self.production["SMTP_URL"] = "smtps://sender:private-production-token@mail.example.invalid"
        self.preview_candidates = ["private-production-token"]
        response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview_production_credential_separation_unproven", response.text)
        spec = self.remote.call_args.kwargs["payload"]["preview_spec"]
        self.assertNotIn("private-production", str(spec))

    async def test_credential_context_survives_decoding_and_url_queries(self) -> None:
        for production, preview, suffix in (
            (
                json.dumps(
                    {
                        "webhook_url": "https://hooks.invalid/private-path-token",
                        "region": "production",
                    }
                ),
                "https://hooks.invalid/private-path-token",
                "SERVICE_SECRET.webhook_url",
            ),
            (
                "https://hooks.invalid/path?sig=private-signature",
                "private-signature",
                "SERVICE_SECRET.sig",
            ),
            (
                json.dumps({"key": "destination", "value": {"value": "private-token"}}),
                "private-token",
                "SERVICE_SECRET.destination",
            ),
        ):
            with self.subTest(suffix=suffix):
                await self.assert_secret_separation_refused(production, preview)

    async def test_unclassified_root_keeps_credential_ancestors_through_decode_and_query(
        self,
    ) -> None:
        address = "https://hooks.invalid/x?sig=private-signature"
        for value in (address, json.dumps({"destination": address})):
            with self.subTest(value=value):
                await self.assert_secret_separation_refused(
                    json.dumps({"webhook_secret": value}), "private-signature", key="APP_CONFIG"
                )

    async def test_large_unclassified_metadata_does_not_consume_the_credential_bound(self) -> None:
        self.production["PUBLIC_OPTIONS"] = json.dumps(
            {"items": [f"metadata-{index}" for index in range(MAX_CREDENTIAL_CANDIDATES)]}
        )
        response = await self.collect()
        self.assertEqual(response.status_code, 202, response.text)

    async def test_ambiguous_public_url_purpose_refuses_without_weakening_bearer_url_protection(
        self,
    ) -> None:
        credential = "https://auth.invalid/public-or-bearer-path"
        for key in ("token_uri", "callback_secret_url"):
            with self.subTest(key=key):
                await self.assert_secret_separation_refused(
                    json.dumps({key: credential}), credential
                )

    async def test_credential_shaped_scalars_and_strings_remain_protected(self) -> None:
        for key, value, status in (
            ("auth_password_policy.minlength", "12", 409),
            ("auth_password_policy.enabled", "True", 409),
            ("custom.api_token", "12", 409),
            ("custom.api_token", "short", 409),
            ("custom.api_token", "True", 409),
            ("custom.api_token", 12, 409),
            ("custom.api_token.value", "12", 409),
            ("mail.ui_enabled", True, 202),
            ("auth_signup.reset_password", True, 409),
            ("auth_signup.reset_password", "True", 409),
        ):
            with self.subTest(key=key, value=value):
                self.production[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = base64.b64encode(
                    json.dumps(
                        {
                            "config_parameters": [
                                {"key": key, "value": {"source": "literal", "value": value}}
                            ]
                        }
                    ).encode()
                ).decode()
                self.preview_candidates = [str(value)]
                response = await self.collect()
                self.assertEqual(response.status_code, status, response.text)

    async def test_production_configuration_errors_are_bounded_without_publication(self) -> None:
        with patch(
            "control_plane.odoo_preview_qualification.source.read_dokploy_config",
            side_effect=click.ClickException("private-provider-diagnostic"),
        ):
            response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview_production_credentials_unavailable", response.text)
        self.assertNotIn("private-provider-diagnostic", response.text)
        self.assertIsNone(
            self.store.read_disposable_lane_safety(
                self.profile.product, "cm", self.issued.preview_slug
            )
        )

    async def test_held_preview_keeps_the_hold_as_its_usage_evidence(self) -> None:
        from control_plane.contracts.dokploy_target_record import (
            DokployTargetPolicies,
            DokployTargetStaffTestingHold,
        )

        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm",
                instance=self.issued.preview_slug,
                updated_at=self.profile.updated_at,
                policies=DokployTargetPolicies(
                    staff_testing_hold=DokployTargetStaffTestingHold(reason="Staff trial")
                ),
            )
        )
        response = await self.collect()
        self.assertEqual(response.status_code, 202, response.text)
        safety = self.store.read_disposable_lane_safety(
            self.profile.product, "cm", self.issued.preview_slug
        )
        assert safety is not None
        self.assertEqual(safety.usage, "in_use")
        held = collect_disposable_lane_facts(
            self.store,
            context="cm",
            instance=self.issued.preview_slug,
            target=self.store.read_dokploy_target_record(
                context_name="cm", instance_name=self.issued.preview_slug
            ),
            target_id=None,
            binding_sha256=safety.binding_sha256,
        )
        self.assertTrue(safety.usage_evidence.startswith(held.usage_evidence), safety)

    async def test_nested_bearer_url_is_protected_when_whole_payloads_differ(self) -> None:
        credential = "https://hooks.invalid/private-path-token"
        await self.assert_secret_separation_refused(
            json.dumps({"callback_secret_url": credential, "region": "production"}), credential
        )

    async def test_nested_credential_names_keep_context_without_disclosing_payload_keys(
        self,
    ) -> None:
        for payload, candidate in (
            ({"api_token": {"value": 12}}, "12"),
            ({"private-production-sensitive-key": "fixture-credential"}, "fixture-credential"),
        ):
            with self.subTest(candidate=candidate):
                await self.assert_secret_separation_refused(json.dumps(payload), candidate)

    async def test_other_integrations_and_encoded_provider_secrets_are_compared(self) -> None:
        secret = "private-extra-production-token"
        binding = SecretBinding(
            binding_id="fixture-binding",
            secret_id="fixture-secret",
            integration="custom-provider",
            binding_key="API_TOKEN",
            context="cm",
            instance="prod",
            created_at=self.profile.updated_at,
            updated_at=self.profile.updated_at,
        )
        self.preview_candidates = [secret]

        def values(**kwargs: Any) -> dict[str, str]:
            return (
                {"API_TOKEN": secret}
                if kwargs["integration"] == binding.integration
                else {key: self.production[key] for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS}
            )

        with (
            patch(
                "control_plane.storage.postgres.PostgresRecordStore.list_secret_bindings",
                return_value=(binding,),
            ),
            patch(
                "control_plane.odoo_preview_qualification.secrets.resolve_secret_values_for_integration_from_store",
                side_effect=values,
            ),
        ):
            response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview_production_credential_separation_unproven", response.text)
        self.production[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY] = base64.b64encode(
            json.dumps(
                {
                    "config_parameters": [
                        {"key": "custom.api_token", "value": {"source": "literal", "value": secret}}
                    ]
                }
            ).encode()
        ).decode()
        response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(
            response.json()["error"]["message"].endswith(
                ":" + ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
            ),
            response.text,
        )

    async def test_credential_inventory_overflow_refuses_without_publication(self) -> None:
        self.production.update(
            {
                f"EXTRA_TOKEN_{index}": f"private-{index}"
                for index in range(MAX_CREDENTIAL_CANDIDATES)
            }
        )
        response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview_production_credential_inventory_unavailable", response.text)
        self.remote.assert_not_called()
        self.assertIsNone(
            self.store.read_disposable_lane_safety(
                self.profile.product, "cm", self.issued.preview_slug
            )
        )

    async def test_production_status_ticks_do_not_invalidate_credential_authority(self) -> None:
        def status_tick(**kwargs: Any) -> None:
            self.production_status = "idle"
            self.read_frame(**kwargs)

        self.remote.side_effect = status_tick
        response = await self.collect()
        self.assertEqual(response.status_code, 202, response.text)

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
                policy = json.loads(base64.b64decode(spec["integration_spec"]))
                restore_only = {
                    family.integration for family in INTEGRATION_FAMILIES if family.restore_only
                }
                self.assertTrue(
                    restore_only.isdisjoint(family["integration"] for family in policy["families"])
                )
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
                self.assertNotIn("reason", response.json()["error"])
                self.remote.assert_not_called()
                self.assertIsNone(
                    self.store.read_disposable_lane_safety(
                        self.profile.product, "cm", self.issued.preview_slug
                    )
                )

    async def test_missing_and_unreadable_facts_refuse_without_publishing_safety(self) -> None:
        resource_reason = f"resource_{ReadbackPredicate.MOUNTS.value}"
        for frame, code in (
            (
                {"type": "complete", "unavailable": True, "reason_code": "session_file_unreadable"},
                "session_file_unreadable",
            ),
            (
                {"type": "complete", "recent_signins": False, "authenticated_sessions": False},
                "probe_result_invalid",
            ),
            (
                {"type": "complete", "unavailable": True, "reason_code": resource_reason},
                resource_reason,
            ),
            (
                {"type": "complete", "unavailable": True, "reason_code": "unrecognized_reason"},
                "probe_result_invalid",
            ),
            (
                {
                    "type": "complete",
                    "unavailable": True,
                    "reason_code": "private-token@host.invalid/database?command=read",
                },
                "probe_result_invalid",
            ),
        ):
            with self.subTest(code=code):
                self.frame = frame
                response = await self.collect()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("preview_qualification_missing:" + code, response.text)
                error = LaunchplaneErrorDetail.model_validate(response.json()["error"])
                self.assertEqual(error.code, "disposable_observation_refused")
                self.assertEqual(error.reason, code)
                self.assertNotIn("private-token", response.text)
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
                self.assertIsNone(
                    self.store.read_disposable_lane_safety(
                        self.profile.product, "cm", self.issued.preview_slug
                    )
                )

    def test_usage_reasons_fit_existing_error_contract(self) -> None:
        for reason in (*ODOO_USAGE_REASON_CODES, "unrecognized_reason"):
            with self.subTest(reason=reason):
                usage = OdooTestingUsage(reason_code=reason)
                refusal = PreviewQualificationMissing("unavailable", usage=usage)
                error = LaunchplaneErrorDetail(
                    code="disposable_observation_refused",
                    message=str(refusal),
                    reason=refusal.reason,
                )
                self.assertEqual(error.reason, usage.payload()["reason_code"])

    async def test_current_shared_alias_cannot_be_overwritten_by_preview_probe(self) -> None:
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm", instance=self.issued.preview_slug, updated_at=self.profile.updated_at
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance=self.issued.preview_slug,
                target_id="production-target",
                updated_at=self.profile.updated_at,
            )
        )
        response = await self.collect()
        self.assertEqual(response.status_code, 409, response.text)
        safety = self.store.read_disposable_lane_safety(
            self.profile.product, "cm", self.issued.preview_slug
        )
        assert safety is not None
        self.assertEqual(safety.production_resources, "shared")
        self.remote.assert_not_called()

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

        for effect, expected, provider_status in (
            (changed, 409, None),
            (NativeSourceAuthorizationDenied("private-provider-error"), 403, None),
            (None, 403, 401),
            (None, 403, 403),
        ):
            with self.subTest(expected=expected, provider_status=provider_status):
                self.store.write_preview_generation_record(self.generation)
                self.remote.reset_mock()
                self.remote.side_effect = effect
                provider_denial = (
                    patch(
                        "control_plane.odoo_preview_qualification.api.fetch_dokploy_target_payload",
                        side_effect=DokployRequestFailed(
                            method="GET",
                            path="/compose.one",
                            detail="private-provider-error",
                            status_code=provider_status,
                        ),
                    )
                    if provider_status
                    else nullcontext()
                )
                with provider_denial:
                    response = await self.collect()
                if provider_status:
                    self.remote.assert_not_called()
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
