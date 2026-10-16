"""Legacy retirement preserves unknown history and leaves actual replacement gated."""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import DisposableTestingSupersession
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.testing_replacement_recovery import (
    TestingReplacementRecoveryJournal,
    TestingReplacementEffect,
)
from control_plane.odoo_testing_restore import fresh_testing_authority_sha256
from control_plane.storage.postgres import DbOnlyMutationRequest
from control_plane.testing_generation_retirement import (
    ROUTE,
    TestingGenerationRetirementRequest,
    retirement_snapshot,
)
from control_plane.testing_replacement_recovery import (
    FRESH_TESTING_INTENT_BINDING_PREFIX,
    recover_stranded_testing_replacements,
)
from control_plane.workflows.odoo_post_deploy import _write_odoo_instance_override_apply_result
from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario
from tests.test_odoo_testing_restore import generation
from tests.test_product_reconcile import _inventory
from tests.test_testing_replacement_recovery import stamp


class TestingGenerationRetirementTests(unittest.TestCase):
    database_url = ""
    native_source = False

    def setUp(self) -> None:
        self.fixture = FreshTestingBootstrapScenario()
        self.fixture.seed(self.database_url)
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.store = fixture.store
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.service_auth import LaunchplaneAuthzPolicy

        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="retirement-fixture-policy",
                source="fixture",
                updated_at=stamp(),
                policy=LaunchplaneAuthzPolicy(schema_version=2),
            )
        )
        self.context = fixture.fixture.context
        self.product = fixture.profile.product
        self.store.write_repository_inventory_record(
            _inventory(repository=fixture.profile.repository)
        )
        original = fixture.fixture.operation
        superseded = original.model_copy(
            update={
                "status": "superseded",
                "phase": "superseded",
                "finished_at": stamp(),
                "disposable_supersession": DisposableTestingSupersession(
                    binding_sha256=canonical_json_sha256({"fixture": "isolated"}),
                    retained_target_id="compose-testing",
                    replacement_target_id=fixture.resources.target_id,
                    evidence_reference="fixture-qualified-allocation",
                    recorded_at=datetime.now(timezone.utc),
                ),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(superseded)
        source_binding = ""
        if getattr(self, "native_source", False):
            from control_plane.odoo_testing_native_source import native_testing_source_authority

            production = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="prod"
            )
            self.store.write_dokploy_target_record(
                production.model_copy(
                    update={"env": {**production.env, "ODOO_DB_NAME": "production"}}
                )
            )
            source_binding = native_testing_source_authority(
                self.store, profile=fixture.profile, testing_context=self.context
            ).binding_sha256
        self.gen = generation().model_copy(
            update={
                "product": self.product,
                "context": self.context,
                "operation_id": original.operation_id,
                "target_id": fixture.resources.target_id,
                "namespace": fixture.resources.namespace,
                "resources_binding_sha256": fixture.resources.binding_sha256,
                "source_mode": "container-export" if source_binding else "ssh",
                "source_binding_sha256": source_binding,
            }
        )
        self.store.write_fresh_odoo_testing_generation(self.gen)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "domains": (self.gen.domain_host,),
                    "env": {
                        **target.env,
                        "ODOO_PROJECT_NAME": self.gen.namespace,
                        "ODOO_STACK_NAME": self.gen.namespace,
                    },
                }
            )
        )
        with patch("control_plane.odoo_testing_restore.require_distinct_testing_credentials"):
            self.store.request_fresh_testing_reconcile(self.gen)
        queued_generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.context
        )
        assert queued_generation is not None
        self.gen = queued_generation
        override = OdooInstanceOverrideRecord(
            context=self.context, instance="testing", updated_at=stamp()
        )
        self.store.write_odoo_instance_override_record(override)
        digest = fresh_testing_authority_sha256(self.store, self.gen, include_override_results=True)
        self.operation = original.model_copy(
            update={
                "operation_id": "stranded-first-copy",
                "idempotency_key": "original-stranded-key",
                "attempt": 1,
                "started_at": stamp(),
                "recovery_journal": TestingReplacementRecoveryJournal(
                    attempt=1,
                    target_id=self.gen.target_id,
                    target_fingerprint=digest,
                    effects=(
                        TestingReplacementEffect(
                            reference="original-effect",
                            path="/api/compose.update",
                            dispatched_at=stamp(),
                            acknowledged=False,
                            target_id=self.gen.target_id,
                        ),
                    ),
                ),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)
        _write_odoo_instance_override_apply_result(
            record_store=self.store,
            record=override,
            status="fail",
            detail="Copy receiver refused before restore.",
        )
        self.assertNotEqual(
            fresh_testing_authority_sha256(self.store, self.gen, include_override_results=True),
            digest,
        )
        self.body = TestingGenerationRetirementRequest(
            context=self.context,
            operation_id=self.operation.operation_id,
            reason="Retain unprovable original generation for isolated successor",
        )
        self.plan = retirement_snapshot(self.store, self.product, self.body).plan
        self.apply_body = self.body.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": self.plan.plan_sha256}
        )
        self.mutation = DbOnlyMutationRequest(
            scope="fixture-director",
            route_path=ROUTE,
            idempotency_key="retirement-key",
            request_fingerprint=canonical_json_sha256(self.apply_body.model_dump(mode="json")),
            lease_owner="fixture",
            response_status_code=202,
            response_trace_id="fixture-trace",
            response_payload={
                "result": self.plan.model_copy(update={"applied": True}).model_dump(mode="json")
            },
        )

    def apply(self) -> Any:
        return self.store.retire_testing_generation(
            product=self.product, request=self.apply_body, mutation=self.mutation
        )

    def test_retirement_preserves_original_operation_and_reserves_its_hostname(self) -> None:
        profiles = self.store.list_product_profile_records()
        targets = self.store.list_dokploy_target_records()
        runtimes = self.store.list_runtime_environment_records()
        with patch("control_plane.dokploy.api._dokploy_request") as provider:
            self.assertEqual(self.apply()[0], "written")
            provider.assert_not_called()
        self.assertIsNone(self.store.read_fresh_odoo_testing_generation(self.product, self.context))
        retained = self.store.read_retained_testing_generation(self.operation.operation_id)
        assert retained is not None
        self.assertEqual(retained.generation, self.gen)
        self.assertEqual(retained.operation, self.operation)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )
        self.assertIn(self.gen.domain_host, self.store.retained_disposable_hostnames())
        self.assertEqual(self.store.list_product_profile_records(), profiles)
        self.assertEqual(self.store.list_dokploy_target_records(), targets)
        self.assertEqual(self.store.list_runtime_environment_records(), runtimes)
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationCancellation,
            DurableOperationReconciliationAttestation,
        )

        assert self.operation.authorization is not None
        cancellation = DurableOperationCancellation(
            reason="Synthetic stale recovery observation",
            cancelled_at=stamp(),
            caller=self.operation.authorization.caller,
            reconciliation_attestation=DurableOperationReconciliationAttestation(
                provider_inspected_at=stamp(),
                provider_state="Synthetic quiescent observation",
                evidence_reference="fixture:stale-observation",
                safe_to_release=True,
            ),
        )
        stale = self.operation.model_copy(
            update={
                "status": "cancelled",
                "phase": "cancelled",
                "finished_at": cancellation.cancelled_at,
                "cancellation": cancellation,
                "error_code": "",
                "error_message": "",
            }
        )
        with patch(
            "control_plane.testing_replacement_recovery.current_binding_fingerprint",
            return_value=self.operation.recovery_journal.target_fingerprint,
        ):
            self.assertFalse(
                self.store.cancel_pending_odoo_stable_target_replacement_operation_record(
                    stale, expected_record=self.operation
                )
            )
        # Only the already-reviewed isolated successor path may supersede it.
        with (
            patch.object(
                self.store, "supersede_disposable_testing_replacement", return_value=False
            ),
            patch(
                "control_plane.testing_replacement_recovery.current_binding_fingerprint",
                return_value=self.operation.recovery_journal.target_fingerprint,
            ),
            patch(
                "control_plane.testing_replacement_recovery.inspect_testing_replacement",
                return_value={"state": "quiescent_safe_to_retry"},
            ) as inspect,
        ):
            self.assertEqual(
                recover_stranded_testing_replacements(self.store, self.fixture.fixture.root), ()
            )
            inspect.assert_not_called()
        self.assertEqual(self.apply()[0], "replayed")
        with self.assertRaises(ValueError):
            self.store.write_fresh_odoo_testing_generation(self.gen)
        with self.assertRaises(ValueError):
            self.store.retire_testing_generation(
                product=self.product,
                request=self.apply_body,
                mutation=replace(self.mutation, idempotency_key="another-key"),
            )
        self.assertEqual(
            self.store.read_retained_testing_generation(self.operation.operation_id), retained
        )

    def test_stale_review_and_failed_transaction_preserve_both_custodies(self) -> None:
        changed = self.operation.model_copy(update={"error_message": "Changed held diagnostic"})
        self.store.write_odoo_stable_target_replacement_operation_record(changed)
        with self.assertRaises(ValueError):
            self.apply()
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)
        with patch.object(
            self.store,
            "_after_product_authority_bundle_step",
            side_effect=RuntimeError("fixture interrupted commit"),
        ):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.product, self.context), self.gen
        )
        self.assertIsNone(self.store.read_retained_testing_generation(self.operation.operation_id))
        self.assertEqual(self.apply()[0], "written")

    def test_live_executor_and_new_journal_never_retire(self) -> None:
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as held:
            self.assertIsNotNone(held)
            with self.assertRaises(ValueError):
                self.apply()
        journal = self.operation.recovery_journal
        assert journal is not None
        newer = self.operation.model_copy(
            update={
                "recovery_journal": journal.model_copy(
                    update={
                        "target_fingerprint": FRESH_TESTING_INTENT_BINDING_PREFIX
                        + self.plan.plan_sha256
                    }
                )
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(newer)
        with self.assertRaises(ValueError):
            retirement_snapshot(self.store, self.product, self.body)
        self.assertIsNone(self.store.read_retained_testing_generation(self.operation.operation_id))

    def test_scope_completed_generation_hold_and_alias_refuse(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        protected = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={"staff_testing_hold": DokployTargetStaffTestingHold(reason="Staff use")}
                )
            }
        )
        self.store.write_dokploy_target_record(protected)
        with self.assertRaises(ValueError):
            retirement_snapshot(self.store, self.product, self.body)
        self.store.write_dokploy_target_record(target)
        identity = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            identity.model_copy(update={"context": "another", "instance": "prod"})
        )
        with self.assertRaises(ValueError):
            retirement_snapshot(self.store, self.product, self.body)
        self.store.delete_dokploy_target_id_record(
            expected_record=identity.model_copy(update={"context": "another", "instance": "prod"})
        )
        for changes in ({"context": "another"}, {"operation_id": "missing"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                retirement_snapshot(self.store, self.product, self.body.model_copy(update=changes))
        completed = self.gen.model_copy(update={"completed_at": stamp()})
        self.store.write_fresh_odoo_testing_generation(completed)
        with self.assertRaises(ValueError):
            retirement_snapshot(self.store, self.product, self.body)
        self.assertIsNone(self.store.read_retained_testing_generation(self.operation.operation_id))

    def test_new_override_intent_stales_the_review(self) -> None:
        override = self.store.read_odoo_instance_override_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_odoo_instance_override_record(
            override.model_copy(update={"apply_on": ("manual",)})
        )
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(self.product, self.context), self.gen
        )

    def test_http_dry_run_and_scoped_refusals_do_not_mutate_custody(self) -> None:
        import asyncio
        from tests.support.http import request
        from tests.support.auth import scoped_agent_transport

        async def post(**changes: object) -> Any:
            return await request(
                self.fixture.fixture.app,
                "POST",
                ROUTE.format(product=self.product),
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "http-original-key",
                },
                payload={**self.body.model_dump(mode="json"), **changes},
            )

        response = asyncio.run(post())
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"], self.plan.model_dump(mode="json"))
        with scoped_agent_transport():
            self.assertEqual(asyncio.run(post()).status_code, 403)
        for changes in ({"instance": "prod"}, {"context": "another"}, {"operation_id": "missing"}):
            with self.subTest(changes=changes):
                self.assertGreaterEqual(asyncio.run(post(**changes)).status_code, 400)
        if self.store.database_dialect_name == "sqlite":
            self.assertEqual(
                asyncio.run(
                    post(mode="apply", reviewed_plan_sha256=self.plan.plan_sha256)
                ).status_code,
                503,
            )
        else:
            receipt = asyncio.run(post(mode="apply", reviewed_plan_sha256=self.plan.plan_sha256))
            self.assertEqual(receipt.status_code, 202, receipt.text)
            self.assertEqual(
                asyncio.run(post(mode="apply", reviewed_plan_sha256=self.plan.plan_sha256)).json()[
                    "result"
                ],
                receipt.json()["result"],
            )
            self.assertEqual(
                asyncio.run(post(mode="readback")).json()["result"], receipt.json()["result"]
            )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )

    def test_native_capture_receipt_stales_review_and_is_preserved_without_transfer(self) -> None:
        from control_plane.contracts.odoo_testing_source import (
            NativeTestingCopyCapture,
            NativeTestingCopyArtifact,
        )
        from control_plane.odoo_testing_restore import generation_binding
        from control_plane.odoo_testing_native_source import native_testing_source_authority
        from tests.test_odoo_testing_native_source import descriptor

        fixture = TestingGenerationRetirementTests()
        fixture.native_source = True
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        authority = native_testing_source_authority(
            fixture.store, profile=fixture.fixture.profile, testing_context=fixture.context
        )
        observed = descriptor().model_copy(update=authority.model_dump())
        capture = NativeTestingCopyCapture(
            product=fixture.product,
            context=fixture.context,
            generation_binding_sha256=generation_binding(fixture.gen),
            source_binding_sha256=fixture.gen.source_binding_sha256,
            capture_id="c" * 32,
            database=NativeTestingCopyArtifact(
                sha256=canonical_json_sha256({"fixture": "database"}), size=1
            ),
            filestore=NativeTestingCopyArtifact(
                sha256=canonical_json_sha256({"fixture": "filestore"}), size=1
            ),
            captured_at=stamp(),
            source_observation=observed,
        )
        fixture.store.write_native_testing_copy_capture(capture)
        with self.assertRaises(ValueError):
            fixture.apply()
        plan = retirement_snapshot(fixture.store, fixture.product, fixture.body).plan
        fixture.apply_body = fixture.body.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )
        fixture.mutation = replace(
            fixture.mutation,
            request_fingerprint=canonical_json_sha256(fixture.apply_body.model_dump(mode="json")),
            response_payload={
                "result": plan.model_copy(update={"applied": True}).model_dump(mode="json")
            },
        )
        with (
            patch(
                "control_plane.odoo_testing_native_source.capture_native_testing_source"
            ) as recapture,
            patch(
                "control_plane.odoo_testing_native_source.remove_completed_native_capture_cache"
            ) as cleanup,
        ):
            self.assertEqual(fixture.apply()[0], "written")
            recapture.assert_not_called()
            cleanup.assert_not_called()
        retained = fixture.store.read_retained_testing_generation(fixture.operation.operation_id)
        assert retained is not None
        self.assertEqual(retained.capture, capture)
        self.assertEqual(
            fixture.store.read_native_testing_copy_capture(generation_binding(fixture.gen)), capture
        )

    def test_original_target_can_keep_its_host_while_metadata_changes_but_rebinding_refuses(
        self,
    ) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
        from control_plane.storage.product_authority_bundle import ProductAuthorityBundle

        self.apply()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        held = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            reason="Staff protection on retained target"
                        )
                    }
                )
            }
        )
        self.store.write_dokploy_target_record(held)
        self.store.write_product_authority_bundle(ProductAuthorityBundle(dokploy_targets=(held,)))
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            held,
        )
        identity = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        different = identity.model_copy(update={"target_id": "unrelated-recipient"})
        with self.assertRaises(ValueError):
            self.store.write_dokploy_target_id_record(different)
        with self.assertRaises(ValueError):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(dokploy_targets=(held,), dokploy_target_ids=(different,))
            )
        with self.assertRaises(ValueError):
            self.store.write_dokploy_target_record(held.model_copy(update={"context": "unrelated"}))
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ),
            identity,
        )
        self.assertIn(self.gen.domain_host, self.store.retained_disposable_hostnames())
