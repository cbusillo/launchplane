"""Real PostgreSQL native policy CAS, shared target and stale evidence fences."""

from threading import Barrier, Event, Thread

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.runner_relabel import ROUTE, RunnerRelabelAdapter, actor_scope, create_review
from control_plane.contracts.runner_relabel import RunnerRelabelReviewRequest
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_runner_relabel_native as scenarios
from tests.test_postgres_integration import _head_postgres_database


class RunnerRelabelPostgresTests(scenarios.RunnerRelabelNativeTests):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()

    def test_two_connections_policy_cas_has_one_winner(self) -> None:
        barrier = Barrier(2)
        results: list[str] = []

        def write(reason: str) -> None:
            store = PostgresRecordStore(database_url=self.database_url)
            try:
                barrier.wait(timeout=10)
                store.compare_and_write_runner_relabel_policy(
                    "fixture",
                    {
                        "policy": self.policy.model_dump(mode="json"),
                        "actor": self.actor,
                        "reason": reason,
                        "expected_policy_sha256": self.saved["policy_sha256"],
                    },
                    expected_sha256=self.saved["policy_sha256"],
                )
                results.append("written")
            except ValueError:
                results.append("stale")
            finally:
                store.close()

        threads = [Thread(target=write, args=(reason,)) for reason in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["written", "stale"])

    def test_other_connection_cannot_change_unknown_target_or_write_stale_evidence(self) -> None:
        other = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(other.close)
        review = self.review()
        adapter = RunnerRelabelAdapter(self.store, review, "trace", "first")
        first = self.store.reserve_mutation(
            scope="runner-owner:first",
            route_path="/runner",
            idempotency_key="first",
            request_fingerprint=canonical_json_sha256({"actor": 1}),
            lease_owner="worker-one",
            reconciliation_key="fixture-review",
            provider_target_key=adapter.target_key(),
        )
        assert first.record is not None
        self.store.mark_mutation_reconcile_required(
            reservation=first.record, reconciliation_key="fixture-review"
        )
        blocked = other.reserve_mutation(
            scope="runner-owner:second",
            route_path="/runner",
            idempotency_key="second",
            request_fingerprint=canonical_json_sha256({"actor": 2}),
            lease_owner="worker-two",
            reconciliation_key="different-review",
            provider_target_key=adapter.target_key(),
        )
        self.assertEqual(blocked.status, "target_busy")
        with self.assertRaises(ValueError):
            other.write_runner_relabel_document(
                "terminal", "stale-worker", {"status": "completed"}, reservation=first.record
            )
        self.assertIsNone(other.read_runner_relabel_document("terminal", "stale-worker"))

    def test_native_adapters_on_two_connections_serialize_before_dispatch(self) -> None:
        other = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(other.close)
        review = self.review()
        ready, release = Event(), Event()
        results: list[object] = []
        actor = {"type": "github_human", "github_id": 52}
        second_review = create_review(
            other,
            "fixture",
            RunnerRelabelReviewRequest(
                desired_custom_labels=("candidate", "general"),
                reason="second actor reviewed target",
            ),
            actor,
        )

        def at_dispatch() -> None:
            ready.set()
            if not release.wait(timeout=15):
                raise RuntimeError("Fixture dispatch was not released.")

        def first() -> None:
            try:
                results.append(self.run_change(review, authorize_effect=at_dispatch))
            except Exception as error:
                results.append(error)

        thread = Thread(target=first)
        thread.start()
        try:
            self.assertTrue(ready.wait(timeout=10))
            intent: dict[str, object] = {
                "review": second_review,
                "actor": actor,
                "operation_key": "second",
            }
            result = run_durable_provider_operation(
                store=other,
                scope=actor_scope(actor),
                route_path=ROUTE.format(policy_id="fixture"),
                idempotency_key="second",
                request_fingerprint=canonical_json_sha256(intent),
                lease_owner="second-worker",
                response_trace_id="second-trace",
                adapter=RunnerRelabelAdapter(other, second_review, "second-trace", "second"),
                original_intent=intent,
            )
            self.assertEqual(result.status, "target_busy")
            self.assertEqual(self.transport.puts, 0)
        finally:
            release.set()
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(getattr(results[0], "status", None), "completed", results)
        self.assertEqual(self.transport.puts, 1)

    def test_native_adapter_cannot_forward_put_after_lease_revocation(self) -> None:
        other = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(other.close)
        review = self.review()

        def revoke() -> None:
            record = other.read_idempotency_record(
                scope=actor_scope(self.actor),
                route_path=ROUTE.format(policy_id="fixture"),
                idempotency_key="original",
            )
            assert record is not None
            other.mark_mutation_reconcile_required(
                reservation=record, reconciliation_key=record.reconciliation_key
            )

        self.assertEqual(
            self.run_change(review, authorize_effect=revoke).status, "reconcile_required"
        )
        self.assertEqual(self.transport.puts, 0)
