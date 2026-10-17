import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import Any, cast
from collections.abc import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_routes.preview_reservations import register_preview_reservation_read_routes
from control_plane.http_routes.support import ReadRouteDependencies, ApiRouteRegistrar
from control_plane.odoo_preview_apply_execution import (
    OdooPreviewApplyOperation,
    prepare_odoo_preview_apply_operation,
    preview_execution_lock_name,
)
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope, ODOO_PREVIEW_APPLY_ROUTE
from control_plane.preview_reservation_recovery import (
    classify_preview_reservation,
    held_preview_reservations,
    original_preview_intent,
    recover_held_preview,
)
from control_plane.product_reconcile import (
    ReconcileOutcome,
    reconcile_reservation_scope,
    reconcile_preview_target,
    PreviewProviderHooks,
)
from control_plane.service_auth import LocalAdminIdentity
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneIdempotencyRow,
    ORIGINAL_MUTATION_INTENT_ROUTE,
)
from tests.support.http import get
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload


class PreviewReservationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        self.request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        self.issued = _issued_plan(self.request)
        assert self.issued.plan_provenance is not None
        self.operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=self.request,
            issued_plan=self.issued,
            reservation_scope=reconcile_reservation_scope(self.profile.product),
            idempotency_key=self.issued.plan_provenance.plan_id,
            request_fingerprint="original",
            trace_id="initial",
            execute_apply=Mock(),
        )
        self.reservation = self.store.reserve_mutation(
            scope=self.operation.reservation_scope,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=self.operation.idempotency_key,
            request_fingerprint="original",
            lease_owner="old-executor",
            reconciliation_key=self.operation.adapter.reconciliation_key(),
            provider_target_key=self.operation.adapter.target_key(),
            original_intent=self.operation.adapter.original_intent(),
        ).record
        assert self.reservation is not None
        self.reservation = self.replace_record(
            lease_expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            updated_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            created_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        )

    def database_url(self) -> str:
        return _sqlite_database_url(self.root / "state.sqlite3")

    def replace_record(self, **changes: object) -> LaunchplaneIdempotencyRecord:
        with self.store._session_factory() as session:
            row = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.record_id == self.reservation.record_id
                )
            )
            assert row is not None
            current = LaunchplaneIdempotencyRecord.model_validate(row.payload)
            record = LaunchplaneIdempotencyRecord.model_validate(
                {**current.model_dump(), **changes}
            )
            self.store._sync_idempotency_row(row, record)
            session.commit()
            return record

    def recover(self, **kwargs: object) -> dict[str, object]:
        return recover_held_preview(
            store=self.store,
            reservation=self.reservation,
            profile=self.profile,
            root=self.root,
            observe=cast(
                Callable[..., tuple[str, dict[str, object] | None, bool]],
                kwargs.get("observe", Mock(return_value=("unknown", None, False))),
            ),
            quiescence=cast(Callable[..., bool], kwargs.get("quiescence", Mock(return_value=True))),
        )

    def test_retained_intent_excludes_ignored_caller_environment_values(self) -> None:
        private_request = self.request.model_copy(
            update={
                "apply": self.request.apply.model_copy(
                    update={"environment_values": {"PASSWORD": "must-not-retain"}}
                )
            }
        )
        operation = prepare_odoo_preview_apply_operation(
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=private_request,
            issued_plan=self.issued,
            reservation_scope=self.reservation.scope,
            idempotency_key=self.reservation.idempotency_key,
            request_fingerprint="original",
            trace_id="test",
        )
        self.assertNotIn("must-not-retain", json.dumps(operation.adapter.original_intent()))

    def test_created_compose_resolution_requires_complete_exact_original_inventory(self) -> None:
        from control_plane.workflows.odoo_preview_runtime import resolve_original_preview_compose_id
        import click

        plan = self.request.apply.dry_run_plan
        complete = [
            {
                "environments": [
                    {
                        "environmentId": plan.environment_id,
                        "composes": [{"composeId": "original-target", "name": plan.compose_name}],
                    }
                ]
            }
        ]
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://provider.example", "test"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request",
                return_value=complete,
            ),
        ):
            self.assertEqual(
                resolve_original_preview_compose_id(
                    control_plane_root=self.root, request=self.request.apply, database_url=None
                ),
                "original-target",
            )
        inventory: object
        for inventory in (
            [None],
            [{"environments": [{}]}],
            [{"environments": [{"environmentId": plan.environment_id}]}],
            [
                {
                    "environments": [
                        {
                            "environmentId": plan.environment_id,
                            "composes": [
                                {"composeId": "one", "name": plan.compose_name},
                                {"composeId": "two", "name": plan.compose_name},
                            ],
                        }
                    ]
                }
            ],
        ):
            with (
                self.subTest(inventory=inventory),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.example", "test"),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request",
                    return_value=inventory,
                ),
            ):
                with self.assertRaises(click.ClickException):
                    resolve_original_preview_compose_id(
                        control_plane_root=self.root, request=self.request.apply, database_url=None
                    )

    def test_original_survives_overwritten_intent_keys_and_absent_preview_records(self) -> None:
        self.assertEqual(self.store.list_preview_records(), ())
        result = self.store.reserve_mutation(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
            request_fingerprint="original",
            lease_owner="new",
            reconciliation_key=self.reservation.reconciliation_key,
            provider_target_key=self.reservation.provider_target_key,
            original_intent={"issued_plan": "overwritten"},
        )
        self.assertEqual(
            self.store.read_original_mutation_intent(result.record),
            self.store.read_original_mutation_intent(self.reservation),
        )
        original = original_preview_intent(result.record, profile=self.profile, store=self.store)
        self.assertEqual(original.issued_plan, self.issued)
        holds = held_preview_reservations(self.store, product=self.profile.product)
        self.assertEqual(holds, (result.record,))
        self.assertEqual(held_preview_reservations(self.store, product="other"), ())

    def test_companion_receipt_and_reservation_commit_or_rollback_together(self) -> None:
        with patch.object(
            self.store,
            "_retain_original_mutation_intent",
            side_effect=RuntimeError("receipt failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.reserve_mutation(
                    scope="fresh-scope",
                    route_path=ODOO_PREVIEW_APPLY_ROUTE,
                    idempotency_key="fresh-key",
                    request_fingerprint="fresh-fingerprint",
                    lease_owner="fresh-executor",
                    original_intent={"product": self.profile.product},
                )
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope="fresh-scope",
                route_path=ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key="fresh-key",
            )
        )
        self.assertEqual(
            held_preview_reservations(self.store, product=self.profile.product), (self.reservation,)
        )

    def test_changed_companion_receipt_refuses_before_lifecycle_or_provider_writes(self) -> None:
        def observe(**_: object) -> tuple[str, dict[str, object], bool]:
            with self.store._session_factory() as session:
                row = session.scalar(
                    select(LaunchplaneIdempotencyRow).where(
                        LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                        LaunchplaneIdempotencyRow.idempotency_key == self.reservation.record_id,
                    )
                )
                assert row is not None
                receipt = LaunchplaneIdempotencyRecord.model_validate(row.payload)
                body = {**receipt.response_payload, "original_intent": {"product": "changed"}}
                changed = receipt.model_copy(update={"response_payload": body})
                self.store._sync_idempotency_row(row, changed)
                session.commit()
            return "present", {"status": "pass"}, True

        with patch(
            "control_plane.odoo_preview_apply_execution.execute_odoo_preview_apply_result"
        ) as write:
            result = self.recover(observe=observe)
        self.assertEqual(result["settlement"], "refused")
        self.assertEqual(self.store.list_preview_records(), ())
        write.assert_not_called()
        current = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert current is not None
        self.assertNotEqual(current.state, "completed")

    def test_expired_running_and_reconcile_required_observe_without_provider_writes(self) -> None:
        original = self.reservation
        for state in ("running", "reconcile_required"):
            with self.subTest(state=state):
                self.reservation = self.replace_record(**{**original.model_dump(), "state": state})
                observer = Mock(return_value=("present", {"status": "fail"}, False))
                with patch(
                    "control_plane.odoo_preview_apply_execution.execute_odoo_preview_apply_result"
                ) as write:
                    result = self.recover(observe=observer)
                self.assertEqual(result["settlement"], "adopted")
                observer.assert_called_once()
                write.assert_not_called()

    def remove_original_intent(self) -> None:
        with self.store._session_factory() as session:
            receipt = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.scope == self.reservation.scope,
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == self.reservation.record_id,
                )
            )
            assert receipt is not None
            session.delete(receipt)
            session.commit()

    def assert_legacy_preview_drain_passes_with_fence(self) -> None:
        from control_plane.preview_reservation_recovery import classify_preview_reservation
        from control_plane.service_deploy_drain import confirm_startup, read_status
        from tests.test_client_release_redeploy import _prepare, _replacement_env

        self.remove_original_intent()
        before = classify_preview_reservation(
            self.reservation, profile=self.profile, store=self.store
        )
        self.assertEqual(before["next_action"], "retain_fence_missing_original_proof")
        _, blockers, dispatch = _prepare(self.store)
        self.assertTrue(dispatch)
        self.assertFalse(blockers)
        details = cast(tuple[dict[str, object], ...], read_status(self.store)["operation_details"])
        self.assertTrue(details[0]["execution_excluded"])
        self.assertFalse(details[0]["blocks_drain"])
        self.assertEqual(
            held_preview_reservations(self.store, product=self.profile.product), (self.reservation,)
        )
        self.assertEqual(
            before,
            classify_preview_reservation(self.reservation, profile=self.profile, store=self.store),
        )
        observer, quiescence = Mock(), Mock()
        self.assertEqual(
            self.recover(observe=observer, quiescence=quiescence)["settlement"], "refused"
        )
        observer.assert_not_called()
        quiescence.assert_not_called()
        with patch.dict("os.environ", _replacement_env()):
            confirm_startup(self.store)
            fresh = self.store.reserve_mutation(
                scope=self.reservation.scope,
                route_path=self.reservation.route_path,
                idempotency_key="fresh-preview",
                request_fingerprint="fresh-preview",
                lease_owner="new-worker",
                reconciliation_key=self.reservation.reconciliation_key,
                provider_target_key=self.reservation.provider_target_key,
            )
            self.assertEqual(fresh.status, "target_busy")
        retained = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert retained is not None
        self.assertEqual(retained.state, "reconcile_required")

    def test_expired_legacy_running_preview_passes_drain_and_retains_fence(self) -> None:
        self.assert_legacy_preview_drain_passes_with_fence()

    def test_expired_legacy_reconcile_required_preview_passes_drain_and_retains_fence(self) -> None:
        self.reservation = self.replace_record(state="reconcile_required")
        self.assert_legacy_preview_drain_passes_with_fence()

    def test_preview_executor_lock_held_by_another_store_blocks_drain(self) -> None:
        from tests.test_client_release_redeploy import _prepare

        executor = PostgresRecordStore(database_url=self.store.database_url)
        self.addCleanup(executor.close)
        with executor.testing_replacement_execution_lock(
            preview_execution_lock_name(self.reservation.provider_target_key)
        ) as check:
            self.assertIsNotNone(check)
            for state in ("running", "reconcile_required"):
                with self.subTest(state=state):
                    self.reservation = self.replace_record(state=state)
                    _, blockers, dispatch = _prepare(self.store)
                    self.assertFalse(dispatch)
                    self.assertIn(self.reservation.record_id, blockers)
        _, blockers, dispatch = _prepare(self.store)
        self.assertTrue(dispatch)
        self.assertFalse(blockers)

    def test_active_preview_leases_block_drain_without_lock_probe(self) -> None:
        from tests.test_client_release_redeploy import _prepare

        with patch.object(self.store, "testing_replacement_execution_lock") as lock:
            for state in ("running", "reconcile_required"):
                with self.subTest(state=state):
                    self.reservation = self.replace_record(
                        state=state,
                        lease_expires_at=(
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat(),
                    )
                    _, blockers, dispatch = _prepare(self.store)
                    self.assertFalse(dispatch)
                    self.assertIn(self.reservation.record_id, blockers)
            lock.assert_not_called()

    def test_unavailable_preview_execution_lock_blocks_drain(self) -> None:
        from contextlib import nullcontext
        from tests.test_client_release_redeploy import _prepare

        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            for state in ("running", "reconcile_required"):
                with self.subTest(state=state):
                    self.reservation = self.replace_record(state=state)
                    _, blockers, dispatch = _prepare(self.store)
                    self.assertFalse(dispatch)
                    self.assertIn(self.reservation.record_id, blockers)

    def test_unbound_or_missing_expiry_preview_still_blocks_drain(self) -> None:
        from tests.test_client_release_redeploy import _prepare

        original = self.reservation.model_dump()
        for changes in (
            {"provider_target_key": ""},
            {"lease_expires_at": "", "state": "reconcile_required"},
        ):
            with self.subTest(changes=changes):
                self.reservation = self.replace_record(**{**original, **changes})
                _, blockers, dispatch = _prepare(self.store)
                self.assertFalse(dispatch)
                self.assertIn(self.reservation.record_id, blockers)

    def test_lost_preview_execution_lock_refuses_dispatch(self) -> None:
        from contextlib import nullcontext
        from tests.test_client_release_redeploy import _prepare

        with patch.object(
            self.store,
            "testing_replacement_execution_lock",
            return_value=nullcontext(Mock(side_effect=RuntimeError("lock lost"))),
        ):
            with self.assertRaises(RuntimeError):
                _prepare(self.store)
        self.assertEqual(
            held_preview_reservations(self.store, product=self.profile.product), (self.reservation,)
        )

    def test_legacy_missing_intent_refuses_without_provider_reads_or_writes(self) -> None:
        self.remove_original_intent()
        observer, quiescence = Mock(), Mock()
        result = self.recover(observe=observer, quiescence=quiescence)
        self.assertEqual(result["settlement"], "refused")
        self.assertFalse(result["original_binding_available"])
        observer.assert_not_called()
        quiescence.assert_not_called()

    def test_missing_original_without_current_preview_lifecycle_stays_fenced(
        self,
    ) -> None:
        self.remove_original_intent()
        transport, observer, planner = Mock(), Mock(), Mock(side_effect=ValueError("fresh plan"))
        transport.get_json.return_value = {"state": "closed", "head": {"sha": "a" * 40}}
        result = reconcile_preview_target(
            record_store=self.store,
            profile=self.profile,
            repository_id="repo",
            transport=transport,
            pull_request_number=self.issued.plan_request.pr_number,
            control_plane_root=self.root,
            preview_hooks=PreviewProviderHooks(build_inputs=planner, observe_apply=observer),
        )
        self.assertEqual(result.plan["reason"], "held_preview_recovery_required")
        held = cast(list[dict[str, object]], result.plan["held_reservations"])[0]
        self.assertFalse(held["original_action_available"])
        self.assertFalse(held["original_plan_available"])
        self.assertEqual(held["target_pull_request_number"], self.issued.plan_request.pr_number)
        self.assertEqual(held["action"], "unavailable")
        self.assertEqual(held["pull_request_number"], 0)
        planner.assert_not_called()
        observer.assert_not_called()
        self.assertEqual(
            held_preview_reservations(self.store, product=self.profile.product), (self.reservation,)
        )

    def test_legacy_target_attribution_requires_exact_current_context_name_and_hash(self) -> None:
        self.remove_original_intent()
        for candidate in (
            self.reservation.model_copy(update={"provider_target_key": "different"}),
            self.reservation.model_copy(update={"reconciliation_key": "different"}),
        ):
            with self.subTest(candidate=candidate):
                classified = classify_preview_reservation(
                    candidate, profile=self.profile, store=self.store
                )
                self.assertEqual(classified["target_pull_request_number"], 0)
                self.assertFalse(classified["original_action_available"])
        changed = self.profile.model_copy(
            update={"preview": self.profile.preview.model_copy(update={"context": "different"})}
        )

        self.assertEqual(
            classify_preview_reservation(self.reservation, profile=changed, store=self.store)[
                "target_pull_request_number"
            ],
            0,
        )

    def prepare_cleanup(self, **facts: str) -> None:
        from control_plane.contracts.preview_record import PreviewRecord
        from control_plane.launchplane_mutations import resolve_launchplane_preview_id

        preview = PreviewRecord(
            preview_id=resolve_launchplane_preview_id(
                context=self.profile.preview.context,
                anchor_repo=self.profile.repository.split("/")[1],
                anchor_pr_number=42,
            ),
            context=self.profile.preview.context,
            anchor_repo=self.profile.repository.split("/")[1],
            anchor_pr_number=42,
            anchor_pr_url=f"https://github.com/{self.profile.repository}/pull/42",
            preview_label="preview",
            canonical_url=self.issued.preview_url,
            state="active",
            created_at=self.profile.updated_at,
            updated_at=self.profile.updated_at,
            eligible_at=self.profile.updated_at,
        )
        self.store.write_preview_record(preview)
        from control_plane.contracts.preview_generation_record import (
            PreviewGenerationRecord,
            PreviewPullRequestSummary,
        )

        generation = PreviewGenerationRecord(
            generation_id=preview.preview_id + "-generation",
            preview_id=preview.preview_id,
            sequence=1,
            state="ready",
            requested_reason="test",
            requested_at=preview.created_at,
            resolved_manifest_fingerprint="test-manifest",
            anchor_summary=PreviewPullRequestSummary(
                repo=preview.anchor_repo,
                pr_number=42,
                head_sha="a" * 40,
                pr_url=preview.anchor_pr_url,
            ),
        )
        self.store.write_preview_generation_record(generation)
        preview = preview.model_copy(update={"serving_generation_id": generation.generation_id})
        self.store.write_preview_record(preview)
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _RejectingVerifier, _local_operator_bearer_config
        from tests.support.http import request as http_request

        policy = LaunchplaneAuthzPolicy()
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="preview-cleanup-fixture-policy",
                source="test",
                updated_at=self.profile.updated_at,
                policy=policy,
            )
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        payload = {
            "mode": "inspect",
            "product": self.profile.product,
            "context": preview.context,
            "instance": "pr-42",
            "preview_id": preview.preview_id,
        }

        def observe() -> Any:
            return asyncio.run(
                http_request(
                    app,
                    "POST",
                    "/v1/disposable-lanes/observations",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=payload,
                )
            )

        payload["mode"] = "clear"
        cleared = observe()
        self.assertEqual(cleared.status_code, 202, cleared.text)
        payload["mode"] = "inspect"
        inspected = observe()
        self.assertEqual(inspected.status_code, 202, inspected.text)
        payload.update(
            {
                "mode": "record",
                "expected_binding_sha256": inspected.json()["result"]["binding_sha256"],
                "usage": "unused",
                "data": "reproducible",
                "production_resources": "isolated",
                "usage_evidence": "fixture:occupancy",
                "data_evidence": "fixture:copy",
                "isolation_evidence": "fixture:isolated",
                **facts,
            }
        )
        recorded = observe()
        self.assertEqual(recorded.status_code, 202, recorded.text)

    def cleanup(
        self, *, previous_plan: dict[str, object] | None = None, **hooks: object
    ) -> ReconcileOutcome:
        from tests.test_product_reconcile import FakePreviewProvider

        self.transport = Mock()
        self.transport.get_json.return_value = {"state": "closed", "head": {"sha": "a" * 40}}
        self.provider = FakePreviewProvider()
        provider_hooks = self.provider.hooks()
        provider_hooks = type(provider_hooks)(**{**provider_hooks.__dict__, **hooks})
        with patch(
            "control_plane.product_reconcile.launchplane_reconcile_preview_destination_allowed",
            return_value=True,
        ):
            return reconcile_preview_target(
                record_store=self.store,
                profile=self.profile,
                repository_id="repo",
                transport=self.transport,
                pull_request_number=42,
                control_plane_root=self.root,
                preview_hooks=provider_hooks,
                previous_plan=previous_plan,
            )

    def test_closed_disposable_preview_is_destroyed_without_original_intent(self) -> None:
        self.remove_original_intent()
        self.prepare_cleanup()
        result = self.cleanup()
        self.assertFalse(result.error, result.plan)
        self.assertEqual(self.provider.applied, [("destroy", 42)])
        self.assertEqual(held_preview_reservations(self.store, product=self.profile.product), ())
        preview = self.store.list_preview_records()[0]
        self.assertEqual(preview.state, "destroyed")
        self.assertTrue(preview.destroyed_at)
        self.assertFalse(preview.serving_generation_id)
        old = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert old is not None
        self.assertNotEqual(old.response_payload["result"]["status"], "pass")

    def test_cleanup_holds_name_protected_and_unknown_conditions(self) -> None:
        self.remove_original_intent()
        for condition, value in (
            ("usage", "in_use"),
            ("usage", "unknown"),
            ("data", "only_copy"),
            ("data", "unknown"),
            ("production_resources", "shared"),
            ("production_resources", "unknown"),
        ):
            with self.subTest(condition=condition, value=value):
                self.prepare_cleanup(**{condition: value})
                result = self.cleanup()
                self.assertTrue(result.plan["held"])
                self.assertIn(value, str(result.plan["reason"]))
                self.assertFalse(self.provider.applied)
                self.assertEqual(self.store.list_preview_records()[0].state, "active")
                self.assertTrue(held_preview_reservations(self.store, product=self.profile.product))

    def test_cleanup_names_invalid_retained_intent_without_spending_retry_budget(self) -> None:
        self.prepare_cleanup()
        with self.store._session_factory() as session:
            row = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == self.reservation.record_id,
                )
            )
            assert row is not None
            receipt = LaunchplaneIdempotencyRecord.model_validate(row.payload)
            intent = self.store.read_original_mutation_intent(self.reservation)
            changed = receipt.model_copy(
                update={
                    "response_payload": {
                        **receipt.response_payload,
                        "original_intent": {**intent, "product": "different"},
                    }
                }
            )
            self.store._sync_idempotency_row(row, changed)
            session.commit()
        result = self.cleanup()
        self.assertEqual(result.plan["reason"], "preview_cleanup_original_intent_binding_unknown")
        self.assertTrue(result.plan["held"])
        self.assertFalse(result.error)
        self.assertFalse(self.provider.applied)
        self.assertFalse(result.plan["destroy_failed_attempts"])
        self.assertTrue(held_preview_reservations(self.store, product=self.profile.product))

    def test_disposable_cleanup_preserves_active_executor_and_unknown_provider_work(self) -> None:
        self.remove_original_intent()
        self.prepare_cleanup()
        with self.store.testing_replacement_execution_lock(
            preview_execution_lock_name(self.reservation.provider_target_key)
        ):
            result = self.cleanup()
        self.assertTrue(result.deferred)
        self.assertFalse(self.provider.applied)
        result = self.cleanup(quiescence_check=lambda **_: False)
        self.assertTrue(result.deferred)
        self.assertFalse(self.provider.applied)
        self.assertEqual(self.store.list_preview_records()[0].state, "active")
        self.assertTrue(held_preview_reservations(self.store, product=self.profile.product))

    def test_disposable_cleanup_preserves_active_lease(self) -> None:
        self.remove_original_intent()
        self.prepare_cleanup()
        self.reservation = self.replace_record(
            lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        )
        result = self.cleanup()
        self.assertTrue(result.deferred)
        self.assertFalse(self.provider.applied)
        self.assertEqual(
            held_preview_reservations(self.store, product=self.profile.product), (self.reservation,)
        )

    def test_cleanup_hold_preserves_failed_destroy_budget(self) -> None:
        from tests.test_product_reconcile import FakePreviewProvider
        from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsResult

        self.remove_original_intent()
        self.prepare_cleanup()

        def fail(**kwargs: object) -> dict[str, object]:
            result = FakePreviewProvider().execute_apply(
                issued_plan=cast(OdooPreviewApplyInputsResult, kwargs["issued_plan"])
            )
            result["status"] = "fail"
            return result

        first = self.cleanup(execute_apply=fail)
        self.assertTrue(first.error)
        self.prepare_cleanup(usage="unknown")
        held = self.cleanup(previous_plan=first.plan, execute_apply=fail)
        self.assertTrue(held.plan["held"])
        self.assertEqual(
            held.plan["destroy_failed_attempts"], first.plan["destroy_failed_attempts"]
        )
        self.prepare_cleanup()
        resumed = self.cleanup(previous_plan=held.plan, execute_apply=fail)
        self.assertEqual(
            resumed.plan["destroy_failed_attempts"],
            cast(int, first.plan["destroy_failed_attempts"]) + 1,
        )

    def test_expired_safety_does_not_block_observation_and_destroyed_lifecycle(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError
        from tests.test_product_reconcile import FakePreviewProvider

        self.remove_original_intent()
        self.prepare_cleanup()
        first = self.cleanup(
            execute_apply=Mock(side_effect=ProviderMutationUnknownError("lost response"))
        )
        self.assertTrue(first.error)
        held = held_preview_reservations(self.store, product=self.profile.product)[0]
        intent = original_preview_intent(held, profile=self.profile, store=self.store)
        safety = self.store.read_disposable_lane_safety(
            self.profile.product, self.profile.preview.context, "pr-42"
        )
        assert safety is not None
        self.store.write_disposable_lane_safety(
            safety.model_copy(
                update={
                    "observed_at": datetime.now(timezone.utc) - timedelta(minutes=20),
                    "expires_at": datetime.now(timezone.utc) - timedelta(minutes=10),
                }
            )
        )
        self.reservation = held
        self.reservation = self.replace_record(
            lease_expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        )
        observed_result = FakePreviewProvider().execute_apply(issued_plan=intent.issued_plan)
        observed = self.cleanup(
            observe_apply=Mock(return_value=("present", observed_result, False))
        )
        self.assertFalse(observed.error, observed.plan)
        self.assertEqual(self.store.list_preview_records()[0].state, "destroyed")

    def test_legacy_missing_original_does_not_fence_an_unrelated_preview(self) -> None:
        self.remove_original_intent()
        transport = Mock()
        transport.get_json.return_value = {"state": "closed", "head": {"sha": "a" * 40}}
        result = reconcile_preview_target(
            record_store=self.store,
            profile=self.profile,
            repository_id="repo",
            transport=transport,
            pull_request_number=self.issued.plan_request.pr_number + 1,
            control_plane_root=self.root,
            preview_hooks=PreviewProviderHooks(),
        )
        self.assertFalse(result.plan["held"])
        self.assertFalse(result.error)
        self.assertEqual(
            held_preview_reservations(self.store, product=self.profile.product), (self.reservation,)
        )

    def test_moved_binding_and_issued_plan_refuse(self) -> None:
        for field in ("reconciliation_key", "provider_target_key", "idempotency_key"):
            with self.subTest(field=field):
                original = self.reservation
                candidate = self.reservation.model_copy(update={field: "different"})
                with self.assertRaises(ValueError):
                    original_preview_intent(candidate, profile=self.profile, store=self.store)
                self.reservation = original

    def test_active_executor_grace_and_unknown_quiescence_refuse_before_observation(self) -> None:
        observer = Mock()
        with self.store.testing_replacement_execution_lock(
            preview_execution_lock_name(self.reservation.provider_target_key)
        ) as check:
            self.assertIsNotNone(check)
            self.assertEqual(self.recover(observe=observer)["reason"], "preview_executor_active")
        observer.assert_not_called()
        self.assertEqual(
            self.recover(observe=observer, quiescence=Mock(return_value=False))["reason"],
            "preview_quiescence_unproven",
        )
        observer.assert_not_called()
        self.reservation = self.replace_record(
            lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(self.recover(observe=observer)["reason"], "preview_grace_active")
        observer.assert_not_called()

    def test_stale_snapshots_before_and_after_provider_read_do_not_settle(self) -> None:
        stale = self.reservation
        current = self.replace_record(attempt=self.reservation.attempt + 1)
        self.assertEqual(self.recover()["reason"], "preview_reservation_changed")
        self.reservation = current

        def move(**_: object) -> bool:
            self.replace_record(attempt=current.attempt + 1)
            return True

        observer = Mock()
        self.assertEqual(
            self.recover(observe=observer, quiescence=move)["reason"], "preview_reservation_changed"
        )
        observer.assert_not_called()
        self.reservation = self.replace_record(attempt=stale.attempt)

        def move_during_observation(**_: object) -> tuple[str, dict[str, object], bool]:
            self.replace_record(attempt=self.reservation.attempt + 1)
            return "present", {"status": "fail"}, False

        self.assertEqual(self.recover(observe=move_during_observation)["settlement"], "refused")
        self.assertTrue(held_preview_reservations(self.store, product=self.profile.product))

    def test_reconcile_cannot_settle_original_when_provider_work_is_unknown(self) -> None:
        observer = Mock(return_value=("unknown", None, False))
        transport = Mock()
        transport.get_json.return_value = {"state": "closed", "head": {"sha": "a" * 40}}
        result = reconcile_preview_target(
            record_store=self.store,
            profile=self.profile,
            repository_id="repo",
            transport=transport,
            pull_request_number=42,
            control_plane_root=self.root,
            preview_hooks=PreviewProviderHooks(
                observe_apply=observer, quiescence_check=lambda **_: False
            ),
            previous_plan={"preview_operation_key": "overwritten", "action": "none"},
        )
        self.assertTrue(result.error)
        observer.assert_not_called()
        self.assertEqual(
            cast(list[dict[str, object]], result.plan["held_reservations"])[0]["reason"],
            "preview_quiescence_unproven",
        )

    def test_periodic_reconcile_requests_retained_original_without_desired_preview(self) -> None:
        from control_plane.product_reconcile import request_product_reconcile_sweep

        transport = Mock()
        transport.get_json.return_value = []
        with patch("control_plane.product_reconcile._has_repository_identity", return_value=True):
            request_product_reconcile_sweep(
                self.store, datetime.now(timezone.utc).isoformat(), lambda *_: transport
            )
        requests = self.store.list_product_reconcile_requests(product=self.profile.product)
        self.assertTrue(
            any(
                request.target_kind == "preview" and request.pull_request_number == 42
                for request in requests
            )
        )
        self.assertEqual(self.store.list_preview_records(), ())

    def test_disappeared_compose_can_use_only_retained_schedule_binding(self) -> None:
        from control_plane.dokploy.post_deploy import ComposeDataWorkflowBinding
        from control_plane.workflows.odoo_preview_runtime import odoo_preview_target_is_quiescent

        request = self.request.apply.model_copy(
            update={
                "dry_run_plan": self.request.apply.dry_run_plan.model_copy(
                    update={"compose_ref": "exact-old-compose"}
                )
            }
        )
        binding = ComposeDataWorkflowBinding(
            schedule_type="server", lookup_id="old-server", app_name="original-data-workflow"
        )
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://provider.example", "test"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime._compose_exists_by_id",
                return_value=False,
            ),
            patch("control_plane.dokploy.post_deploy.api.dokploy_request", return_value=[]) as read,
        ):
            self.assertFalse(
                odoo_preview_target_is_quiescent(
                    control_plane_root=self.root, request=request, database_url=None
                )
            )
            self.assertTrue(
                odoo_preview_target_is_quiescent(
                    control_plane_root=self.root,
                    request=request,
                    database_url=None,
                    schedule_binding=binding,
                )
            )
        self.assertEqual(read.call_args.kwargs["query"]["id"], binding.lookup_id)

    def test_complete_schedule_and_deployment_quiescence_fails_closed(self) -> None:
        from control_plane.dokploy.post_deploy import (
            ComposeDataWorkflowBinding,
            bound_data_workflow_is_quiescent,
        )
        from control_plane.workflows.odoo_preview_runtime import odoo_preview_target_is_quiescent
        import click

        binding = ComposeDataWorkflowBinding(
            schedule_type="server", lookup_id="server", app_name="preview-data-workflow"
        )
        cases: tuple[object, ...] = (
            {},
            [None],
            [{"name": "data", "appName": binding.app_name}],
            [{"name": "data", "appName": binding.app_name, "deployments": [{"status": "running"}]}],
            [
                {
                    "name": "data",
                    "appName": binding.app_name,
                    "deployments": [{"status": "new-unknown-status"}],
                }
            ],
            [{"name": "data", "appName": binding.app_name, "deployments": [None]}],
        )
        for schedules in cases:
            with (
                self.subTest(schedules=schedules),
                patch(
                    "control_plane.dokploy.post_deploy.api.dokploy_request", return_value=schedules
                ),
            ):
                self.assertFalse(
                    bound_data_workflow_is_quiescent(
                        host="https://provider.example", token="test", binding=binding
                    )
                )
        with patch(
            "control_plane.dokploy.post_deploy.api.dokploy_request",
            return_value=[
                {"name": "data", "appName": binding.app_name, "deployments": [{"status": "done"}]},
                {
                    "name": "unrelated",
                    "appName": "other-preview",
                    "deployments": [{"status": "running"}],
                },
            ],
        ):
            self.assertTrue(
                bound_data_workflow_is_quiescent(
                    host="https://provider.example", token="test", binding=binding
                )
            )
        request = self.request.apply.model_copy(
            update={
                "dry_run_plan": self.request.apply.dry_run_plan.model_copy(
                    update={"compose_ref": "exact-compose"}
                )
            }
        )
        for deployments in (
            [{"status": "running"}],
            [{"status": "unknown"}],
            click.ClickException("incomplete"),
        ):
            with (
                self.subTest(deployments=deployments),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                    return_value=("https://provider.example", "test"),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime._compose_exists_by_id",
                    return_value=True,
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_api.list_deployments_for_target",
                    **(
                        {"side_effect": deployments}
                        if isinstance(deployments, Exception)
                        else {"return_value": deployments}
                    ),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_post_deploy.compose_data_workflow_is_quiescent"
                ) as schedules,
            ):
                self.assertFalse(
                    odoo_preview_target_is_quiescent(
                        control_plane_root=self.root, request=request, database_url=None
                    )
                )
                schedules.assert_not_called()

    def test_guarded_supersession_retains_both_intents_and_refuses_stale_or_active_proof(
        self,
    ) -> None:
        from control_plane.odoo_preview_apply_http import issue_odoo_preview_apply_plan

        def replacement(
            check: Callable[..., bool], key: str = "new-plan"
        ) -> tuple[OdooPreviewApplyOperation, Mock]:
            execute = Mock(return_value={"status": "fail", "provider_effect_attempted": True})
            issued = issue_odoo_preview_apply_plan(result=self.issued, plan_id=key)
            operation = prepare_odoo_preview_apply_operation(
                control_plane_root=self.root,
                record_store=self.store,
                profile=self.profile,
                apply_request=self.request,
                issued_plan=issued,
                reservation_scope=self.reservation.scope,
                idempotency_key=key,
                request_fingerprint="new",
                trace_id="new",
                execute_apply=execute,
                supersession_is_quiescent=check,
            )
            return operation, execute

        operation, execute = replacement(Mock(return_value=False))
        self.assertEqual(operation.run(self.store).status, "target_busy")
        execute.assert_not_called()
        held = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert held is not None
        self.reservation = held
        original = held

        def stale(**_: object) -> bool:
            self.replace_record(
                updated_at=(
                    datetime.fromisoformat(original.updated_at.replace("Z", "+00:00"))
                    + timedelta(seconds=1)
                ).isoformat()
            )
            return True

        operation, execute = replacement(stale)
        self.assertEqual(operation.run(self.store).status, "target_busy")
        execute.assert_not_called()
        self.reservation = self.replace_record(**original.model_dump())
        operation, execute = replacement(Mock(return_value=True))
        operation.run(self.store)
        execute.assert_called_once()
        completed = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
        )
        assert completed is not None
        self.assertEqual(completed.state, "completed")
        self.assertEqual(
            self.store.read_original_mutation_intent(completed),
            self.store.read_original_mutation_intent(original),
        )
        new = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=operation.idempotency_key,
        )
        assert new is not None
        self.assertEqual(
            self.store.read_original_mutation_intent(new)["issued_plan"]["plan_provenance"][
                "plan_id"
            ],
            operation.idempotency_key,
        )

    def test_scheduled_monitor_alerts_for_legacy_preview_without_original_intent(self) -> None:
        self.remove_original_intent()
        self.assert_scheduled_preview_alert(recover=False)

    def test_scheduled_monitor_resolves_preview_incident_after_supported_settlement(self) -> None:
        self.assert_scheduled_preview_alert(recover=True)

    def assert_scheduled_preview_alert(self, *, recover: bool) -> None:
        from control_plane.contracts.public_ingress_monitoring import (
            PublicIngressNotificationDestination,
            PublicIngressNotificationPolicyRecord,
        )
        from control_plane.workflows.public_ingress_monitor import (
            PublicIngressNotificationDriverSet,
            deliver_public_ingress_notification_outbox_delivery,
        )

        destination = PublicIngressNotificationDestination(
            destination_id="existing-discord",
            kind="discord",
            discord_webhook_secret="existing-channel",
        )
        self.store.write_public_ingress_notification_policy_record(
            PublicIngressNotificationPolicyRecord(
                policy_id="existing-notifications",
                destinations=(destination,),
                created_at=self.reservation.created_at,
                updated_at=self.reservation.updated_at,
                source="test",
            )
        )
        posts: list[dict[str, object]] = []
        drivers = PublicIngressNotificationDriverSet(
            discord_sender=lambda _url, payload: posts.append(payload),
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/webhook",
        )
        now = datetime.now(timezone.utc).isoformat()
        from control_plane.health_monitor_scheduler import HealthMonitorScheduler
        from control_plane.contracts.public_ingress_monitoring import (
            health_monitor_interval_seconds,
        )

        clock = datetime.fromisoformat(now).timestamp()
        scheduler = HealthMonitorScheduler(self.store, clock=lambda: clock)
        with patch(
            "control_plane.health_monitor_scheduler.public_ingress_notification_drivers",
            return_value=drivers,
        ):
            self.assertTrue(scheduler.run_once())
            self.assertFalse(scheduler.run_once())
        incidents = self.store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].failure_code, "deploy_fence_held")
        queued = self.store.list_outbox_delivery_records(
            states=("pending",), kind="public_ingress_notification"
        )
        self.assertTrue(queued)
        delivery = deliver_public_ingress_notification_outbox_delivery(
            record=queued[0],
            drivers=drivers,
            record_store=self.store,
            mark_provider_started=lambda _key, _provider: None,
        )
        self.assertEqual(delivery.state, "delivered")
        self.assertTrue(posts)
        for private in (
            self.reservation.scope,
            self.reservation.idempotency_key,
            self.reservation.reconciliation_key,
        ):
            self.assertNotIn(private, json.dumps(posts))
        if not recover:
            self.assertEqual(
                held_preview_reservations(self.store, product=self.profile.product),
                (self.reservation,),
            )
            return
        result = self.recover(observe=Mock(return_value=("present", {"status": "fail"}, False)))
        self.assertEqual(result["settlement"], "adopted")
        clock += health_monitor_interval_seconds("prod")
        with patch(
            "control_plane.health_monitor_scheduler.public_ingress_notification_drivers",
            return_value=drivers,
        ):
            self.assertTrue(scheduler.run_once())
        self.assertFalse(self.store.list_public_ingress_incident_records(status="open"))
        self.assertTrue(self.store.list_public_ingress_incident_records(status="resolved"))

    @patch.dict(
        "os.environ",
        {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "incident-test-key"},
        clear=True,
    )
    def test_preview_incident_reads_select_owned_lanes_and_preserve_resolved_history(self) -> None:
        from control_plane.http_routes.products import (
            ProductReadRouteDependencies,
            register_product_environment_read_routes,
        )
        from control_plane.workflows.public_ingress_monitor import run_public_ingress_monitor_once

        run_public_ingress_monitor_once(record_store=self.store, notify=False)
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        allowed = Mock(return_value=True)
        app = FastAPI()
        register_product_environment_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=ProductReadRouteDependencies(
                common=ReadRouteDependencies(
                    read_identity=lambda: LocalAdminIdentity(subject="admin", token_label="test"),
                    get_record_store=lambda: self.store,
                    next_trace_id=lambda: "trace",
                    authorization_allows=allowed,
                    http_error=lambda **kw: HTTPException(kw["status_code"], kw["code"]),
                    error_response_model=BaseModel,
                ),
                read_product_profile_list_identity=lambda: None,
                work_graph_planning_facts_provider=None,
                workflow_credentials_ready=lambda *_: False,
                control_plane_root=self.root,
                github_token=lambda **_: "",
            ),
        )
        path = f"/v1/products/{self.profile.product}/environments/{incident.instance}/public-ingress/incidents"
        for status in ("open", "resolved"):
            with (
                self.subTest(status=status),
                patch.object(
                    self.store,
                    "reserve_mutation",
                    side_effect=AssertionError("incident read mutated"),
                ),
            ):
                listed = asyncio.run(get(app, path))
                detail = asyncio.run(get(app, path + "/" + incident.incident_id))
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(
                listed.json()["incident_list"]["incidents"][0]["incident_id"], incident.incident_id
            )
            self.assertEqual(allowed.call_args.kwargs["action"], "preview.read")
            self.assertEqual(allowed.call_args.kwargs["context"], self.profile.preview.context)
            self.assertNotIn(self.reservation.reconciliation_key, json.dumps(detail.json()))
            if status == "open":
                self.assertEqual(
                    self.recover(observe=Mock(return_value=("present", {"status": "fail"}, False)))[
                        "settlement"
                    ],
                    "adopted",
                )
                run_public_ingress_monitor_once(record_store=self.store, notify=False)
        self.assertEqual(detail.json()["incident"]["incident"]["status"], "resolved")
        other = self.profile.model_copy(update={"product": "other-product"})
        self.store.write_product_profile_record(other)
        self.assertEqual(
            asyncio.run(get(app, path.replace(self.profile.product, other.product))).status_code,
            404,
        )
        self.assertEqual(
            asyncio.run(
                get(app, path.replace(incident.instance, "preview-mutation-reservation-unknown"))
            ).status_code,
            404,
        )
        allowed.return_value = False
        for url in (path, path + "/" + incident.incident_id):
            self.assertEqual(asyncio.run(get(app, url)).status_code, 403)

    def test_report_only_sweep_exposes_reservation_residue_without_provider_inventory(self) -> None:
        from control_plane.preview_lifecycle_cleanup_routes import (
            build_preview_lifecycle_sweep,
            PreviewLifecycleSweepEnvelope,
        )
        import click

        with patch(
            "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory",
            side_effect=click.ClickException("inventory unavailable"),
        ):
            result = build_preview_lifecycle_sweep(
                control_plane_root=self.root,
                record_store=self.store,
                request=PreviewLifecycleSweepEnvelope(apply=False),
                requested_profiles=(self.profile,),
            )
        profiles = cast(list[dict[str, object]], result["profiles"])
        holds = cast(list[dict[str, object]], profiles[0]["held_reservations"])
        self.assertEqual(holds[0]["reservation_id"], self.reservation.record_id)
        self.assertEqual(self.store.list_preview_records(), ())
        with patch(
            "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory"
        ) as inventory:
            refused = build_preview_lifecycle_sweep(
                control_plane_root=self.root,
                record_store=self.store,
                request=PreviewLifecycleSweepEnvelope(apply=True),
                requested_profiles=(self.profile,),
            )
        inventory.assert_not_called()
        entries = cast(list[dict[str, object]], refused["profiles"])
        self.assertEqual(entries[0]["status"], "blocked")

    def test_classification_http_is_bounded_redacted_and_does_not_mutate(self) -> None:
        app = FastAPI()
        allowed = Mock(return_value=True)
        register_preview_reservation_read_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: LocalAdminIdentity(subject="admin", token_label="test"),
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "trace",
                authorization_allows=allowed,
                http_error=lambda **kwargs: HTTPException(kwargs["status_code"], kwargs["code"]),
                error_response_model=BaseModel,
            ),
        )
        before = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        with patch.object(
            self.store, "reserve_mutation", side_effect=AssertionError("read mutated")
        ):
            response = asyncio.run(
                get(app, f"/v1/products/{self.profile.product}/preview-reservations?limit=1")
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reservations"][0]["attempt"], self.reservation.attempt)
        for private in (
            self.reservation.scope,
            self.reservation.idempotency_key,
            self.reservation.reconciliation_key,
            self.request.apply.dry_run_plan.compose_name,
        ):
            self.assertNotIn(private, json.dumps(response.json()))
        self.assertEqual(
            before,
            self.store.read_idempotency_record(
                scope=self.reservation.scope,
                route_path=self.reservation.route_path,
                idempotency_key=self.reservation.idempotency_key,
            ),
        )
        allowed.return_value = False
        self.assertEqual(
            asyncio.run(
                get(app, f"/v1/products/{self.profile.product}/preview-reservations")
            ).status_code,
            403,
        )
