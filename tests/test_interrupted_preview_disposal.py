from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from sqlalchemy import delete
import click

from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.contracts.preview_execution import odoo_preview_destroy_issued_at
from control_plane.preview_reservation_recovery import (
    interrupted_preview_restore as read_interrupted_preview_restore,
    original_preview_intent,
)
from control_plane.preview_filestore import PreviewRestoreDisposalRequired
from control_plane.contracts.preview_execution import preview_execution_lock_name
from control_plane.odoo_preview_apply_http import (
    issue_odoo_preview_apply_plan,
)
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.storage.postgres import LaunchplaneIdempotencyRow, ORIGINAL_MUTATION_INTENT_ROUTE
from tests.support.disposable_lanes import record_safety
from tests.test_product_reconcile import FakePreviewProvider, ProductReconcileTestCase, PR_HEAD


class InterruptedPreviewDisposalTests(ProductReconcileTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.advance_preview_recovery_grace = True
        profile = self.store.read_product_profile_record("site")
        self.profile = profile.model_copy(
            update={"preview": profile.preview.model_copy(update={"clean_testing_snapshot": True})}
        )
        self.store.write_product_profile_record(self.profile)
        self.clean_snapshot = CleanTestingSnapshot(
            product=self.profile.product,
            context=self.profile.preview.context,
            snapshot_id="a" * 64,
            server_id="fixture-server",
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            captured_at=datetime.now(timezone.utc).isoformat(),
            storage_budget=OdooTestingDrillStorageBudget(
                database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
            ),
        )
        self.native_interrupted = False
        build = self.provider.build_inputs
        execute = self.provider.execute_apply

        def inputs(**kwargs: Any) -> dict[str, object]:
            result = build(**kwargs)
            if kwargs["request"].operation == "refresh":
                plan = result["dry_run_plan"]
                assert isinstance(plan, dict)
                plan["clean_snapshot"] = self.clean_snapshot.model_dump(mode="json")
            return result

        def native(**kwargs: Any) -> bool:
            original = original_preview_intent(
                kwargs["reservation"], profile=self.profile, store=self.store
            )
            return self.native_interrupted and original.issued_plan.operation == "refresh"

        def apply(**kwargs: Any) -> dict[str, object]:
            operation = kwargs["issued_plan"].operation
            kwargs["provider_effect_checkpoint"](operation)
            if operation == "refresh" and not self.store.list_preview_records():
                self.native_interrupted = True
                raise ProviderMutationUnknownError("fixture interrupted restore")
            result = execute(**kwargs)
            if operation == "destroy":
                self.native_interrupted = False
            return result

        self.enterContext(patch.object(self.provider, "build_inputs", side_effect=inputs))
        self.enterContext(patch.object(self.provider, "execute_apply", side_effect=apply))
        self.enterContext(
            patch.object(self.provider, "observe_apply", return_value=("unknown", None, False))
        )
        self.native = self.enterContext(
            patch(
                "control_plane.preview_reservation_recovery.interrupted_preview_restore",
                side_effect=native,
            )
        )
        self.enterContext(
            patch.object(
                self.store, "serialize_existing_preview_refresh", return_value=nullcontext()
            )
        )
        self.github.add_run(50, PR_HEAD, event="pull_request")
        self.request("preview", 5)
        failed = self.run_once()
        self.assertEqual(failed.state, "failed")
        self.original = self.store.list_held_odoo_preview_reservations(product="site")[0]

    def test_reconciler_disposes_then_rebuilds_without_replaying_the_restore(self) -> None:
        self.request("preview", 5)
        disposed = self.reconcile()
        self.assertEqual(disposed["action"], "destroy")
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")
        original = self.store.read_idempotency_record_by_id(self.original.record_id)
        assert original is not None
        self.assertEqual(original.state, "completed")
        self.assertNotEqual(original.response_payload["result"]["status"], "pass")
        self.request("preview", 5)
        preview = self.store.list_preview_records()[0]
        after_destroy = datetime.fromisoformat(
            odoo_preview_destroy_issued_at(preview.destroy_reason).replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        with patch(
            "control_plane.product_reconcile.issue_odoo_preview_apply_plan",
            side_effect=lambda **kwargs: issue_odoo_preview_apply_plan(
                **kwargs, issued_at=after_destroy
            ),
        ):
            rebuilt = self.reconcile()
        self.assertEqual(rebuilt["action"], "apply")
        self.assertEqual(self.store.list_preview_records()[0].state, "active")
        self.assertEqual(self.provider.applied, [("destroy", 5), ("refresh", 5)])
        self.assertEqual(self.store.list_held_odoo_preview_reservations(product="site"), ())

    def test_protected_use_and_needed_data_refuse_before_disposal(self) -> None:
        for protection in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(protection=protection):
                record_safety(
                    self.store,
                    self.profile.product,
                    self.profile.preview.context,
                    "pr-5",
                    **protection,
                )
                self.request("preview", 5)
                result = self.run_once()
                self.assertTrue(result.last_plan["held"])
                self.assertEqual(result.last_plan["destroy_failed_attempts"], 0)
                self.assertFalse(self.provider.applied)
                self.assertTrue(self.native_interrupted)

    def test_unknown_native_custody_retains_original_without_disposal(self) -> None:
        self.native_interrupted = False
        self.request("preview", 5)
        result = self.run_once()
        self.assertTrue(result.last_error or result.last_plan.get("held"))
        self.assertFalse(self.provider.applied)
        held = self.store.list_held_odoo_preview_reservations(product="site")
        self.assertEqual([item.record_id for item in held], [self.original.record_id])

    def test_native_custody_is_rechecked_under_execution_before_disposal(self) -> None:
        self.native.side_effect = [True, False]
        self.request("preview", 5)
        result = self.run_once()
        self.assertTrue(result.last_plan["held"])
        self.assertFalse(self.provider.applied)

    def test_missing_original_proof_never_reaches_native_disposal(self) -> None:
        with self.store._session_factory() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == self.original.record_id,
                )
            )
            session.commit()
        self.native.reset_mock()
        self.request("preview", 5)
        result = self.run_once()
        self.assertTrue(result.last_plan["held"])
        self.assertFalse(self.provider.applied)
        self.native.assert_not_called()

    def test_active_executor_keeps_its_original_preview(self) -> None:
        self.native.reset_mock()
        with self.store.testing_replacement_execution_lock(
            preview_execution_lock_name(self.original.provider_target_key)
        ):
            self.request("preview", 5)
            result = self.run_once()
        self.assertTrue(result.last_plan["held"])
        self.assertFalse(self.provider.applied)
        self.native.assert_not_called()

    def test_native_reader_accepts_only_the_distinct_original_bound_refusal(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.resolve_original_preview_compose_id",
                return_value="fixture-original-target",
            ),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("fixture", "fixture"),
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                return_value={
                    "serverId": self.clean_snapshot.server_id,
                    "appName": "fixture-project",
                },
            ),
            patch("control_plane.preview_filestore.prepare_preview_filestore") as inspect,
        ):
            for failure, disposable in (
                (PreviewRestoreDisposalRequired(), True),
                (click.ClickException("fixture unknown custody"), False),
                (None, False),
            ):
                with self.subTest(failure=type(failure).__name__):
                    inspect.side_effect = failure
                    self.assertEqual(
                        read_interrupted_preview_restore(
                            store=self.store,
                            profile=self.profile,
                            reservation=self.original,
                            root=self.root,
                        ),
                        disposable,
                    )
                    self.assertEqual(inspect.call_args.kwargs["snapshot"], self.clean_snapshot)
                    self.assertFalse(inspect.call_args.kwargs["apply"])

    def test_new_needed_copy_protection_stops_the_first_destroy_effect(self) -> None:
        def protected(**kwargs: Any) -> dict[str, object]:
            record_safety(
                self.store,
                self.profile.product,
                self.profile.preview.context,
                "pr-5",
                data="only_copy",
            )
            kwargs["provider_effect_checkpoint"]("compose_destroy")
            self.fail("Needed data must prevent the provider effect")

        with patch.object(self.provider, "execute_apply", side_effect=protected):
            self.request("preview", 5)
            result = self.run_once()
        self.assertTrue(result.last_plan["held"])
        self.assertFalse(self.provider.applied)
        self.assertTrue(self.native_interrupted)

    def test_lost_destroy_reply_is_observed_before_rebuild_on_the_original_key(self) -> None:
        effects: list[str] = []

        def lost_reply(**kwargs: Any) -> dict[str, object]:
            kwargs["provider_effect_checkpoint"]("compose_destroy")
            effects.append("destroy")
            self.native_interrupted = False
            raise ProviderMutationUnknownError("fixture lost destroy reply")

        with patch.object(self.provider, "execute_apply", side_effect=lost_reply):
            self.request("preview", 5)
            failed = self.run_once()
        self.assertEqual(failed.state, "failed")
        self.assertFalse(self.store.list_preview_records())
        disposal = self.store.list_held_odoo_preview_reservations(product="site")[0]
        intent = original_preview_intent(disposal, profile=self.profile, store=self.store)
        result = FakePreviewProvider().execute_apply(issued_plan=intent.issued_plan)
        assert intent.issued_plan.plan_provenance is not None
        after_destroy = intent.issued_plan.plan_provenance.issued_at + timedelta(seconds=1)
        with (
            patch.object(self.provider, "observe_apply", return_value=("present", result, False)),
            patch(
                "control_plane.product_reconcile.issue_odoo_preview_apply_plan",
                side_effect=lambda **kwargs: issue_odoo_preview_apply_plan(
                    **kwargs, issued_at=after_destroy
                ),
            ),
        ):
            self.request("preview", 5)
            self.reconcile()
        self.assertEqual(effects, ["destroy"])
        original_disposal = self.store.read_idempotency_record_by_id(disposal.record_id)
        assert original_disposal is not None
        self.assertEqual(original_disposal.state, "completed")
        self.assertEqual(original_disposal.idempotency_key, disposal.idempotency_key)
        self.assertEqual(self.store.list_preview_records()[0].state, "active")
        self.assertEqual(self.provider.applied, [("refresh", 5)])
