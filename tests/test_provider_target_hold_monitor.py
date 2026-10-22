from dataclasses import replace
from pathlib import Path
from typing import Any
from tempfile import TemporaryDirectory
import logging
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import delete, select

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentMaterialFingerprint,
    public_ingress_material_fingerprint_sha256,
)
from control_plane.provider_target_holds import ProviderOperationReference
from control_plane.storage.postgres import (
    ORIGINAL_MUTATION_INTENT_ROUTE,
    LaunchplaneIdempotencyRow,
    LaunchplaneProviderTargetRow,
    PostgresRecordStore,
    PublicIngressTransitionWriteResult,
)
from control_plane.workflows.public_ingress_monitor import (
    DEPLOY_FENCE_CHECK_NAME,
    PublicIngressMonitorResult,
    check_public_ingress_target,
    discover_public_ingress_monitor_targets,
    run_public_ingress_monitor_once,
)
from tests.test_public_ingress_monitor import _hold_deploy_fence, _minutes_after, _profile


class ProviderTargetHoldMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'holds.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = _profile(lane=ProductLaneProfile(instance="prod", context="example-site"))
        self.store.write_product_profile_record(self.profile)
        self.origin = ProviderTargetOrigin(
            product=self.profile.product,
            context="example-site",
            instance="prod",
            provider_id="dokploy",
            target_type="compose",
            target_id="old-target",
        )

    def reserve(
        self,
        key: str,
        *,
        origin: ProviderTargetOrigin | None = None,
        now: str = "2026-10-01T00:00:00Z",
    ) -> LaunchplaneIdempotencyRecord:
        origin = origin or self.origin
        with patch.object(self.store, "_database_mutation_timestamp", return_value=now):
            return self.store.reserve_mutation(
                scope="private-scope",
                route_path="/private-operation",
                idempotency_key=key,
                request_fingerprint="private-fingerprint",
                lease_owner="fixture",
                lease_seconds=60,
                reconciliation_key="opaque-family-intent",
                provider_target_key=origin.target_key(),
                provider_target_origin=origin,
            ).record

    def observe(self, checked_at: str = "2026-10-01T01:00:00Z") -> PublicIngressMonitorResult:
        with patch.object(self.store, "_database_mutation_timestamp", return_value=checked_at):
            return run_public_ingress_monitor_once(
                record_store=self.store, checked_at=checked_at, notify=False
            )

    def complete(self, row: LaunchplaneIdempotencyRecord) -> None:
        self.store.write_idempotency_record(
            row.model_copy(
                update={
                    "state": "completed",
                    "response_status_code": 500,
                    "response_trace_id": "terminal-failure",
                    "recorded_at": _minutes_after(row.updated_at, 10),
                }
            )
        )

    def test_two_holds_counted_and_live_current_never_hides_old_stuck(self) -> None:
        old = self.reserve("private-old-key")
        live_origin = self.origin.model_copy(update={"target_id": "new-target"})
        live = self.reserve("private-live-key", origin=live_origin, now="2026-10-01T01:00:00Z")
        transition = self.store.write_public_ingress_transition_with_outbox

        def heartbeat_then_publish(**kwargs: Any) -> PublicIngressTransitionWriteResult:
            with patch.object(
                self.store, "_database_mutation_timestamp", return_value="2026-10-01T01:00:30Z"
            ):
                self.assertEqual(
                    self.store.renew_mutation_reservation(
                        reservation=live, lease_seconds=120
                    ).status,
                    "updated",
                )
                return transition(**kwargs)

        with patch.object(
            self.store,
            "write_public_ingress_transition_with_outbox",
            side_effect=heartbeat_then_publish,
        ):
            result = self.observe()
        self.assertEqual(result.fail_count, 1)
        self.assertEqual(result.open_incident_count, 1)
        snapshot = discover_public_ingress_monitor_targets(self.store)[0].provider_hold_snapshot
        assert snapshot is not None
        self.assertEqual(
            {hold.reservation.record_id for hold in snapshot.holds}, {old.record_id, live.record_id}
        )
        self.assertIn(old.record_id, result.records[0].summary)
        material = result.records[0].model_dump_json()
        for private in (
            old.scope,
            old.idempotency_key,
            live.idempotency_key,
            old.request_fingerprint,
            old.provider_target_key,
        ):
            self.assertNotIn(private, material)

    def test_acceptance_rechecks_classification_after_lease_change_or_elapsed_grace(self) -> None:
        from control_plane.workflows.public_ingress_monitor import DEPLOY_FENCE_RECONCILE_GRACE
        from control_plane.contracts.idempotency_record import (
            parse_launchplane_mutation_timestamp,
            format_launchplane_mutation_timestamp,
        )

        row = self.reserve("classification")
        target = discover_public_ingress_monitor_targets(self.store)[0]
        snapshot = target.provider_hold_snapshot
        assert snapshot is not None
        observed_at = row.lease_expires_at
        observation = check_public_ingress_target(
            target=target,
            checked_at=observed_at,
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        self.assertEqual(observation.status, "fail")
        renewed = row.model_copy(update={"lease_expires_at": _minutes_after(observed_at, 10)})
        with self.store._session_factory.begin() as session:
            stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
            assert stored is not None
            self.store._sync_idempotency_row(stored, renewed)
        with patch.object(self.store, "_database_mutation_timestamp", return_value=observed_at):
            result = self.store.write_public_ingress_transition_with_outbox(
                observation=observation, incidents=(), expected_provider_hold_snapshot=snapshot
            )
        self.assertEqual(result.status, "provider_source_changed")

        held = renewed.model_copy(
            update={
                "state": "reconcile_required",
                "updated_at": observed_at,
                "lease_expires_at": "",
            }
        )
        with self.store._session_factory.begin() as session:
            stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
            assert stored is not None
            self.store._sync_idempotency_row(stored, held)
        target = discover_public_ingress_monitor_targets(self.store)[0]
        observation = check_public_ingress_target(
            target=target,
            checked_at=observed_at,
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        self.assertEqual(observation.status, "pass")
        accepted_at = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(observed_at, field_name="observed_at")
            + DEPLOY_FENCE_RECONCILE_GRACE
        )
        with patch.object(self.store, "_database_mutation_timestamp", return_value=accepted_at):
            result = self.store.write_public_ingress_transition_with_outbox(
                observation=observation,
                incidents=(),
                expected_provider_hold_snapshot=target.provider_hold_snapshot,
            )
        self.assertEqual(result.status, "provider_source_changed")
        self.assertEqual(self.store.list_public_ingress_observation_records(), ())

    def test_corrupt_provider_source_keeps_other_probes_and_scheduler_running(self) -> None:
        from control_plane.health_monitor_scheduler import HealthMonitorScheduler
        from control_plane.contracts.private_health_endpoint_record import (
            PrivateHealthEndpointRecord,
        )
        from control_plane.contracts.product_profile_record import ProductLaneHealthCheck
        from control_plane.outbound_http import PublicTlsProbeResult
        from control_plane.storage.postgres import (
            LaunchplaneDokployTargetRow,
            LaunchplaneDokployTargetIdRow,
        )
        from control_plane.workflows.public_ingress_monitor import HttpObservation
        from tests.test_public_ingress_monitor import _route_binding, _tls_certificate

        logger = discover_public_ingress_monitor_targets.__module__
        row = self.reserve("corrupt")
        with self.assertNoLogs(logger, level="WARNING"):
            self.observe()
        profile = _profile()
        lane = profile.lanes[0]
        lane = lane.model_copy(
            update={
                "health_monitoring": lane.health_monitoring.model_copy(
                    update={
                        "checks": (
                            *lane.health_monitoring.checks,
                            ProductLaneHealthCheck(
                                name="private-runtime",
                                kind="private_http",
                                private_endpoint_key="fixture-private",
                            ),
                        )
                    }
                )
            }
        )
        self.store.write_product_profile_record(profile.model_copy(update={"lanes": (lane,)}))
        self.store.write_private_health_endpoint_record(
            PrivateHealthEndpointRecord(
                endpoint_key="fixture-private",
                product=profile.product,
                context=lane.context,
                instance=lane.instance,
                url="http://10.0.0.5:8080/health",
                updated_at=row.created_at,
            )
        )
        self.store.write_route_binding_record(_route_binding())
        corrupt_authority = (
            LaunchplaneProviderTargetRow(
                context="unrelated-source",
                instance="prod",
                provider_id="fixture",
                target_category="compose",
                target_id="fixture",
                display_name="fixture",
                provider_target_type="compose",
                updated_at=row.created_at,
                payload={},
            ),
            LaunchplaneDokployTargetRow(
                context="unrelated-source",
                instance="prod",
                updated_at=row.created_at,
                payload={},
            ),
            LaunchplaneDokployTargetIdRow(
                context="unrelated-source",
                instance="prod",
                target_id="fixture",
                updated_at=row.created_at,
                payload={},
            ),
        )
        for index, authority in enumerate((None, *corrupt_authority)):
            with self.subTest(source=type(authority).__name__):
                with self.store._session_factory.begin() as session:
                    if authority is None:
                        stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
                        assert stored is not None
                        stored.state = "reconcile_required"
                    else:
                        session.add(authority)
                try:
                    http = Mock(side_effect=lambda url, timeout: HttpObservation(200, url, 0))
                    private = Mock(side_effect=lambda url, timeout: HttpObservation(200, url, 0))
                    tls = Mock(
                        return_value=PublicTlsProbeResult(
                            status="valid",
                            failure_code=None,
                            summary="fixture certificate",
                            validated_address_count=1,
                            certificate=_tls_certificate(),
                        )
                    )
                    checked_at = _minutes_after("2026-10-01T01:00:00Z", index + 1)
                    with (
                        patch.object(
                            self.store, "_database_mutation_timestamp", return_value=checked_at
                        ),
                        self.assertLogs(logger, level="WARNING") as warnings,
                    ):
                        result = run_public_ingress_monitor_once(
                            record_store=self.store,
                            checked_at=checked_at,
                            notify=False,
                            http_get=http,
                            private_http_get=private,
                            tls_get=tls,
                        )
                    self.assertTrue(
                        all(record.levelno == logging.WARNING for record in warnings.records)
                    )
                    self.assertTrue(http.called)
                    self.assertTrue(private.called)
                    self.assertTrue(tls.called)
                    provider = [
                        record for record in result.records if record.check_kind == "provider"
                    ]
                    self.assertEqual([record.status for record in provider], ["skipped"])
                    self.assertEqual(result.resolved_incident_count, 0)
                    self.assertTrue(self.store.list_public_ingress_incident_records(status="open"))
                    persisted = self.store.list_public_ingress_observation_records()
                    self.assertTrue(
                        {"public_http", "private_http", "tls"}.issubset(
                            {
                                record.check_kind
                                for record in persisted
                                if record.observed_at == checked_at
                            }
                        )
                    )
                    if authority is None:
                        with self.assertRaises(ValueError):
                            self.store.read_provider_hold_attention_page()
                    with self.assertRaises(ValueError):
                        self.store.list_provider_target_holds()
                    probe = Mock()
                    scheduler = HealthMonitorScheduler(
                        self.store, run_monitor=probe, clock=lambda: 1_800_000_000 + index * 3600
                    )
                    self.assertTrue(scheduler.run_once())
                    self.assertTrue(probe.called)
                finally:
                    with self.store._session_factory.begin() as session:
                        if authority is None:
                            stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
                            assert stored is not None
                            self.store._sync_idempotency_row(stored, row)
                        else:
                            session.delete(session.merge(authority))

        private_failure = "private-intent-and-fingerprint-error-sentinel"
        with (
            patch.object(
                self.store, "read_provider_hold_snapshots", side_effect=ValueError(private_failure)
            ),
            self.assertLogs(logger, level="WARNING") as warnings,
        ):
            discover_public_ingress_monitor_targets(self.store)
        for record in warnings.records:
            self.assertEqual(record.levelno, logging.WARNING)
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)
            self.assertNotIn(private_failure, record.getMessage())

    def typed_operation(self, key: str, *, running: bool = False) -> Any:
        from datetime import datetime, timedelta, timezone
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementApplyRequest,
        )
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            OdooStableTargetReplacementOperationRecord,
            build_odoo_stable_target_replacement_operation_id,
        )
        from control_plane.launchplane_reconcile_authorization import (
            build_launchplane_reconcile_authorization,
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        created_at = now.isoformat().replace("+00:00", "Z")
        profile = self.profile.model_copy(
            update={"lanes": (ProductLaneProfile(context=self.origin.context, instance="testing"),)}
        )
        self.store.write_product_profile_record(profile)
        scope = "launchplane-reconcile:" + profile.product
        operation = OdooStableTargetReplacementOperationRecord(
            schema_version=2,
            operation_id=build_odoo_stable_target_replacement_operation_id(
                product=profile.product,
                context=self.origin.context,
                instance="testing",
                created_at=created_at,
                idempotency_key=key,
                idempotency_scope=scope,
            ),
            product=profile.product,
            context=self.origin.context,
            instance="testing",
            created_at=created_at,
            updated_at=created_at,
            idempotency_key=key,
            idempotency_scope=scope,
            request_fingerprint="PRIVATE-TYPED-FINGERPRINT",
            request=OdooStableTargetReplacementApplyRequest(
                product=profile.product,
                instance="testing",
                strategy="recreate-in-place",
                data_source_mode="existing",
            ),
            authorization=build_launchplane_reconcile_authorization(
                product=profile.product,
                context=self.origin.context,
                authorized_at=created_at,
            ),
            status="running" if running else "reconciliation_required",
            phase="apply",
            error_code="" if running else "operation_reconciliation_required",
            error_message="" if running else "Fixture requires reconciliation.",
            lease_owner="fixture" if running else "",
            lease_expires_at=(now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            if running
            else "",
            heartbeat_at=created_at if running else "",
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        return operation

    def test_typed_participant_terminal_failure_settles_exact_reference_and_mixed_generic(
        self,
    ) -> None:
        from control_plane.provider_target_holds import TestingReplacementReference

        operation = self.typed_operation("PRIVATE-TYPED-KEY")
        generic = self.reserve(
            "generic", origin=self.origin.model_copy(update={"instance": "testing"})
        )
        opened = self.observe(operation.updated_at)
        self.assertEqual(opened.open_incident_count, 1)
        fingerprint = opened.records[0].material_fingerprint
        assert fingerprint is not None
        self.assertIn(
            TestingReplacementReference.from_record(operation).encode(),
            fingerprint.route_authority.target_keys,
        )
        public = opened.records[0].model_dump_json()
        self.assertNotIn(operation.request_fingerprint, public)
        self.assertNotIn(operation.idempotency_scope, public)
        self.assertNotIn(operation.idempotency_key, public)
        self.complete(generic)
        self.assertEqual(self.observe(operation.updated_at).resolved_incident_count, 0)
        terminal = operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "finished_at": operation.updated_at,
                "error_code": "fixture_failed",
                "error_message": "Fixture terminal failure.",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(terminal)
        lane_key = (operation.product, operation.context, operation.instance)
        other_lane = (operation.product + "-other", operation.context, operation.instance)
        reference = TestingReplacementReference.from_record(terminal).encode()
        bulk = self.store.read_provider_hold_snapshots(
            lane_keys=(lane_key, other_lane), references_by_lane={other_lane: (reference,)}
        )
        self.assertEqual(
            bulk[lane_key],
            self.store.read_provider_hold_snapshot(
                product=operation.product, context=operation.context, instance=operation.instance
            ),
        )
        self.assertIn(reference, bulk[other_lane].unavailable_references)
        resolved = self.observe(operation.updated_at)
        self.assertEqual(resolved.resolved_incident_count, 1)
        self.assertEqual(resolved.pass_count, 1)

    def test_typed_missing_corrupt_mismatched_or_nonterminal_record_never_settles(self) -> None:
        from control_plane.storage.postgres import (
            LaunchplaneOdooStableTargetReplacementOperationRow,
        )

        for corruption in ("missing", "indexed-status", "created-at", "family", "pending"):
            with self.subTest(corruption=corruption):
                operation = self.typed_operation(corruption)
                self.observe(operation.updated_at)
                with self.store._session_factory.begin() as session:
                    row = session.get(
                        LaunchplaneOdooStableTargetReplacementOperationRow, operation.operation_id
                    )
                    assert row is not None
                    if corruption == "missing":
                        session.delete(row)
                    elif corruption == "indexed-status":
                        row.status = "pass"
                    elif corruption == "created-at":
                        row.payload = {**row.payload, "created_at": "changed-original-time"}
                    elif corruption == "family":
                        row.payload = {
                            **row.payload,
                            "request": {
                                **row.payload["request"],
                                "data_source_mode": "empty",
                                "allow_empty_data": True,
                            },
                        }
                    else:
                        self.store._sync_odoo_stable_target_replacement_operation_row(
                            row,
                            operation.model_copy(
                                update={
                                    "status": "pending",
                                    "error_code": "",
                                    "error_message": "",
                                }
                            ),
                        )
                observed = self.observe(operation.updated_at)
                self.assertEqual(observed.pass_count, 0)
                self.assertEqual(observed.resolved_incident_count, 0)
                self.assertEqual(
                    observed.skipped_count, int(corruption in {"indexed-status", "created-at"})
                )
                with self.store._session_factory.begin() as session:
                    session.execute(
                        delete(LaunchplaneOdooStableTargetReplacementOperationRow).where(
                            LaunchplaneOdooStableTargetReplacementOperationRow.operation_id
                            == operation.operation_id
                        )
                    )

    def test_typed_heartbeat_is_stable_but_new_membership_before_acceptance_is_rejected(
        self,
    ) -> None:
        operation = self.typed_operation("typed-live", running=True)
        self.assertEqual(self.observe(operation.updated_at).target_count, 0)
        self.reserve("generic-stuck", origin=self.origin.model_copy(update={"instance": "testing"}))
        transition = self.store.write_public_ingress_transition_with_outbox

        def renew_before_publish(**kwargs: Any) -> PublicIngressTransitionWriteResult:
            self.store.write_odoo_stable_target_replacement_operation_record(
                operation.model_copy(
                    update={
                        "updated_at": _minutes_after(operation.updated_at, 1),
                        "heartbeat_at": _minutes_after(operation.updated_at, 1),
                        "lease_expires_at": _minutes_after(operation.lease_expires_at, 1),
                    }
                )
            )
            return transition(**kwargs)

        with patch.object(
            self.store,
            "write_public_ingress_transition_with_outbox",
            side_effect=renew_before_publish,
        ):
            self.assertEqual(self.observe(operation.updated_at).open_incident_count, 1)
        self.assertTrue(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=operation.model_copy(
                    update={
                        "status": "fail",
                        "phase": "failed",
                        "finished_at": operation.updated_at,
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                        "error_code": "fixture_failed",
                        "error_message": "Fixture finished.",
                    }
                ),
                lease_owner=operation.lease_owner,
            )
        )
        self.complete(self.store.list_held_provider_target_reservations()[0])

        def add_before_publish(**kwargs: Any) -> PublicIngressTransitionWriteResult:
            self.typed_operation("new-typed-held")
            return transition(**kwargs)

        with patch.object(
            self.store,
            "write_public_ingress_transition_with_outbox",
            side_effect=add_before_publish,
        ):
            result = self.observe(operation.updated_at)
        self.assertEqual(result.pass_count, 0)
        self.assertEqual(result.resolved_incident_count, 0)
        self.assertEqual(result.skipped_count, 1)

    def test_typed_import_preserves_original_identity_and_terminal_record_and_completion_cas(
        self,
    ) -> None:
        from control_plane.storage.filesystem import FilesystemRecordStore

        operation = self.typed_operation("import", running=True)
        directory = self.enterContext(TemporaryDirectory())
        source = FilesystemRecordStore(state_dir=Path(directory))
        source.write_odoo_stable_target_replacement_operation_record(operation)
        self.store.import_core_records_from_filesystem(source)
        changed = operation.model_copy(update={"request_fingerprint": "different-original-request"})
        source.write_odoo_stable_target_replacement_operation_record(changed)
        with self.assertRaises(ValueError):
            self.store.import_core_records_from_filesystem(source)
        terminal = operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "finished_at": operation.updated_at,
                "error_code": "fixture_failed",
                "error_message": "Fixture failed.",
                "lease_owner": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
            }
        )
        self.assertFalse(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=terminal.model_copy(
                    update={"request_fingerprint": changed.request_fingerprint}
                ),
                lease_owner=operation.lease_owner,
            )
        )
        self.assertTrue(
            self.store.complete_odoo_stable_target_replacement_operation_record(
                record=terminal, lease_owner=operation.lease_owner
            )
        )
        terminal = terminal.model_copy(
            update={"error_message": "Updated historical failure evidence."}
        )
        source.write_odoo_stable_target_replacement_operation_record(terminal)
        self.store.import_core_records_from_filesystem(source)
        for changed_status in (
            operation,
            terminal.model_copy(
                update={
                    "status": "reconciliation_required",
                    "phase": "apply",
                    "finished_at": "",
                }
            ),
            terminal.model_copy(
                update={
                    "status": "pass",
                    "phase": "completed",
                    "error_code": "",
                    "error_message": "",
                }
            ),
        ):
            changed_status = type(operation).model_validate(changed_status.model_dump(mode="json"))
            source.write_odoo_stable_target_replacement_operation_record(changed_status)
            with self.assertRaises(ValueError):
                self.store.import_core_records_from_filesystem(source)
            self.assertEqual(
                self.store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                ),
                terminal,
            )
        source.write_odoo_stable_target_replacement_operation_record(
            terminal.model_copy(
                update={
                    "request": terminal.request.model_copy(
                        update={"reason": "different original intent"}
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            self.store.import_core_records_from_filesystem(source)
        source.write_odoo_stable_target_replacement_operation_record(terminal)
        self.store.import_core_records_from_filesystem(source)

    def test_captured_product_never_moves_to_product_reusing_context(self) -> None:
        self.reserve("old")
        other = self.profile.model_copy(
            update={"product": "other-product", "repository": "fixture/other"}
        )
        self.store.write_product_profile_record(other)
        result = self.observe()
        self.assertEqual({record.product for record in result.records}, {self.profile.product})
        revisions = self.store.health_monitor_authority_revisions()
        self.complete(self.store.list_held_provider_target_reservations()[0])
        after = self.store.health_monitor_authority_revisions()
        self.assertNotEqual(
            revisions[(self.profile.product, "example-site", "prod")],
            after[(self.profile.product, "example-site", "prod")],
        )
        self.assertEqual(
            revisions[(other.product, "example-site", "prod")],
            after[(other.product, "example-site", "prod")],
        )

    def test_unused_application_history_cannot_block_prod_observation(self) -> None:
        held = self.reserve("ordinary-prod")
        target = discover_public_ingress_monitor_targets(self.store)[0]
        for with_hold in (True, False):
            if not with_hold:
                self.complete(held)
            snapshot = self.store.read_provider_hold_snapshot(
                product=self.profile.product, context=self.origin.context, instance="prod"
            )
            target = replace(target, provider_hold_snapshot=snapshot)
            revisions = self.store.health_monitor_authority_revisions()
            observation = check_public_ingress_target(
                target=target,
                checked_at="2026-10-01T01:00:00Z",
                timeout_seconds=1,
                http_get=Mock(),
                tls_get=Mock(),
            )
            for corruption in ("original", "parent"):
                with self.subTest(with_hold=with_hold, corruption=corruption):
                    original = held.model_copy(
                        update={
                            "record_id": "unrelated-application-original",
                            "route_path": ORIGINAL_MUTATION_INTENT_ROUTE,
                            "provider_target_key": "",
                            "state": "completed",
                            "response_status_code": 200,
                            "recorded_at": held.updated_at,
                            "response_payload": {
                                "original_intent": {
                                    "testing_activation": {
                                        "product": self.profile.product,
                                        "context": self.origin.context,
                                    }
                                },
                                **(
                                    {"reservation_id": "unrelated-parent"}
                                    if corruption == "parent"
                                    else {}
                                ),
                            },
                        }
                    )
                    parent = held.model_copy(
                        update={
                            "record_id": "unrelated-parent",
                            "route_path": f"/v1/products/{self.profile.product}/testing-activation",
                            "provider_target_key": "",
                        }
                    )
                    with self.store._session_factory.begin() as session:
                        for record in (original, parent):
                            row = LaunchplaneIdempotencyRow(
                                record_id=record.record_id,
                                scope=record.scope,
                                route_path=record.route_path,
                                idempotency_key=record.idempotency_key,
                            )
                            self.store._sync_idempotency_row(row, record)
                            if record is parent:
                                row.payload = {
                                    key: value
                                    for key, value in row.payload.items()
                                    if key != "request_fingerprint"
                                }
                            session.add(row)
                    try:
                        self.assertEqual(
                            self.store.read_provider_hold_snapshot(
                                product=self.profile.product,
                                context=self.origin.context,
                                instance="prod",
                            ),
                            snapshot,
                        )
                        self.assertEqual(self.store.health_monitor_authority_revisions(), revisions)
                        self.assertEqual(
                            {
                                hold.reservation.record_id
                                for hold in self.store.list_provider_target_holds()
                            },
                            {held.record_id} if with_hold else set(),
                        )
                        with patch.object(
                            self.store,
                            "_database_mutation_timestamp",
                            return_value=observation.observed_at,
                        ):
                            publication = self.store.write_public_ingress_transition_with_outbox(
                                observation=observation,
                                incidents=(),
                                expected_provider_hold_snapshot=snapshot,
                            )
                        self.assertEqual(publication.status, "written")
                    finally:
                        with self.store._session_factory.begin() as session:
                            session.execute(
                                delete(LaunchplaneIdempotencyRow).where(
                                    LaunchplaneIdempotencyRow.record_id.in_(
                                        (original.record_id, parent.record_id)
                                    )
                                )
                            )

    def test_completed_old_incident_cannot_change_reused_target_lane_source(self) -> None:
        old = self.reserve("old-product")
        self.observe()
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        assert incident.material_fingerprint is not None
        references = tuple(
            value
            for value in incident.material_fingerprint.route_authority.target_keys
            if ProviderOperationReference.parse(value) is not None
        )
        self.assertTrue(references)
        self.complete(old)
        other = self.profile.model_copy(
            update={"product": "new-product", "repository": "fixture/new"}
        )
        self.store.write_product_profile_record(other)
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.origin.context,
                instance="prod",
                provider_id=self.origin.provider_id,
                target_category="compose",
                target_id=self.origin.target_id,
                display_name="Fixture",
                provider_target_type=self.origin.target_type,
                updated_at=old.created_at,
            )
        )
        current = self.reserve(
            "new-product", origin=self.origin.model_copy(update={"product": other.product})
        )
        original_lane = (self.profile.product, self.origin.context, "prod")
        current_lane = (other.product, self.origin.context, "prod")
        bulk = self.store.read_provider_hold_snapshots(
            lane_keys=(original_lane, current_lane), references_by_lane={original_lane: references}
        )
        singular = self.store.read_provider_hold_snapshot(
            product=other.product, context=self.origin.context, instance="prod"
        )
        self.assertEqual(bulk[current_lane], singular)
        self.assertEqual(
            {hold.reservation.record_id for hold in singular.holds}, {current.record_id}
        )
        target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.product == other.product and target.check_name == DEPLOY_FENCE_CHECK_NAME
        )
        observation = check_public_ingress_target(
            target=target,
            checked_at="2026-10-01T01:00:00Z",
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=observation.observed_at
        ):
            result = self.store.write_public_ingress_transition_with_outbox(
                observation=observation,
                incidents=(),
                expected_provider_hold_snapshot=target.provider_hold_snapshot,
            )
        self.assertEqual(result.status, "written")

    def test_repoint_type_change_and_delete_do_not_erase_origin(self) -> None:
        self.reserve("old")
        for target_type in ("compose", "application"):
            self.store.write_provider_target_record(
                ProviderTargetRecord(
                    context="example-site",
                    instance="prod",
                    provider_id="dokploy",
                    target_category=target_type,
                    target_id="different-target",
                    display_name="Fixture",
                    provider_target_type=target_type,
                    updated_at="2026-10-01T00:00:00Z",
                )
            )
            self.assertEqual(self.observe().fail_count, 1)
        with self.store._session_factory() as session:
            session.execute(delete(LaunchplaneProviderTargetRow))
            session.commit()
        self.assertEqual(self.observe().fail_count, 1)
        self.assertEqual(
            self.store.list_provider_target_holds()[0].lane,
            (self.profile.product, "example-site", "prod"),
        )

    def test_completed_exact_reference_settles_hold_without_claiming_success(self) -> None:
        old = self.reserve("old")
        self.assertEqual(self.observe().open_incident_count, 1)
        self.complete(old)
        result = self.observe("2026-10-01T01:01:00Z")
        self.assertEqual(result.resolved_incident_count, 1)
        self.assertEqual(result.records[0].check_kind, "provider")

    def test_missing_row_and_different_incarnation_never_settle_reference(self) -> None:
        old = self.reserve("old")
        opened = self.observe()
        self.assertEqual(opened.open_incident_count, 1)
        with self.store._session_factory() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.record_id == old.record_id
                )
            )
            session.commit()
        missing = self.observe("2026-10-01T01:01:00Z")
        self.assertEqual(missing.fail_count, 1)
        self.assertEqual(missing.resolved_incident_count, 0)
        self.reserve("old", now="2026-10-01T01:02:00Z")
        replaced = self.observe("2026-10-01T01:02:00Z")
        self.assertEqual(replaced.fail_count, 1)
        self.assertEqual(replaced.resolved_incident_count, 0)

    def test_missing_or_corrupt_receipt_is_unavailable_even_after_completion(self) -> None:
        for corruption in ("missing", "capsule", "full-receipt", "receipt-id", "receipt-sql-state"):
            with self.subTest(corruption=corruption):
                row = self.reserve(
                    corruption, origin=self.origin.model_copy(update={"target_id": corruption})
                )
                capsule = self.store.read_provider_target_origin(row)
                assert capsule is not None
                reference = ProviderOperationReference(
                    row.record_id, capsule.operation_nonce
                ).encode()
                self.observe()
                with self.store._session_factory() as session:
                    receipt = session.scalar(
                        select(LaunchplaneIdempotencyRow).where(
                            LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                            LaunchplaneIdempotencyRow.idempotency_key == row.record_id,
                        )
                    )
                    assert receipt is not None
                    if corruption == "missing":
                        session.delete(receipt)
                    else:
                        payload = dict(receipt.payload)
                        if corruption == "capsule":
                            payload["response_payload"] = {
                                **payload["response_payload"],
                                "provider_target_origin": {"invalid": True},
                            }
                        elif corruption == "receipt-sql-state":
                            receipt.state = "running"
                        elif corruption == "receipt-id":
                            payload["record_id"] = "different-receipt"
                        else:
                            payload["state"] = "not-a-state"
                        receipt.payload = payload
                    session.commit()
                self.complete(row)
                snapshot = self.store.read_provider_hold_snapshot(
                    product=self.profile.product,
                    context=self.origin.context,
                    instance=self.origin.instance,
                    references=(reference,),
                )
                self.assertEqual(snapshot.unavailable_references, (reference,))
                observed = self.observe("2026-10-01T01:01:00Z")
                self.assertEqual(observed.fail_count, 1)
                self.assertEqual(observed.resolved_incident_count, 0)

    def test_operation_set_is_material_heartbeat_and_order_are_not(self) -> None:
        first = self.reserve("first")
        initial = self.observe()
        fingerprint = initial.records[0].material_fingerprint_sha256
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value="2026-10-01T00:00:30Z"
        ):
            self.assertEqual(
                self.store.renew_mutation_reservation(reservation=first, lease_seconds=120).status,
                "updated",
            )
        repeated = self.observe("2026-10-01T01:01:00Z")
        self.assertEqual(repeated.records[0].material_fingerprint_sha256, fingerprint)
        self.assertEqual(repeated.incident_events, ())
        second = self.reserve(
            "second", origin=self.origin.model_copy(update={"target_id": "second"})
        )
        changed = self.observe("2026-10-01T01:02:00Z")
        self.assertNotEqual(changed.records[0].material_fingerprint_sha256, fingerprint)
        self.assertEqual(len(changed.incident_events), 1)
        target = discover_public_ingress_monitor_targets(self.store)[0]
        snapshot = target.provider_hold_snapshot
        assert snapshot is not None
        reversed_target = replace(
            target, provider_hold_snapshot=replace(snapshot, holds=tuple(reversed(snapshot.holds)))
        )
        reverse = check_public_ingress_target(
            target=reversed_target,
            checked_at="2026-10-01T01:02:00Z",
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        self.assertEqual(
            reverse.material_fingerprint_sha256, changed.records[0].material_fingerprint_sha256
        )
        refs = changed.records[0].material_fingerprint
        assert refs is not None
        self.assertEqual(
            {
                ref.record_id
                for value in refs.route_authority.target_keys
                if (ref := ProviderOperationReference.parse(value))
            },
            {first.record_id, second.record_id},
        )

    def test_mixed_legacy_participants_remain_unverified_in_both_completion_orders(self) -> None:
        from control_plane.provider_target_holds import ProviderOperationAttentionReference

        def make_legacy(row: LaunchplaneIdempotencyRecord) -> None:
            with self.store._session_factory.begin() as session:
                session.execute(
                    delete(LaunchplaneIdempotencyRow).where(
                        LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                        LaunchplaneIdempotencyRow.idempotency_key == row.record_id,
                    )
                )

        for strong_first in (False, True):
            with self.subTest(strong_first=strong_first):
                legacy = _hold_deploy_fence(self.store, idempotency_key=f"legacy-{strong_first}")
                make_legacy(legacy)
                strong = self.reserve(
                    f"strong-{strong_first}",
                    origin=self.origin.model_copy(update={"target_id": f"strong-{strong_first}"}),
                    now=legacy.created_at,
                )
                checked_at = _minutes_after(legacy.updated_at, 60)
                opened = self.observe(checked_at)
                fingerprint = opened.records[0].material_fingerprint
                assert fingerprint is not None
                markers = {
                    parsed.state
                    for value in fingerprint.route_authority.target_keys
                    if (parsed := ProviderOperationAttentionReference.parse(value))
                    and parsed.record_id == legacy.record_id
                }
                self.assertEqual(markers, {"held", "unverified"})
                repeated = self.observe(_minutes_after(checked_at, 1))
                self.assertEqual(repeated.incident_events, ())
                first, second = (strong, legacy) if strong_first else (legacy, strong)
                self.complete(first)
                changed = self.observe(_minutes_after(checked_at, 2))
                self.assertEqual(changed.fail_count, 1)
                self.assertEqual(changed.resolved_incident_count, 0)
                self.assertTrue(changed.incident_events)
                self.complete(second)
                final = self.observe(_minutes_after(checked_at, 3))
                self.assertEqual(final.fail_count, 1)
                self.assertEqual(final.resolved_incident_count, 0)

    def test_missing_legacy_is_material_but_never_settles_and_order_is_stable(self) -> None:
        from control_plane.provider_target_holds import ProviderOperationAttentionReference

        first = _hold_deploy_fence(self.store, idempotency_key="legacy-member")
        with self.store._session_factory.begin() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == first.record_id,
                )
            )
        checked_at = _minutes_after(first.updated_at, 60)
        self.observe(checked_at)
        self.reserve(
            "strong-member", origin=self.origin.model_copy(update={"target_id": "another"})
        )
        added = self.observe(_minutes_after(checked_at, 1))
        self.assertTrue(added.incident_events)
        target = discover_public_ingress_monitor_targets(self.store)[0]
        snapshot = target.provider_hold_snapshot
        assert snapshot is not None
        reversed_observation = check_public_ingress_target(
            target=replace(
                target,
                provider_hold_snapshot=replace(snapshot, holds=tuple(reversed(snapshot.holds))),
            ),
            checked_at=_minutes_after(checked_at, 1),
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        self.assertEqual(
            reversed_observation.material_fingerprint_sha256,
            added.records[0].material_fingerprint_sha256,
        )
        with self.store._session_factory.begin() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.record_id == first.record_id
                )
            )
        removed = self.observe(_minutes_after(checked_at, 2))
        self.assertTrue(removed.incident_events)
        self.assertNotEqual(
            removed.records[0].material_fingerprint_sha256,
            added.records[0].material_fingerprint_sha256,
        )
        self.assertEqual(removed.resolved_incident_count, 0)
        fingerprint = removed.records[0].material_fingerprint
        assert fingerprint is not None
        self.assertIn(
            ProviderOperationAttentionReference(first.record_id).encode(),
            fingerprint.route_authority.target_keys,
        )
        self.assertNotIn(
            ProviderOperationAttentionReference(first.record_id, "held").encode(),
            fingerprint.route_authority.target_keys,
        )

    def test_old_fingerprint_without_references_keeps_parser_and_hash(self) -> None:
        old = _hold_deploy_fence(self.store, idempotency_key="legacy-fixture")
        target = discover_public_ingress_monitor_targets(self.store)[0]
        observation = check_public_ingress_target(
            target=replace(target, provider_hold_snapshot=None, deploy_fence=old),
            checked_at=_minutes_after(old.updated_at, 60),
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        fingerprint = observation.material_fingerprint
        assert fingerprint is not None
        round_trip = PublicIngressIncidentMaterialFingerprint.model_validate_json(
            fingerprint.model_dump_json()
        )
        self.assertEqual(
            public_ingress_material_fingerprint_sha256(round_trip),
            observation.material_fingerprint_sha256,
        )
        self.assertFalse(
            any(
                ProviderOperationReference.parse(value)
                for value in round_trip.route_authority.target_keys
            )
        )

    def test_ref_less_warning_and_catalog_removal_do_not_auto_resolve(self) -> None:
        old = self.reserve("old")
        self.observe()
        incident = self.store.list_public_ingress_incident_records(status="open")[0]
        assert incident.material_fingerprint is not None
        fingerprint = incident.material_fingerprint.model_copy(
            update={
                "route_authority": incident.material_fingerprint.route_authority.model_copy(
                    update={"target_keys": ("provider:launchplane:launchplane-deploy-fence",)}
                )
            }
        )
        self.store.write_public_ingress_incident_record(
            incident.model_copy(
                update={
                    "material_fingerprint": fingerprint,
                    "material_fingerprint_sha256": public_ingress_material_fingerprint_sha256(
                        fingerprint
                    ),
                }
            )
        )
        self.complete(old)
        self.assertEqual(self.observe("2026-10-01T01:01:00Z").resolved_incident_count, 0)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"driver_id": "odoo"})
        )
        observed = self.observe("2026-10-01T01:02:00Z")
        self.assertEqual(observed.resolved_incident_count, 0)
        self.assertEqual(
            self.store.list_public_ingress_incident_records(status="open")[0].check_name,
            DEPLOY_FENCE_CHECK_NAME,
        )

    def test_legacy_origin_requires_product_and_physical_key_agreement(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            decode_generic_web_provider_reconciliation_target,
        )
        import base64

        row = _hold_deploy_fence(self.store, idempotency_key="legacy")
        with self.store._session_factory() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == row.record_id,
                )
            )
            session.commit()
        projected = self.store.list_provider_target_holds()[0]
        self.assertEqual(projected.attribution, "legacy")
        self.assertEqual(
            projected.origin.product if projected.origin else None, self.profile.product
        )
        self.assertEqual(projected.reference, "")
        snapshot = decode_generic_web_provider_reconciliation_target(row.reconciliation_key)
        for change in ({"product": ""}, {"target_id": "different-physical-target"}):
            changed = snapshot.model_copy(update=change)
            encoded = "generic-web-provider-target:" + base64.urlsafe_b64encode(
                changed.model_dump_json(exclude_none=True).encode()
            ).decode().rstrip("=")
            with self.store._session_factory() as session:
                stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
                assert stored is not None
                self.store._sync_idempotency_row(
                    stored, row.model_copy(update={"reconciliation_key": encoded})
                )
                session.commit()
            self.assertEqual(self.store.list_provider_target_holds()[0].attribution, "unavailable")
            self.assertEqual(self.store.list_provider_target_holds()[0].lane, None)

    def test_unassigned_and_other_namespaces_do_not_fan_out_to_product_lanes(self) -> None:
        row = self.reserve("unknown")
        with self.store._session_factory() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == row.record_id,
                )
            )
            session.commit()
        for target_key in (self.origin.target_key() + ":phase", "other-family-target"):
            self.store.reserve_mutation(
                scope="child",
                route_path="/child",
                idempotency_key=target_key,
                request_fingerprint="child",
                lease_owner="child",
                reconciliation_key="child",
                provider_target_key=target_key,
            )
        self.assertEqual(len(self.store.list_provider_target_holds()), 1)
        self.assertIsNone(self.store.list_provider_target_holds()[0].origin)
        self.assertEqual(self.observe().target_count, 0)
        self.assertEqual(self.observe().open_incident_count, 0)

    def test_original_attention_survives_retirement_deletion_and_current_product_reuse(
        self,
    ) -> None:
        from control_plane.storage.postgres import LaunchplaneProductProfileRow

        self.reserve("held")
        retired = self.profile.model_copy(update={"lifecycle_state": "retired"})
        self.store.write_product_profile_record(retired)
        self.assertEqual(
            self.store.list_provider_target_holds()[0].lane,
            (self.profile.product, "example-site", "prod"),
        )
        with self.store._session_factory() as session:
            session.execute(delete(LaunchplaneProductProfileRow))
            session.commit()
        other = self.profile.model_copy(update={"product": "current-product"})
        self.store.write_product_profile_record(other)
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.origin.context,
                instance=self.origin.instance,
                provider_id=self.origin.provider_id,
                target_category="compose",
                target_id=self.origin.target_id,
                display_name="Current fixture",
                provider_target_type="compose",
                updated_at="2026-10-01T00:00:00Z",
            )
        )
        projected = self.store.list_provider_target_holds()[0]
        self.assertEqual(projected.lane, (self.profile.product, "example-site", "prod"))
        self.assertFalse(projected.current_target_matches)
        self.assertEqual(projected.current_lanes, ((other.product, "example-site", "prod"),))
        self.assertEqual(self.observe().target_count, 0)

    def test_incident_retry_then_new_hold_never_leaves_an_unlinked_pass(self) -> None:
        first = self.reserve("first")
        self.observe()
        self.complete(first)
        transition = self.store.write_public_ingress_transition_with_outbox
        raced = False

        def change_incident_then_source(**kwargs: Any) -> PublicIngressTransitionWriteResult:
            nonlocal raced
            if raced:
                return transition(**kwargs)
            raced = True
            incident = self.store.list_public_ingress_incident_records(status="open")[0]
            self.store.write_public_ingress_incident_record(
                incident.model_copy(update={"state_version": incident.state_version + 1})
            )
            result = transition(**kwargs)
            self.assertEqual(result.status, "incident_changed")
            self.reserve(
                "replacement", origin=self.origin.model_copy(update={"target_id": "replacement"})
            )
            return result

        with patch.object(
            self.store,
            "write_public_ingress_transition_with_outbox",
            side_effect=change_incident_then_source,
        ):
            observed = self.observe("2026-10-01T01:01:00Z")
        self.assertEqual(observed.resolved_incident_count, 0)
        self.assertEqual(observed.pass_count, 0)
        self.assertFalse(
            any(
                record.status == "pass"
                for record in self.store.list_public_ingress_observation_records()
            )
        )

    def test_encoded_live_last_row_cannot_hide_a_stuck_same_lane_hold(self) -> None:
        import base64
        from control_plane.workflows.generic_web_deploy_provider import (
            decode_generic_web_provider_reconciliation_target,
        )

        old = _hold_deploy_fence(self.store, idempotency_key="encoded-old")
        checked_at = _minutes_after(old.updated_at, 60)
        snapshot = decode_generic_web_provider_reconciliation_target(old.reconciliation_key)
        for candidate in range(100):
            origin = self.origin.model_copy(update={"target_id": f"live-target-{candidate}"})
            if origin.target_key() > old.provider_target_key:
                break
        else:
            self.fail("fixture could not order the live physical key last")
        changed = snapshot.model_copy(update={"target_id": origin.target_id})
        reconciliation = "generic-web-provider-target:" + base64.urlsafe_b64encode(
            changed.model_dump_json(exclude_none=True).encode()
        ).decode().rstrip("=")
        with patch.object(self.store, "_database_mutation_timestamp", return_value=checked_at):
            live = self.store.reserve_mutation(
                scope="fixture",
                route_path="/fixture",
                idempotency_key="encoded-live",
                request_fingerprint="fixture",
                lease_owner="fixture",
                reconciliation_key=reconciliation,
                provider_target_key=origin.target_key(),
                provider_target_origin=origin,
            ).record
        observed = self.observe(checked_at)
        self.assertEqual(observed.fail_count, 1)
        target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.check_name == DEPLOY_FENCE_CHECK_NAME
        )
        assert target.provider_hold_snapshot is not None
        self.assertEqual(
            {hold.reservation.record_id for hold in target.provider_hold_snapshot.holds},
            {old.record_id, live.record_id},
        )

    def test_completed_reference_with_drifted_sql_projection_is_never_settlement(self) -> None:
        row = self.reserve("drifted")
        self.observe()
        self.complete(row)
        target = discover_public_ingress_monitor_targets(self.store)[0]
        snapshot = target.provider_hold_snapshot
        assert snapshot is not None
        observation = check_public_ingress_target(
            target=target,
            checked_at="2026-10-01T01:01:00Z",
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        self.assertEqual(observation.status, "pass")
        for column, corrupt in (("state", "running"), ("provider_target_key", "different-sql-key")):
            with self.subTest(column=column):
                with self.store._session_factory() as session:
                    stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
                    assert stored is not None
                    original = getattr(stored, column)
                    setattr(stored, column, corrupt)
                    session.commit()
                try:
                    if column == "state":
                        with self.assertRaises(ValueError):
                            self.store.list_provider_target_holds()
                    with self.assertRaises(ValueError):
                        self.store.read_provider_hold_snapshot(
                            product=self.profile.product,
                            context="example-site",
                            instance="prod",
                            references=snapshot.references,
                        )
                    result = self.store.write_public_ingress_transition_with_outbox(
                        observation=observation,
                        incidents=(),
                        expected_provider_hold_snapshot=snapshot,
                    )
                    self.assertEqual(result.status, "provider_source_changed")
                    self.assertFalse(
                        any(
                            record.status == "pass"
                            for record in self.store.list_public_ingress_observation_records()
                        )
                    )
                    self.assertTrue(self.store.list_public_ingress_incident_records(status="open"))
                finally:
                    with self.store._session_factory() as session:
                        stored = session.get(LaunchplaneIdempotencyRow, row.record_id)
                        assert stored is not None
                        setattr(stored, column, original)
                        session.commit()
