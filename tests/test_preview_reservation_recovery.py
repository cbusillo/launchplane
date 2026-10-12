import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from typing import cast
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
    held_preview_reservations,
    original_preview_intent,
    recover_held_preview,
)
from control_plane.product_reconcile import (
    reconcile_reservation_scope,
    reconcile_preview_target,
    PreviewProviderHooks,
)
from control_plane.service_auth import LocalAdminIdentity
from control_plane.storage.postgres import PostgresRecordStore, LaunchplaneIdempotencyRow
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
        self.assertEqual(result.record.original_intent, self.reservation.original_intent)
        original = original_preview_intent(result.record, profile=self.profile)
        self.assertEqual(original.issued_plan, self.issued)
        holds = held_preview_reservations(self.store, product=self.profile.product)
        self.assertEqual(holds, (result.record,))
        self.assertEqual(held_preview_reservations(self.store, product="other"), ())

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

    def test_legacy_missing_intent_refuses_without_provider_reads_or_writes(self) -> None:
        self.reservation = self.replace_record(original_intent={})
        observer, quiescence = Mock(), Mock()
        result = self.recover(observe=observer, quiescence=quiescence)
        self.assertEqual(result["settlement"], "refused")
        self.assertFalse(result["original_binding_available"])
        observer.assert_not_called()
        quiescence.assert_not_called()

    def test_moved_binding_and_issued_plan_refuse(self) -> None:
        for field in ("reconciliation_key", "provider_target_key", "idempotency_key"):
            with self.subTest(field=field):
                original = self.reservation
                candidate = self.reservation.model_copy(update={field: "different"})
                with self.assertRaises(ValueError):
                    original_preview_intent(candidate, profile=self.profile)
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

    def test_reconcile_refuses_original_before_current_pr_or_target_discovery(self) -> None:
        observer = Mock(return_value=("unknown", None, False))
        transport = Mock()
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
        transport.get_json.assert_not_called()
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
        self.assertEqual(completed.original_intent, original.original_intent)
        new = self.store.read_idempotency_record(
            scope=original.scope,
            route_path=original.route_path,
            idempotency_key=operation.idempotency_key,
        )
        assert new is not None
        self.assertEqual(
            new.original_intent["issued_plan"]["plan_provenance"]["plan_id"],
            operation.idempotency_key,
        )

    def test_monitor_alerts_for_a_preview_without_desired_state_and_resolves_after_settlement(
        self,
    ) -> None:
        from control_plane.contracts.public_ingress_monitoring import (
            PublicIngressNotificationDestination,
            PublicIngressNotificationPolicyRecord,
        )
        from control_plane.workflows.public_ingress_monitor import (
            run_public_ingress_monitor_once,
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
        observed = run_public_ingress_monitor_once(
            record_store=self.store, checked_at=now, notification_drivers=drivers
        )
        self.assertEqual(observed.open_incident_count, 1)
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
        result = self.recover(observe=Mock(return_value=("present", {"status": "fail"}, False)))
        self.assertEqual(result["settlement"], "adopted")
        recovered = run_public_ingress_monitor_once(
            record_store=self.store,
            checked_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            notification_drivers=drivers,
        )
        self.assertEqual(recovered.resolved_incident_count, 1)

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
