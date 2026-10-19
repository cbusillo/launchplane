"""Real PostgreSQL native policy CAS, shared target and stale evidence fences."""

from threading import Barrier, Event, Thread
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.runner_relabel import ROUTE, RunnerRelabelAdapter, actor_scope, create_review
from control_plane.contracts.runner_relabel import RunnerRelabelReviewRequest
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_runner_relabel_native as scenarios
from tests.test_postgres_integration import _head_postgres_database
from tests import test_http_runner_relabel as http_scenarios
from tests.support.http import request
from datetime import timedelta
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp, format_launchplane_mutation_timestamp


class RunnerRelabelPostgresTests(scenarios.RunnerRelabelNativeTests):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()

    def test_concurrent_immutable_insert_replays_identical_and_rejects_changed_payload(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                barrier = Barrier(2)
                results: list[str] = []
                payload = {"policy": self.policy.model_dump(mode="json"), "actor": self.actor,
                    "reason": "reviewed candidate", "expected_policy_sha256": ""}
                key = canonical_json_sha256([payload, changed])

                def write(change: bool) -> None:
                    store = PostgresRecordStore(database_url=self.database_url)
                    factory = store._session_factory

                    @contextmanager
                    def synchronized() -> Iterator[Any]:
                        with factory() as session:
                            scalar = session.scalar
                            waited = False

                            def read(*args: Any, **kwargs: Any) -> Any:
                                nonlocal waited
                                result = scalar(*args, **kwargs)
                                if result is None and not waited:
                                    waited = True
                                    barrier.wait(timeout=10)
                                return result

                            with patch.object(session, "scalar", side_effect=read):
                                yield session

                    candidate = {**payload, "reason": "different candidate"} if change else payload
                    try:
                        with patch.object(store, "_session_factory", side_effect=synchronized):
                            store.write_runner_relabel_document("policy-review", key, candidate)
                        results.append("saved")
                    except ValueError:
                        results.append("conflict")
                    except Exception as error:
                        results.append(type(error).__name__)
                    finally:
                        store.close()

                threads = [Thread(target=write, args=(False,)), Thread(target=write, args=(changed,))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)
                    self.assertFalse(thread.is_alive())
                self.assertCountEqual(results, ["saved", "conflict" if changed else "saved"])
                winner = self.store.read_runner_relabel_document("policy-review", key)
                self.assertIn(winner, [payload, {**payload, "reason": "different candidate"}] if changed else [payload])

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


class HttpRunnerRelabelPostgresTests(http_scenarios.HttpRunnerRelabelTests):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()

    async def reviewed_operation(self) -> dict[str, Any]:
        response = await request(self.app, "POST", self.path, headers=self.headers(), payload=self.body)
        self.assertEqual(response.status_code, 202, response.text)
        saved = response.json()["result"]
        return {"review_id": saved["review_id"], "expected_review_sha256": saved["review_sha256"]}

    async def operate(self, body: dict[str, Any], *, key: str = "original", headers: dict[str, str] | None = None, capture: bool = False) -> Any:
        return await request(self.app, "POST", ROUTE.format(policy_id=self.fixture.policy.policy_id),
            headers={**(headers if headers is not None else self.headers()), "Idempotency-Key": key},
            payload=body, capture_server_error_response=capture)

    async def test_http_policy_apply_read_operation_apply_replay_key_conflict_and_reader(self) -> None:
        path = f"/v1/runner-relabel/{self.fixture.policy.policy_id}/policy"
        candidate = {"policy": self.fixture.policy.model_dump(mode="json"), "reason": "new reviewed policy",
            "expected_policy_sha256": self.fixture.saved["policy_sha256"]}
        review = await request(self.app, "POST", path, headers=self.headers(), payload=candidate)
        self.assertEqual(review.status_code, 202, review.text)
        applied = await request(self.app, "POST", path, headers=self.headers(), payload={**candidate, "mode": "apply", "expected_plan_sha256": review.json()["result"]["plan_sha256"]})
        self.assertEqual(applied.status_code, 202, applied.text)
        read = await request(self.app, "GET", path, headers={"Cookie": self.sessions.session_cookie_header(self.session)})
        self.assertEqual(read.status_code, 202, read.text)
        self.assertEqual(read.json()["result"], applied.json()["result"])
        body = await self.reviewed_operation()
        first = await self.operate(body)
        self.assertEqual(first.status_code, 202, first.text)
        replay = await self.operate(body)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(first.json(), replay.json())
        changed_key = await self.operate(body, key="changed-key")
        self.assertEqual(changed_key.status_code, 409, changed_key.text)
        fresh = await self.reviewed_operation()
        changed_review = await self.operate(fresh)
        self.assertEqual(changed_review.status_code, 409, changed_review.text)
        state = await request(self.app, "GET", self.path + "/" + body["review_id"], headers={"Authorization": "Bearer fixture-agent"})
        self.assertEqual(state.status_code, 202, state.text)
        self.assertEqual(state.json()["result"]["operation"]["response_payload"], first.json())
        self.assertEqual(self.fixture.transport.puts, 1)

    async def test_http_unknown_operation_reconciles_without_second_put(self) -> None:
        body = await self.reviewed_operation()
        self.fixture.transport.unknown = True
        applied = await self.operate(body)
        self.assertEqual(applied.status_code, 409, applied.text)
        self.fixture.transport.unknown = False
        observed = await self.operate({**body, "mode": "reconcile"})
        self.assertEqual(observed.status_code, 202, observed.text)
        self.assertEqual(self.fixture.transport.puts, 1)

    async def test_http_successful_terminal_recovers_lost_completion_without_provider(self) -> None:
        body = await self.reviewed_operation()
        with patch.object(self.store, "complete_mutation_reservation", side_effect=OSError("fixture completion lost")):
            failed = await self.operate(body, capture=True)
        self.assertEqual(failed.status_code, 500, failed.text)
        actor = {"type": "github_human", "github_id": self.human.github_id}
        original = self.store.read_idempotency_record(scope=actor_scope(actor), route_path=ROUTE.format(policy_id=self.fixture.policy.policy_id), idempotency_key="original")
        assert original is not None
        after = format_launchplane_mutation_timestamp(parse_launchplane_mutation_timestamp(original.lease_expires_at, field_name="lease_expires_at") + timedelta(seconds=1))
        self.fixture.factory.reset_mock()
        with patch.object(self.store, "_database_mutation_timestamp", return_value=after):
            recovered = await self.operate({**body, "mode": "recover-evidence"})
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertTrue(recovered.json()["result"]["evidence_recovered"])
        self.fixture.factory.assert_not_called()
        self.assertEqual(self.fixture.transport.puts, 1)

    async def test_scoped_agent_applies_under_own_identity_and_recorded_scope(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import LaunchplaneAuthzPolicy, BearerIdentityConfig
        from tests.http_app_test_support import _RejectingVerifier
        from tests.support.auth import scoped_agent_transport
        policy = LaunchplaneAuthzPolicy.model_validate({"local_operators": [{
            "subjects": ["scoped-runner-agent"], "token_labels": ["scoped-runner"],
            "products": [self.fixture.policy.product], "contexts": [self.fixture.policy.context],
            "actions": ["runner_relabel.plan", "runner_relabel.apply", "runner_relabel.read", "runner_relabel.recover"],
        }]})
        # Separate database gives this test its own current authorization policy.
        url = self.enterContext(_head_postgres_database())
        own = PostgresRecordStore(database_url=url)
        self.addCleanup(own.close)
        own.compare_and_write_runner_relabel_policy(self.fixture.policy.policy_id, {
            "policy": self.fixture.policy.model_dump(mode="json"), "actor": self.fixture.actor,
            "reason": "fixture binding", "expected_policy_sha256": "",
        }, expected_sha256="")
        self.seed_authorization(own, policy)
        app = create_launchplane_fastapi_app(verifier=_RejectingVerifier(), authz_policy=policy, record_store_factory=lambda: own,
            bearer_identity_config=BearerIdentityConfig(local_operator_token="scoped-token", local_operator_subject="scoped-runner-agent", local_operator_token_label="scoped-runner"))
        headers = {"Authorization": "Bearer scoped-token"}
        with scoped_agent_transport():
            review = await request(app, "POST", self.path, headers=headers, payload=self.body)
            self.assertEqual(review.status_code, 202, review.text)
            saved = review.json()["result"]
            applied = await request(app, "POST", ROUTE.format(policy_id=self.fixture.policy.policy_id), headers={**headers, "Idempotency-Key": "scoped-original"}, payload={"review_id": saved["review_id"], "expected_review_sha256": saved["review_sha256"]})
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(applied.json()["result"]["actor"], saved["actor"])
        self.assertEqual(saved["actor"]["subject"], "scoped-runner-agent")
        self.assertEqual(self.fixture.transport.puts, 1)
