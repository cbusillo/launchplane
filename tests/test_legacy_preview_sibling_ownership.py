from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from control_plane.contracts.generic_web_extensions import (
    GenericWebLaneExtensions,
    GenericWebMaintenanceCommand,
)
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.generic_web_preview_http import GENERIC_WEB_PREVIEW_REFRESH_ROUTE
from control_plane.workflows.generic_web_application_lifecycle import preview_application_lifecycle
from control_plane.workflows.generic_web_preview import _build_preview_runtime_identity
from tests.test_legacy_preview_reconciliation import LegacyPreviewReconciliationFixture
from tests.support.http import request


class LegacyGenericSiblingOwnershipTests(LegacyPreviewReconciliationFixture):
    async def produced_sibling(
        self, *, keyed: bool = True, configured_lifecycle: bool = False
    ) -> RuntimeIdentity:
        self.app = self.build_app(
            (
                "preview_inventory.read",
                "preview_destroy.execute",
                "preview_destroyed.write",
                "preview_refresh.execute",
                "preview_generation.write",
            )
        )
        started = datetime.now(timezone.utc)
        self.application = {
            "applicationId": "real-sibling-application",
            "name": "example-site-preview-pr-7",
            "appName": "example-site-pr-7-provider",
            "dockerImage": self.profile.image.repository + "@sha256:" + "b" * 64,
        }
        self.application_domains = [{"host": "pr-7.example.test"}]

        def provider_result(**kwargs: Any) -> dict[str, object]:
            refresh = kwargs["request"]
            runtime = _build_preview_runtime_identity(
                profile=kwargs["profile"],
                preview_slug=refresh.preview_slug,
                image_reference=refresh.image_reference,
                anchor_head_sha=refresh.anchor_head_sha,
            )
            assert runtime is not None
            if configured_lifecycle:
                self.lifecycle = preview_application_lifecycle(
                    host="https://provider.example",
                    token="fixture",
                    application_id=str(self.application["applicationId"]),
                    product=self.profile.product,
                    context=self.profile.preview.context,
                    instance="pr-7",
                    artifact_id=runtime.artifact_id,
                    source_git_ref=runtime.source_git_ref,
                    extensions=GenericWebLaneExtensions(
                        maintenance=(GenericWebMaintenanceCommand(name="prepare", command="true"),)
                    ),
                    record_store=self.store,
                )
                self.lifecycle.capture()
                self.lifecycle.prepare()
                with patch.object(self.lifecycle, "_run_command"):
                    self.lifecycle.maintain()
                self.lifecycle.complete()
            return dict(
                refresh_status="pass",
                refresh_started_at=started.isoformat(),
                refresh_finished_at=datetime.now(timezone.utc).isoformat(),
                product=self.profile.product,
                context=self.profile.preview.context,
                preview_slug="pr-7",
                application_name=self.application["name"],
                application_id=self.application["applicationId"],
                preview_url="https://pr-7.example.test",
                runtime_identity=runtime.model_dump(mode="json"),
            )

        headers = {"Authorization": "Bearer local-operator-token"}
        if keyed:
            headers["Idempotency-Key"] = "real-generic-sibling"
        with patch(
            "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
            side_effect=provider_result,
        ):
            refreshed = await request(
                self.app,
                "POST",
                GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
                headers=headers,
                payload={
                    "product": self.profile.product,
                    "refresh": {
                        "product": self.profile.product,
                        "preview_slug": "pr-7",
                        "anchor_pr_number": 7,
                        "anchor_head_sha": "a" * 40,
                        "preview_url": "https://pr-7.example.test",
                        "image_reference": self.application["dockerImage"],
                    },
                },
            )
        self.assertEqual(refreshed.status_code, 202, refreshed.text)
        self.assertEqual(refreshed.json()["result"]["refresh_status"], "pass")
        finished = datetime.fromisoformat(refreshed.json()["result"]["refresh_finished_at"])
        self.generation_id = refreshed.json()["records"]["generation_id"]
        self.sibling_id = refreshed.json()["records"]["preview_id"]
        self.assertEqual(
            self.store.read_preview_generation_record(self.generation_id).state, "verifying"
        )
        verified = await request(
            self.app,
            "POST",
            "/v1/drivers/generic-web/preview-verification",
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "real-sibling-verification",
            },
            payload={
                "product": self.profile.product,
                "verification": {
                    "context": self.profile.preview.context,
                    "anchor_repo": self.preview.anchor_repo,
                    "anchor_pr_number": 7,
                    "verification_status": "pass",
                    "verified_at": (finished + timedelta(seconds=1)).isoformat(),
                },
            },
        )
        self.assertEqual(verified.status_code, 202, verified.text)
        generation = self.store.read_preview_generation_record(self.generation_id)
        runtime = RuntimeIdentity.model_validate(refreshed.json()["result"]["runtime_identity"])
        self.assertEqual(generation.runtime_identity, runtime)
        self.assertEqual(
            (generation.state, generation.deploy_status, generation.verify_status),
            ("ready", "pass", "pass"),
        )
        self.assertEqual(
            self.store.read_preview_record(self.sibling_id).serving_generation_id,
            generation.generation_id,
        )
        self.assertEqual(self.store.list_provider_target_records(), ())
        self.assertEqual(self.store.list_dokploy_target_id_records(), ())
        self.assertEqual(self.store.list_dokploy_target_records(), ())
        return runtime

    def provider_inventory(self) -> Any:
        self.search.return_value = ({"applicationId": self.application["applicationId"]},)
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                side_effect=lambda **_: self.application,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                side_effect=lambda **_: self.application_domains,
            )
        )
        return stack

    async def test_actual_http_sibling_proves_application_and_rejects_uncertain_receipts(
        self,
    ) -> None:
        runtime = await self.produced_sibling()
        self.assertEqual(self.store.list_deployment_records(), ())
        receipts = self.store.find_generic_preview_refresh_receipts(runtime.deployment_record_id)
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertNotEqual(runtime.deployment_record_id, receipt.record_id)
        with self.provider_inventory():
            plan = await self.plan()
            self.assertTrue(plan["apply_eligible"])
            for changed in (
                receipt.model_copy(update={"response_status_code": 409}),
                receipt.model_copy(
                    update={
                        "response_payload": {
                            **receipt.response_payload,
                            "records": {
                                **receipt.response_payload["records"],
                                "generation_id": "other-generation",
                            },
                        }
                    }
                ),
            ):
                with patch.object(
                    self.store, "find_generic_preview_refresh_receipts", return_value=(changed,)
                ):
                    self.assertEqual((await self.call("inspect")).status_code, 409)
            with patch.object(self.store, "find_generic_preview_refresh_receipts", return_value=()):
                self.assertEqual((await self.call("inspect")).status_code, 409)
            self.application_domains[:] = [{"host": "pr-1.example.test"}]
            self.assertEqual((await self.call("inspect")).status_code, 409)
            self.application_domains[:] = [{"host": "pr-7.example.test"}]
            self.assertEqual((await self.apply(plan)).status_code, 202)
            trace = receipt.response_trace_id + "-duplicate"
            self.store.write_idempotency_record(
                receipt.model_copy(
                    update={
                        "record_id": build_launchplane_idempotency_record_id(
                            response_trace_id=trace
                        ),
                        "response_trace_id": trace,
                        "idempotency_key": "duplicate",
                        "response_payload": {**receipt.response_payload, "trace_id": trace},
                    }
                )
            )
            self.assertEqual(
                len(self.store.find_generic_preview_refresh_receipts(runtime.deployment_record_id)),
                2,
            )
            self.assertEqual((await self.call("inspect")).status_code, 409)

    async def test_actual_configured_lifecycle_proves_application_and_later_hold_refuses(
        self,
    ) -> None:
        runtime = await self.produced_sibling(keyed=False, configured_lifecycle=True)
        self.assertEqual(
            self.store.find_generic_preview_refresh_receipts(runtime.deployment_record_id), ()
        )
        self.assertEqual(len(self.store.list_deployment_records()), 1)
        with self.provider_inventory():
            plan = await self.plan()
            self.assertTrue(plan["apply_eligible"])
            captured_at = self.lifecycle.snapshot.captured_at
            self.lifecycle.snapshot.captured_at = (
                datetime.fromisoformat(
                    self.store.read_preview_generation_record(self.generation_id).requested_at
                )
                - timedelta(seconds=1)
            ).isoformat()
            self.lifecycle.complete()
            self.assertEqual((await self.call("inspect")).status_code, 409)
            self.lifecycle.snapshot.captured_at = captured_at
            self.lifecycle.complete()
            self.lifecycle.hold()
            self.assertEqual((await self.apply(plan)).status_code, 409)

    async def test_unproven_sibling_evidence_leaves_provider_classification_intact(self) -> None:
        from control_plane.legacy_preview_sibling_ownership import current_generic_sibling_ownership

        runtime = await self.produced_sibling()
        receipt = self.store.find_generic_preview_refresh_receipts(runtime.deployment_record_id)[0]
        for changed in (
            receipt.model_copy(update={"response_status_code": 409}),
            receipt.model_copy(
                update={
                    "response_payload": {
                        **receipt.response_payload,
                        "result": {
                            **receipt.response_payload["result"],
                            "refresh_started_at": "unavailable-old-time",
                        },
                    }
                }
            ),
        ):
            with patch.object(
                self.store, "find_generic_preview_refresh_receipts", return_value=(changed,)
            ):
                self.assertEqual((await self.call("inspect")).status_code, 202)
                with self.provider_inventory():
                    self.assertEqual((await self.call("inspect")).status_code, 409)
                self.search.return_value = ()
        original_profile = self.profile
        self.profile = self.profile.model_copy(
            update={
                "preview": self.profile.preview.model_copy(
                    update={"slug_template": "change-{number}"}
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.assertEqual((await self.call("inspect")).status_code, 202)
        with self.provider_inventory():
            self.assertEqual((await self.call("inspect")).status_code, 409)
        self.store.write_product_profile_record(original_profile)
        sibling = self.store.read_preview_record(self.sibling_id)
        with patch.object(
            self.store,
            "find_generic_preview_refresh_receipts",
            side_effect=ValueError("storage identity failure"),
        ):
            with self.assertRaises(ValueError):
                current_generic_sibling_ownership(self.store, original_profile, sibling, "pr-7")

    async def test_uncertain_lifecycle_discards_even_a_successful_http_exclusion(self) -> None:
        from control_plane.workflows.generic_web_application_snapshot import (
            APPLICATION_LIFECYCLE_EVIDENCE_KEY,
        )

        await self.produced_sibling(configured_lifecycle=True)
        record = self.store.list_deployment_records()[0]
        malformed = record.model_copy(
            update={
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={
                        "evidence": {APPLICATION_LIFECYCLE_EVIDENCE_KEY: "old-unreadable-snapshot"}
                    }
                )
            }
        )
        old_binding = record.model_copy(update={"source_git_ref": "b" * 40})
        for changed in (malformed, old_binding):
            with patch.object(self.store, "list_deployment_records", return_value=(changed,)):
                self.assertEqual((await self.call("inspect")).status_code, 202)
                with self.provider_inventory():
                    self.assertEqual((await self.call("inspect")).status_code, 409)
                self.search.return_value = ()
        # A second positively qualified receipt must not be treated as merely
        # missing evidence, even when no provider target currently exists.
        self.lifecycle.application_id = "conflicting-physical-application"
        self.lifecycle.snapshot.target_id = self.lifecycle.application_id
        self.lifecycle.complete()
        self.assertEqual((await self.call("inspect")).status_code, 409)
        self.lifecycle.application_id = str(self.application["applicationId"])
        self.lifecycle.snapshot.target_id = self.lifecycle.application_id
        self.lifecycle.complete()
        self.lifecycle.hold()
        self.assertEqual((await self.call("inspect")).status_code, 202)
        with self.provider_inventory():
            self.assertEqual((await self.call("inspect")).status_code, 409)
