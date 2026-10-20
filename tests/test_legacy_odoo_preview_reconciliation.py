import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from typing import Any

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_execution import (
    odoo_preview_reconciliation_key,
    odoo_preview_target_key,
    preview_execution_lock_name,
)
from control_plane.legacy_preview_completion import (
    LegacyPreviewRetirementConflict,
    reconciliation_completion_time,
)
from control_plane.legacy_preview_reconciliation import (
    bind_legacy_preview,
    LegacyPreviewReconciliationRequest,
)
from tests import test_legacy_preview_reconciliation as legacy_tests
from tests.support.http import request as http_request


class LegacyOdooPreviewReconciliationTests(legacy_tests.LegacyPreviewReconciliationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.profile = self.profile.model_copy(update={"driver_id": "odoo"})
        self.store.write_product_profile_record(self.profile)
        self.composes: dict[str, dict[str, object]] = {}
        self.domains: dict[str, object] = {}
        self.reads: list[str] = []
        self.raw_inventory: object | None = None
        self.provider = patch(
            "control_plane.legacy_odoo_preview.api.dokploy_request", side_effect=self.provider_read
        )
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def provider_read(self, **kwargs: Any) -> object:
        self.assertEqual(kwargs.get("method", "GET"), "GET")
        path = kwargs["path"]
        self.reads.append(path)
        if path == "/api/project.all":
            if self.raw_inventory is not None:
                return copy.deepcopy(self.raw_inventory)
            return [
                {
                    "environments": [
                        {
                            "environmentId": "preview-environment",
                            "compose": [
                                {"composeId": key, "name": value["name"]}
                                for key, value in self.composes.items()
                            ],
                        }
                    ]
                }
            ]
        identifier = kwargs["query"]["composeId"]
        if path == "/api/compose.one":
            return copy.deepcopy(self.composes[identifier])
        if path == "/api/domain.byComposeId":
            return copy.deepcopy(self.domains.get(identifier, []))
        raise AssertionError(f"Unexpected provider read {path}")

    def compose(
        self, *, name: str = "unrelated-compose", image: str = "example.invalid/other:tag"
    ) -> None:
        self.composes["compose-1"] = {
            "composeId": "compose-1",
            "environmentId": "preview-environment",
            "name": name,
            "appName": name + "-suffix",
            "sourceType": "raw",
            "composeFile": "x-base: &base\n  image: "
            + image
            + "\nservices:\n  web:\n    <<: *base\n",
        }

    async def test_absent_compose_round_trip_has_authentic_record_only_receipt(self) -> None:
        plan = await self.plan()
        applied = await self.apply(plan)
        self.assertEqual(applied.status_code, 202, applied.text)
        closed = self.store.read_preview_record(self.preview.preview_id)
        self.assertEqual(closed.state, "destroyed")
        self.assertTrue(closed.reconciliation_receipt_id)
        self.assertEqual(reconciliation_completion_time(self.store, closed), closed.destroyed_at)
        replay = await self.apply(plan)
        self.assertTrue(replay.json()["replayed"])
        inspected = await self.call("inspect")
        self.assertEqual(inspected.json()["result"]["preview_state"], "destroyed")
        self.assertTrue(inspected.json()["result"]["provider_absence_verified"])
        self.assertTrue(self.search.called)

    async def test_exact_compose_or_domain_blocks_apply_without_delete(self) -> None:
        self.compose(name="example-site-preview-pr-1")
        planned = await self.plan()
        self.assertFalse(planned["apply_eligible"])
        self.compose()
        self.domains["compose-1"] = [{"host": "pr-1.example.test"}]
        planned = await self.plan()
        self.assertFalse(planned["apply_eligible"])
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_renamed_raw_compose_with_product_image_is_not_absence(self) -> None:
        self.compose(image=self.profile.image.repository + "@sha256:" + "a" * 64)
        response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_private_image_binding_is_not_disclosed_and_duplicate_refuses(self) -> None:
        self.compose(image="${IMAGE_REFERENCE}")
        self.composes["compose-1"]["env"] = (
            "IMAGE_REFERENCE="
            + self.profile.image.repository
            + ":current\nPRIVATE_VALUE=fixture-private"
        )
        response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("fixture-private", response.text)
        self.composes["compose-1"]["env"] = "IMAGE_REFERENCE=x\nexport IMAGE_REFERENCE=y"
        response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_unowned_unknown_source_and_malformed_domains_fail_closed(self) -> None:
        for source in (
            "services: [",
            "services: {}",
            "services: {web: {build: .}}",
            "services: {web: {image: '${MISSING}'}}",
        ):
            with self.subTest(source=source):
                self.compose()
                self.composes["compose-1"]["composeFile"] = source
                response = await self.call("inspect")
                self.assertEqual(response.status_code, 409, response.text)
        malformed_domains: tuple[object, ...] = (
            {},
            [None],
            [{"host": ""}],
            [{"host": "other.test", "composeId": "foreign"}],
        )
        for domains in malformed_domains:
            with self.subTest(domains=domains):
                self.compose()
                self.domains["compose-1"] = domains
                response = await self.call("inspect")
                self.assertEqual(response.status_code, 409, response.text)

    async def test_incomplete_or_duplicate_inventory_does_not_prove_absence(self) -> None:
        malformed_inventories: tuple[object, ...] = (
            {},
            [{"environments": None}],
            [
                {
                    "environments": [
                        {
                            "environmentId": "e",
                            "compose": [
                                {"composeId": "same", "name": "one"},
                                {"composeId": "same", "name": "two"},
                            ],
                        }
                    ]
                }
            ],
        )
        for inventory in malformed_inventories:
            with self.subTest(inventory=inventory):
                self.raw_inventory = inventory
                response = await self.call("inspect")
                self.assertEqual(response.status_code, 409, response.text)

    async def test_known_category_bound_sibling_can_have_unknown_source_but_not_this_domain(
        self,
    ) -> None:
        sibling = self.preview.model_copy(update={"preview_id": "sibling", "anchor_pr_number": 7})
        self.store.write_preview_record(sibling)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=sibling.context,
                instance="pr-7",
                target_id="compose-1",
                updated_at=sibling.updated_at,
            )
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=sibling.context,
                instance="pr-7",
                target_type="compose",
                target_name="sibling",
                updated_at=sibling.updated_at,
            )
        )
        self.compose()
        self.composes["compose-1"].pop("composeFile")
        planned = await self.plan()
        self.assertTrue(planned["apply_eligible"])
        self.domains["compose-1"] = [{"host": "pr-1.example.test"}]
        response = await self.apply(planned)
        self.assertEqual(response.status_code, 409, response.text)

    async def test_application_id_cannot_exclude_same_string_compose_id(self) -> None:
        self.compose(image=self.profile.image.repository + ":current")
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.profile.lanes[0].context,
                instance="testing",
                target_id="compose-1",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=self.profile.lanes[0].context,
                instance="testing",
                target_type="application",
                target_name="stable-app",
                updated_at=self.profile.updated_at,
            )
        )
        response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_cross_category_application_domain_cannot_be_absent(self) -> None:
        self.search.return_value = ({"applicationId": "foreign-app"},)
        with (
            patch(
                "control_plane.legacy_odoo_preview.api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "foreign-app",
                    "name": "unrelated",
                    "appName": "unrelated-app",
                    "dockerImage": "example.invalid/unrelated:tag",
                },
            ),
            patch(
                "control_plane.legacy_odoo_preview.api.fetch_dokploy_application_domains",
                return_value=[{"applicationId": "foreign-app", "host": "pr-1.example.test"}],
            ),
        ):
            plan = await self.plan()
        self.assertFalse(plan["apply_eligible"])
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_nonstring_repository_metadata_does_not_prove_unrelated(self) -> None:
        self.compose()
        self.composes["compose-1"]["repository"] = ["unknown"]
        response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_completion_insert_failure_rolls_back_preview_and_receipt(self) -> None:
        plan = await self.plan()
        with patch.object(
            self.store, "_idempotency_row", side_effect=RuntimeError("fixture commit failure")
        ):
            with self.assertRaises(RuntimeError):
                await self.apply(plan)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)
        response = await self.apply(plan)
        self.assertEqual(response.status_code, 202, response.text)

    async def test_held_modern_and_raw_manual_target_keys_block_before_provider_read(self) -> None:
        key = odoo_preview_reconciliation_key(
            context=self.profile.preview.context, compose_name="example-site-preview-pr-1"
        )
        target = odoo_preview_target_key(
            context=self.profile.preview.context, compose_name="example-site-preview-pr-1"
        )
        reserved = self.store.reserve_mutation(
            scope="old-manual-worker",
            route_path="/v1/drivers/odoo/preview-apply",
            idempotency_key="held-preview",
            request_fingerprint="fixture",
            lease_owner="fixture-worker",
            reconciliation_key=key,
        )
        self.assertEqual(reserved.status, "acquired")
        assert reserved.record is not None
        self.assertEqual(reserved.record.provider_target_key, key)
        response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.reads)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (reserved.record,))
        # The exact same durable target identity uses a different physical lock namespace.
        with self.store.testing_replacement_execution_lock(
            preview_execution_lock_name(target)
        ) as check:
            self.assertIsNotNone(check)
            response = await self.call("inspect")
        self.assertEqual(response.status_code, 409, response.text)

    def test_integrated_product_retirement_does_not_gain_odoo_support(self) -> None:
        with self.assertRaisesRegex(ValueError, "generic-web"):
            bind_legacy_preview(
                self.store,
                LegacyPreviewReconciliationRequest(
                    product=self.profile.product,
                    preview_id=self.preview.preview_id,
                    reason="fixture",
                ),
                allow_tracked=True,
            )

    async def closed_preview(self) -> Any:
        response = await self.apply(await self.plan())
        self.assertEqual(response.status_code, 202, response.text)
        return self.store.read_preview_record(self.preview.preview_id)

    async def test_stale_bare_replacement_and_old_generation_cannot_erase_terminal_proof(
        self,
    ) -> None:
        original = self.historical_generation().model_copy(update={"runtime_identity": None})
        self.store.write_preview_generation_record(original)
        closed = await self.closed_preview()
        later = (
            datetime.fromisoformat(closed.destroyed_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()
        replacement = closed.model_copy(
            update={
                "state": "active",
                "updated_at": later,
                "destroyed_at": "",
                "destroy_reason": "",
            }
        )
        with self.assertRaises(LegacyPreviewRetirementConflict):
            self.store.write_preview_record(replacement)
        old = original.model_copy(update={"ready_at": later, "finished_at": later})
        with self.assertRaises(LegacyPreviewRetirementConflict):
            self.store.write_preview_generation_evidence_records(
                preview_record=replacement,
                generation_record=old,
            )
        with self.assertRaises(LegacyPreviewRetirementConflict):
            self.store.write_preview_generation_evidence_records(
                preview_record=replacement,
                generation_record=original.model_copy(update={"requested_at": later}),
            )
        with self.assertRaises(LegacyPreviewRetirementConflict):
            self.store.write_preview_generation_record(
                original.model_copy(update={"requested_at": later})
            )
        self.assertEqual(self.store.read_preview_record(closed.preview_id), closed)
        self.assertEqual(
            self.store.read_preview_generation_record(original.generation_id), original
        )
        fresh = original.model_copy(
            update={
                "generation_id": "fresh-after-retirement",
                "sequence": original.sequence + 1,
                "requested_at": later,
                "ready_at": later,
                "finished_at": later,
            }
        )
        self.store.write_preview_generation_evidence_records(
            preview_record=replacement.model_copy(
                update={
                    "active_generation_id": fresh.generation_id,
                    "serving_generation_id": fresh.generation_id,
                    "latest_generation_id": fresh.generation_id,
                }
            ),
            generation_record=fresh,
        )
        self.assertFalse(
            self.store.read_preview_record(closed.preview_id).reconciliation_receipt_id
        )

    async def test_stale_http_generation_evidence_returns_conflict_without_partial_write(
        self,
    ) -> None:
        closed = await self.closed_preview()
        future = (
            datetime.fromisoformat(closed.destroyed_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()
        app = self.build_app(("preview_generation.write",))
        identity = {
            "context": self.preview.context,
            "anchor_repo": self.preview.anchor_repo,
            "anchor_pr_number": self.preview.anchor_pr_number,
            "anchor_pr_url": self.preview.anchor_pr_url,
        }
        response = await http_request(
            app,
            "POST",
            "/v1/evidence/previews/generations",
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "old-generation",
            },
            payload={
                "product": self.profile.product,
                "preview": {
                    **identity,
                    "canonical_url": self.preview.canonical_url,
                    "created_at": self.preview.created_at,
                    "updated_at": future,
                },
                "generation": {
                    **identity,
                    "generation_id": "old-delayed-generation",
                    "anchor_head_sha": "a" * 40,
                    "state": "ready",
                    "requested_reason": "fixture",
                    "requested_at": self.preview.created_at,
                    "ready_at": future,
                    "finished_at": future,
                    "resolved_manifest_fingerprint": "fixture-original",
                    "artifact_id": "fixture-artifact",
                },
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "preview_retirement_conflict")
        self.assertEqual(self.store.read_preview_record(closed.preview_id), closed)
        self.assertEqual(
            self.store.list_preview_generation_records(preview_id=closed.preview_id), ()
        )

    async def test_receipt_pointer_and_arbitrary_time_do_not_establish_an_ordering_barrier(
        self,
    ) -> None:
        closed = await self.closed_preview()
        self.assertEqual(
            reconciliation_completion_time(self.store, closed, product="wrong-product"), ""
        )
        self.assertEqual(
            reconciliation_completion_time(
                self.store,
                closed.model_copy(
                    update={
                        "reconciliation_receipt_id": "missing",
                    }
                ),
            ),
            "",
        )
        self.assertEqual(
            reconciliation_completion_time(
                self.store,
                closed.model_copy(
                    update={
                        "destroyed_at": self.preview.created_at,
                    }
                ),
            ),
            "",
        )
        receipt = self.store.read_idempotency_record_by_id(closed.reconciliation_receipt_id)
        assert receipt is not None
        for changes in (
            {"mode": "plan"},
            {"product": "wrong-product"},
            {"context": "foreign"},
            {"provider_absence_verified": False},
            {"provider_writes": True},
        ):
            result = {**receipt.response_payload["result"], **changes}
            changed = receipt.model_copy(
                update={"response_payload": {**receipt.response_payload, "result": result}}
            )
            self.store.write_idempotency_record(changed)
            self.assertEqual(
                reconciliation_completion_time(self.store, closed, product=self.profile.product), ""
            )
        self.store.write_idempotency_record(receipt)

    def prepared_operation(
        self, fake: Any, key: str, issued_at: datetime, operation_name: str = "refresh"
    ) -> Any:
        from control_plane.odoo_preview_apply_execution import prepare_odoo_preview_apply_operation
        from control_plane.odoo_preview_apply_http import (
            OdooPreviewApplyEnvelope,
            issue_odoo_preview_apply_plan,
            validate_odoo_preview_issued_plan,
        )
        from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsRequest

        planned = fake.build_inputs(
            profile=self.profile,
            request=OdooPreviewApplyInputsRequest(
                product=self.profile.product,
                pr_number=self.preview.anchor_pr_number,
                operation=operation_name,
                preview_slug="custom-physical-name",
                image_reference=self.profile.image.repository + "@sha256:" + "a" * 64,
                source_git_ref="c" * 40,
                source="fixture",
            ),
        )
        # Exercise a supported physical name distinct from the profile-derived lock key.
        planned["preview_slug"] = "custom-physical-name"
        planned["runtime_plan"]["preview_slug"] = "custom-physical-name"
        planned["dry_run_plan"]["preview_slug"] = "custom-physical-name"
        planned["dry_run_plan"]["compose_name"] = "custom-physical-name"
        issued = issue_odoo_preview_apply_plan(result=planned, plan_id=key, issued_at=issued_at)
        envelope = OdooPreviewApplyEnvelope.model_validate(
            {
                "product": self.profile.product,
                "apply": {
                    "dry_run_plan": issued.dry_run_plan.model_dump(mode="json"),
                    "image_reference": issued.plan_request.image_reference,
                    "wait_for_deploy": False,
                    "smoke_check": False,
                },
            }
        )
        validated = validate_odoo_preview_issued_plan(
            plan_id=key, issued_plan=issued, request=envelope
        )
        return prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=validated,
            issued_plan=issued,
            reservation_scope="fixture-worker",
            idempotency_key=key,
            request_fingerprint=key,
            trace_id=key,
            execute_apply=fake.execute_apply,
            observe_apply=Mock(),
        )

    async def test_old_unreserved_custom_name_plan_cannot_recreate_provider_target(self) -> None:
        from control_plane.odoo_preview_apply_http import OdooPreviewPlanProvenanceError
        from tests.test_product_reconcile import FakePreviewProvider

        fake = FakePreviewProvider()

        def operation(key: str, issued_at: datetime, operation_name: str = "refresh") -> Any:
            return self.prepared_operation(fake, key, issued_at, operation_name)

        old = operation("old-custom-name-plan", datetime.now(timezone.utc) - timedelta(seconds=1))
        closed = await self.closed_preview()
        with patch.object(old.adapter, "capture_schedule_binding"):
            with self.assertRaises(OdooPreviewPlanProvenanceError):
                old.run(self.store)
        self.assertFalse(fake.applied)
        self.assertEqual(self.store.read_preview_record(closed.preview_id), closed)
        later = datetime.fromisoformat(closed.destroyed_at.replace("Z", "+00:00")) + timedelta(
            seconds=1
        )
        destroy = operation("fresh-custom-destroy", later, "destroy")
        with patch.object(destroy.adapter, "capture_schedule_binding"):
            result = destroy.run(self.store)
        self.assertEqual(result.status, "completed", result.response_payload)
        destroyed = self.store.read_preview_record(closed.preview_id)
        self.assertEqual(destroyed.state, "destroyed")
        self.assertFalse(destroyed.reconciliation_receipt_id)
        self.assertNotEqual(destroyed.destroy_reason, closed.destroy_reason)
        fresh = operation("fresh-custom-name-plan", later + timedelta(seconds=1))
        with patch.object(fresh.adapter, "capture_schedule_binding"):
            result = fresh.run(self.store)
        self.assertEqual(result.status, "completed", result.response_payload)
        self.assertTrue(fake.applied)
        resumed = self.store.read_preview_record(closed.preview_id)
        self.assertEqual(resumed.state, "active")
        self.assertFalse(resumed.reconciliation_receipt_id)
