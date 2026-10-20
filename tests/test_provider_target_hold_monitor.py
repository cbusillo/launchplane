"""Behavioral proof of all-held custody, exact settlement and material operation sets."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from tempfile import TemporaryDirectory
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
        result = self.observe()
        self.assertEqual(result.fail_count, 1)
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
        self.complete(old)  # Deliberately terminal failure. It no longer holds.
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
