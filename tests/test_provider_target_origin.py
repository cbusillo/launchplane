"""Original attention identity survives retries without changing physical fencing."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.storage.postgres import PostgresRecordStore, MutationReservationResult


class ProviderTargetOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'records.db'}"
        )
        self.store.ensure_schema()
        self.origin = ProviderTargetOrigin(
            product="original-product",
            context="context",
            instance="testing",
            provider_id="dokploy",
            target_type="compose",
            target_id="original-target",
        )

    def reserve(
        self,
        *,
        key: str = "operation",
        origin: ProviderTargetOrigin | None = None,
        fingerprint: str = "fingerprint",
        intent: dict[str, object] | None = None,
    ) -> MutationReservationResult:
        return self.store.reserve_mutation(
            scope="scope",
            route_path="/test",
            idempotency_key=key,
            request_fingerprint=fingerprint,
            lease_owner="owner",
            reconciliation_key="reconcile",
            provider_target_key=self.origin.target_key(),
            provider_target_origin=origin,
            original_intent=intent,
        )

    def test_fresh_origin_required_and_rollback_leaves_no_holder(self) -> None:
        with self.assertRaises(ValueError):
            self.reserve()
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        result = self.reserve(origin=self.origin)
        self.assertEqual(result.status, "acquired")
        capsule = self.store.read_provider_target_origin(result.record)
        assert capsule is not None
        self.assertEqual(capsule.origin, self.origin)

    def test_retry_and_loser_preserve_first_capsule_and_family_intent(self) -> None:
        intent: dict[str, object] = {"family": {"unchanged": True}}
        first = self.reserve(origin=self.origin, intent=intent)
        capsule = self.store.read_provider_target_origin(first.record)
        other = self.origin.model_copy(update={"product": "different-product"})
        self.assertEqual(self.reserve(origin=other).status, "in_progress")
        self.assertEqual(self.reserve(key="loser", origin=other).status, "target_busy")
        self.assertEqual(self.reserve(fingerprint="different").status, "conflict")
        self.assertEqual(self.store.read_provider_target_origin(first.record), capsule)
        self.assertEqual(self.store.read_original_mutation_intent(first.record), intent)

    def test_fresh_reacquisition_mints_new_nonce_even_with_same_record_id(self) -> None:
        first = self.reserve(origin=self.origin)
        before = self.store.read_provider_target_origin(first.record)
        self.assertEqual(
            self.store.release_reserved_mutation(reservation=first.record).status, "released"
        )
        from unittest.mock import patch

        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=first.record.created_at
        ):
            second = self.reserve(origin=self.origin)
        self.assertEqual(first.record.created_at, second.record.created_at)
        after = self.store.read_provider_target_origin(second.record)
        self.assertEqual(first.record.record_id, second.record.record_id)
        assert before is not None and after is not None
        self.assertNotEqual(before.operation_nonce, after.operation_nonce)

    def test_mismatched_origin_cannot_acquire(self) -> None:
        with self.assertRaises(ValueError):
            self.reserve(origin=self.origin.model_copy(update={"target_id": "different-target"}))
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_receipt_write_exception_rolls_back_parent_and_receipt(self) -> None:
        from unittest.mock import patch
        from control_plane.storage.postgres import ORIGINAL_MUTATION_INTENT_ROUTE

        retain = self.store._retain_original_mutation_intent

        def fail_after_receipt(
            session: Any,
            reservation: LaunchplaneIdempotencyRecord,
            intent: dict[str, object] | None,
            origin: ProviderTargetOrigin | None,
        ) -> None:
            retain(session, reservation, intent, origin)
            session.flush()
            raise RuntimeError("injected after receipt")

        with patch.object(
            self.store, "_retain_original_mutation_intent", side_effect=fail_after_receipt
        ):
            with self.assertRaisesRegex(RuntimeError, "injected after receipt"):
                self.reserve(origin=self.origin)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        with self.store._session_factory() as session:
            from control_plane.storage.postgres import LaunchplaneIdempotencyRow
            from sqlalchemy import select

            self.assertEqual(
                list(
                    session.scalars(
                        select(LaunchplaneIdempotencyRow).where(
                            LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE
                        )
                    )
                ),
                [],
            )
        self.assertEqual(self.reserve(origin=self.origin).status, "acquired")

    def test_historical_absence_survives_observation_conflict_expiry_and_replay(self) -> None:
        from unittest.mock import patch
        from control_plane.contracts.idempotency_record import (
            build_launchplane_mutation_reservation,
        )
        from control_plane.service_deploy_drain import ServiceDeployDraining

        historical = build_launchplane_mutation_reservation(
            scope="scope",
            route_path="/test",
            idempotency_key="operation",
            request_fingerprint="fingerprint",
            lease_owner="old-owner",
            reserved_at="2026-10-01T00:00:00Z",
            lease_expires_at="2026-10-01T00:01:00Z",
            reconciliation_key="reconcile",
            provider_target_key=self.origin.target_key(),
        )
        # Explicit old-schema fixture insertion, not a new admission without origin.
        with self.store._session_factory() as session:
            session.add(self.store._idempotency_row(historical))
            session.commit()
        self.assertEqual(self.reserve(fingerprint="different").status, "conflict")
        self.assertEqual(self.reserve(key="other").status, "target_busy")
        expired = self.reserve(origin=self.origin)
        self.assertEqual(expired.status, "reconcile_required")
        self.assertIsNone(self.store.read_provider_target_origin(expired.record))
        with patch(
            "control_plane.service_deploy_drain.admit_reservation",
            side_effect=ServiceDeployDraining("drain"),
        ):
            self.assertEqual(self.reserve().status, "reconcile_required")
        self.assertEqual(
            self.store.adopt_reconciled_mutation(
                reservation=expired.record,
                response_status_code=202,
                response_trace_id="recovered",
                response_payload={"observed": True},
            ).status,
            "adopted",
        )
        self.assertEqual(self.reserve().status, "replayed")
        self.assertIsNone(self.store.read_provider_target_origin(self.reserve().record))

    def test_runner_refuses_missing_origin_before_dispatch(self) -> None:
        from unittest.mock import patch
        from tests.test_provider_operations import _FakeAdapter
        from control_plane.provider_operations import run_durable_provider_operation

        adapter = _FakeAdapter()
        with patch.object(adapter, "target_key", return_value=self.origin.target_key()):
            with self.assertRaises(ValueError):
                run_durable_provider_operation(
                    store=self.store,
                    scope="scope",
                    route_path="/test",
                    idempotency_key="operation",
                    request_fingerprint="fingerprint",
                    lease_owner="owner",
                    response_trace_id="trace",
                    adapter=adapter,
                )
        self.assertEqual(adapter.apply_calls, 0)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_supersession_requires_new_origin_and_mints_distinct_nonce_atomically(self) -> None:
        first = self.reserve(origin=self.origin, intent={"original": "family"}).record
        marked = self.store.mark_mutation_reconcile_required(
            reservation=first, reconciliation_key=first.reconciliation_key
        ).record
        assert marked is not None
        from unittest.mock import patch

        before = self.store.read_provider_target_origin(marked)
        arguments: dict[str, Any] = dict(
            reservation=marked,
            response_status_code=409,
            response_trace_id="superseded",
            response_payload={"superseded": True},
            scope="scope",
            route_path="/test",
            idempotency_key="replacement",
            request_fingerprint="replacement",
            lease_owner="new-owner",
            lease_seconds=300,
            minimum_expired_seconds=0,
            reconciliation_key=marked.reconciliation_key,
            provider_target_key=marked.provider_target_key,
            expected_original_intent={"original": "family"},
        )
        expired_at = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                marked.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        with patch.object(self.store, "_database_mutation_timestamp", return_value=expired_at):
            with self.assertRaises(ValueError):
                self.store.supersede_expired_reconciled_mutation_and_reserve(**arguments)
            current = self.store.read_idempotency_record(
                scope="scope", route_path="/test", idempotency_key="operation"
            )
            self.assertEqual(current, marked)
            self.assertEqual(self.store.read_provider_target_origin(marked), before)
            replacement = self.store.supersede_expired_reconciled_mutation_and_reserve(
                **arguments, provider_target_origin=self.origin
            ).record
        assert replacement is not None
        after = self.store.read_provider_target_origin(replacement)
        assert before is not None and after is not None
        self.assertNotEqual(before.operation_nonce, after.operation_nonce)
        self.assertEqual(self.store.read_provider_target_origin(marked), before)
        self.assertEqual(self.store.read_original_mutation_intent(marked), {"original": "family"})

    def test_invalid_or_mismatched_receipt_is_unavailable(self) -> None:
        from control_plane.storage.postgres import ORIGINAL_MUTATION_INTENT_ROUTE

        acquired = self.reserve(origin=self.origin, intent={"family": True}).record
        receipt = self.store.read_idempotency_record(
            scope=acquired.scope,
            route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
            idempotency_key=acquired.record_id,
        )
        assert receipt is not None
        for payload in (
            {**receipt.response_payload, "reservation_id": "other"},
            {**receipt.response_payload, "provider_target_origin": {}},
            {
                **receipt.response_payload,
                "provider_target_origin": {
                    **receipt.response_payload["provider_target_origin"],
                    "origin": self.origin.model_copy(update={"target_id": "other"}).model_dump(
                        mode="json"
                    ),
                },
            },
        ):
            with self.subTest(payload=payload):
                self.store.write_idempotency_record(
                    receipt.model_copy(update={"response_payload": payload})
                )
                self.assertIsNone(self.store.read_provider_target_origin(acquired))
        self.store.write_idempotency_record(
            receipt.model_copy(update={"request_fingerprint": "wrong"})
        )
        self.assertIsNone(self.store.read_provider_target_origin(acquired))

    def test_child_effect_namespace_keeps_its_own_admission_without_origin(self) -> None:
        result = self.store.reserve_mutation(
            scope="scope",
            route_path="/effect",
            idempotency_key="effect",
            request_fingerprint="effect",
            lease_owner="owner",
            reconciliation_key="effect",
            provider_target_key=self.origin.target_key() + ":retire-old",
        )
        self.assertEqual(result.status, "acquired")
        self.assertIsNone(self.store.read_provider_target_origin(result.record))
        with self.assertRaises(ValueError):
            self.store.reserve_mutation(
                scope="scope",
                route_path="/malformed",
                idempotency_key="malformed",
                request_fingerprint="malformed",
                lease_owner="owner",
                reconciliation_key="malformed",
                provider_target_key="generic-web-provider-target:malformed",
            )

    def test_reconciled_retry_keeps_original_nonce_and_product(self) -> None:
        original = self.reserve(origin=self.origin).record
        before = self.store.read_provider_target_origin(original)
        marked = self.store.mark_mutation_reconcile_required(
            reservation=original, reconciliation_key=original.reconciliation_key
        ).record
        assert marked is not None
        retried = self.store.retry_reconciled_mutation(
            reservation=marked, lease_owner="recovering-owner"
        ).record
        assert retried is not None
        self.assertGreater(retried.attempt, original.attempt)
        self.assertEqual(self.store.read_provider_target_origin(retried), before)
        self.assertEqual(self.store.read_original_mutation_intent(retried), {})

    def test_expired_unbound_reclaim_requires_origin_and_preserves_family_custody(self) -> None:
        from unittest.mock import patch

        first = self.store.reserve_mutation(
            scope="scope",
            route_path="/test",
            idempotency_key="operation",
            request_fingerprint="fingerprint",
            lease_owner="old",
            original_intent={"original": "unbound-family"},
        ).record
        expired_at = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                first.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        with patch.object(self.store, "_database_mutation_timestamp", return_value=expired_at):
            with self.assertRaises(ValueError):
                self.reserve()
            unchanged = self.store.read_idempotency_record(
                scope="scope", route_path="/test", idempotency_key="operation"
            )
            self.assertEqual(unchanged, first)
            reclaimed = self.reserve(
                origin=self.origin, intent={"retry": "must-not-replace-family"}
            ).record
        self.assertEqual(reclaimed.record_id, first.record_id)
        self.assertGreater(reclaimed.attempt, first.attempt)
        capsule = self.store.read_provider_target_origin(reclaimed)
        assert capsule is not None
        self.assertEqual(capsule.origin, self.origin)
        self.assertEqual(
            self.store.read_original_mutation_intent(reclaimed), {"original": "unbound-family"}
        )

    def test_corrupt_full_receipt_is_unavailable_without_changing_family_reader(self) -> None:
        from control_plane.storage.postgres import ORIGINAL_MUTATION_INTENT_ROUTE

        acquired = self.reserve(origin=self.origin).record
        with self.store._session_factory() as session:
            row = session.scalar(
                self.store._idempotency_statement(
                    scope=acquired.scope,
                    route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
                    idempotency_key=acquired.record_id,
                )
            )
            assert row is not None
            row.payload = {"schema_version": 2}
            session.commit()
        self.assertIsNone(self.store.read_provider_target_origin(acquired))
        with self.assertRaises(ValueError):
            self.store.read_original_mutation_intent(acquired)
