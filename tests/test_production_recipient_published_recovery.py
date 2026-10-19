"""Published recovery keeps historical bootstrap proof and owns its exact capture."""

from dataclasses import replace
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
import json
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.production_recipient import RecipientRecoveryRequest
from control_plane.production_recipient import custody, runtime_values
from control_plane.production_recipient_recovery import RECOVERY_ROUTE, RecipientRecoveryAdapter
from control_plane.production_recipient_published_recovery import (
    CAPTURE_WITNESS_MAX_AGE_SECONDS,
    CAPTURE_TIMEOUT_SECONDS,
    published_proof_is_fresh,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient_publication as publication_tests
from tests.support.recipient_recovery_observation import (
    AUTHORITY_DRIFT,
    PROVIDER_DRIFT,
    UNREADABLE_OBSERVATIONS,
    assert_observation_scenarios,
    assert_optional_provider_fields_remain_qualified,
)


class PublishedRecoveryTests(unittest.TestCase):
    database_url = ""
    scenario_database: Callable[[], AbstractContextManager[str]] | None = None

    def setUp(self) -> None:
        self.publication = publication_tests.RecipientPublicationTests()
        self.publication.database_url = self.database_url
        self.publication.setUp()
        self.addCleanup(self.publication.doCleanups)
        self.fixture = self.publication.fixture
        self.store: PostgresRecordStore = self.fixture.store
        self.record = self.publication.publish()
        self.adapter = RecipientRecoveryAdapter(
            self.fixture.root,
            self.store,
            self.record.product,
            RecipientRecoveryRequest(
                context=self.record.context,
                preparation_plan_sha256=self.record.plan_sha256,
                purpose="published",
                generation="post-window-1",
                reason="Prove the current placement",
            ),
            self.record.actor,
            self.record.scope,
            "published-recovery-fixture",
            "published-key",
        )
        _, plan = self.adapter.current()
        self.adapter.request = self.adapter.request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": plan.plan_sha256,
            }
        )
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def evidence(spec: dict[str, Any]) -> dict[str, str]:
        now = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        return {
            "recipient_recovery_ok": "true",
            "recipient_recovery_empty": "false",
            "recipient_recovery_binding": spec["recovery_binding"],
            "recipient_recovery_operation": spec.get("operation", ""),
            "recipient_recovery_capture": "a" * 64,
            "recipient_recovery_source": "b" * 64,
            "recipient_recovery_data": "c" * 64,
            "recipient_recovery_epoch": now,
            "recipient_recovery_started": now,
            "recipient_recovery_duration_ms": "25",
        }

    def host(self, _root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
        self.calls.append(spec)
        checkpoint("published_capture")
        return self.evidence(spec)

    def run_recovery(self, adapter: Any = None) -> Any:
        selected = adapter or self.adapter
        return run_durable_provider_operation(
            store=selected.store,
            scope=selected.scope,
            route_path=RECOVERY_ROUTE.format(product=self.record.product),
            idempotency_key=selected.original_key,
            request_fingerprint="e" * 64,
            lease_owner="published-fixture",
            response_trace_id=selected.trace_id,
            adapter=selected,
        )

    def test_published_capture_is_fresh_separate_and_replays_only_its_original_key(self) -> None:
        self.assertFalse(self.adapter.current()[1].recovered)
        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=self.host,
        ):
            first = self.run_recovery()
            self.assertEqual(first.status, "completed")
            self.assertEqual(self.run_recovery().response_payload, first.response_payload)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(first.response_payload["result"]["captured_database_empty"])
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertEqual(saved.recovery_proof, self.record.recovery_proof)
        proof = saved.published_recovery_proofs[self.adapter.request.reviewed_plan_sha256]
        self.assertEqual(proof["publication_sha256"], self.record.publication["published_sha256"])
        self.assertEqual(proof["original_key"], self.adapter.original_key)
        with self.assertRaises(ValueError):
            replace(self.adapter, original_key="another-key").current()
        # A new generation requests new proof; historical success is not fresh evidence.
        next_adapter = replace(
            self.adapter,
            request=self.adapter.request.model_copy(
                update={
                    "generation": "post-window-2",
                    "reviewed_plan_sha256": "",
                }
            ),
        )
        self.assertFalse(next_adapter.current()[1].recovered)
        self.assertIn(self.record.retained_target_id.target_id, self.fixture.provider)

    def test_terminal_passed_witness_with_new_current_plan_completes_historically(self) -> None:
        from control_plane.production_recipient_published_recovery import CAPTURE_PROTOCOL

        original_binding = self.adapter.request.reviewed_plan_sha256
        terminal: dict[str, str] = {}
        calls: list[str] = []
        observable = False

        def host(_root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
            calls.append("inspect" if spec.get("inspect") else "apply")
            checkpoint("original_capture")
            if not spec.get("inspect"):
                terminal.update(self.evidence(spec))
                raise TimeoutError("Original host reply was lost")
            if not observable:
                raise TimeoutError("Terminal receipt is not observable yet")
            return dict(terminal)

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program", side_effect=host
        ):
            self.assertEqual(self.run_recovery().status, "reconcile_required")
            saved = custody(self.store, self.record.product, self.record.context)
            assert saved is not None
            owner = saved.execution["published_recovery_owner:" + original_binding]
            observable = True
            with patch(
                "control_plane.production_recipient_published_recovery.CAPTURE_PROTOCOL",
                CAPTURE_PROTOCOL + "-next",
            ):
                _, current = self.adapter.current()
                self.assertNotEqual(current.plan_sha256, original_binding)
                settled = self.run_recovery()
                self.assertEqual(settled.status, "adopted")
                self.assertFalse(settled.response_payload["result"]["recovered"])
                self.assertEqual(
                    settled.response_payload["result"]["plan_sha256"], original_binding
                )
                count = len(calls)
                self.assertEqual(self.run_recovery().status, "replayed")
                self.assertEqual(len(calls), count)
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)
        self.assertEqual(saved.execution["published_recovery_owner:" + original_binding], owner)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        self.assertEqual(calls.count("apply"), 1)

    def test_terminal_provider_drift_completes_historically(self) -> None:
        assert_observation_scenarios(self, PROVIDER_DRIFT, qualifies=False)

    def test_terminal_authority_facts_complete_historically(self) -> None:
        assert_observation_scenarios(self, AUTHORITY_DRIFT, qualifies=False)

    def test_unreadable_observations_retry_the_original_proof(self) -> None:
        assert_observation_scenarios(self, UNREADABLE_OBSERVATIONS, qualifies=True)

    def test_optional_provider_fields_preserve_qualification(self) -> None:
        assert_optional_provider_fields_remain_qualified(self)

    def test_owned_authority_race_completes_the_original_witness(self) -> None:
        assert_observation_scenarios(self, ("owned_row_race",), qualifies=False)

    def test_capture_only_empty_or_stale_evidence_never_qualifies(self) -> None:
        _, plan = self.adapter.current()
        spec = self.adapter.host_spec(self.record, plan)
        changes = (
            {"recipient_recovery_ok": "false"},
            {"recipient_recovery_empty": "true"},
            {"recipient_recovery_binding": "d" * 64},
            {"recipient_recovery_data": ""},
            {"recipient_recovery_source": ""},
            {"recipient_recovery_capture": "wrong"},
            {"recipient_recovery_started": "1"},
            {"recipient_recovery_duration_ms": str(CAPTURE_TIMEOUT_SECONDS * 1000 + 1)},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.adapter.record_host_result(self.record, plan, self.evidence(spec) | change)
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)

    def test_stale_witness_completes_historically_and_allows_a_fresh_generation(self) -> None:
        _, plan = self.adapter.current()
        evidence = self.evidence(self.adapter.host_spec(self.record, plan))
        future = datetime.now(timezone.utc) + timedelta(
            seconds=CAPTURE_WITNESS_MAX_AGE_SECONDS + 60
        )

        with (
            patch(
                "control_plane.production_recipient_published_recovery.datetime", wraps=datetime
            ) as clock,
            patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                return_value=evidence,
            ) as host,
        ):
            clock.now.return_value = future

            # The checkpoint still belongs to this original operation.
            def witnessed(
                _root: Any, _record: Any, _spec: Any, _program: Any, checkpoint: Any
            ) -> Any:
                checkpoint("historical_capture")
                return evidence | {"recipient_recovery_operation": _spec["operation"]}

            host.side_effect = witnessed
            self.assertEqual(self.run_recovery().status, "completed")
            historical = self.adapter.current()[1]
        self.assertFalse(historical.recovered)
        with patch(
            "control_plane.production_recipient_published_recovery.datetime", wraps=datetime
        ) as clock:
            clock.now.return_value = future
            self.assertFalse(published_proof_is_fresh(historical.model_dump()))
            replayed = self.run_recovery()
        self.assertEqual(replayed.status, "replayed")
        self.assertEqual(
            replayed.response_payload["result"]["recovery_fresh_until"],
            historical.recovery_fresh_until,
        )
        self.assertEqual(
            historical.recovered_at,
            datetime.fromtimestamp(
                int(evidence["recipient_recovery_epoch"]) / 1000, timezone.utc
            ).isoformat(),
        )
        next_adapter = replace(
            self.adapter,
            original_key="fresh-generation-key",
            request=self.adapter.request.model_copy(
                update={
                    "generation": "post-window-2",
                    "reviewed_plan_sha256": "",
                }
            ),
        )
        _, next_plan = next_adapter.current()
        next_adapter.request = next_adapter.request.model_copy(
            update={"reviewed_plan_sha256": next_plan.plan_sha256}
        )
        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=self.host,
        ):
            fresh = self.run_recovery(next_adapter)
        self.assertEqual(fresh.status, "completed")
        self.assertTrue(published_proof_is_fresh(fresh.response_payload["result"]))

    def test_wrong_actor_preparation_publication_or_authority_refuses(self) -> None:
        with self.assertRaises(ValueError):
            replace(self.adapter, actor="another-admin").current()
        with self.assertRaises(ValueError):
            replace(
                self.adapter,
                request=self.adapter.request.model_copy(
                    update={
                        "preparation_plan_sha256": "d" * 64,
                    }
                ),
            ).current()
        changed = self.publication.publication.inventory.model_copy(
            update={"updated_at": "changed"}
        )
        self.store.write_environment_inventory(changed)
        with self.assertRaises(ValueError):
            self.adapter.current()

    def test_published_managed_ports_can_match_the_current_recipient(self) -> None:
        self.fixture.values.update(
            {key: str(value) for key, value in self.record.request.ports.items()}
        )
        _, plan = self.adapter.current()
        spec = self.adapter.host_spec(self.record, plan)
        expected_mount = self.fixture.document["services"][self.record.request.application_service][
            "volumes"
        ][0]
        self.assertEqual(
            cast(dict[str, Any], spec["mounts"])["config"]["destination"],
            expected_mount.split(":")[1],
        )
        with self.assertRaises(ValueError):
            runtime_values(self.store, self.fixture.root, self.record)

    def test_authority_changes_between_last_read_and_atomic_write_are_refused(self) -> None:
        writer = self.store.write_production_recipient

        def racing_write(**kwargs: Any) -> None:
            if any(
                key.startswith("published_recovery:") for key in kwargs["replacement"]["execution"]
            ):
                self.store.write_environment_inventory(
                    self.publication.publication.inventory.model_copy(
                        update={"updated_at": "raced"}
                    )
                )
            writer(**kwargs)

        with (
            patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                side_effect=self.host,
            ),
            patch.object(self.store, "write_production_recipient", side_effect=racing_write),
        ):
            terminal = self.run_recovery()
            self.assertEqual(terminal.status, "completed")
            self.assertFalse(terminal.response_payload["result"]["recovered"])
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_lost_reply_adopts_only_terminal_host_witness_without_recapture(self) -> None:
        def uncertain(_root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
            self.calls.append(spec)
            if not spec.get("inspect"):
                checkpoint("capture_dispatched")
                raise RuntimeError("reply lost")
            if sum(bool(call.get("inspect")) for call in self.calls) == 1:
                raise RuntimeError("host has not finished yet")
            return self.evidence(spec)

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=uncertain,
        ):
            self.assertEqual(self.run_recovery().status, "reconcile_required")
            adopted = self.run_recovery()
        self.assertEqual(adopted.status, "adopted")
        self.assertEqual(sum(not call.get("inspect") for call in self.calls), 1)
        self.assertTrue(adopted.response_payload["result"]["recovered"])

    def test_known_terminal_failure_finishes_without_holding_the_production_target(self) -> None:
        def refused(_root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
            self.calls.append(spec)
            if not spec.get("inspect"):
                checkpoint("host_preflight")
                raise RuntimeError("insufficient space")
            return {
                "recipient_recovery_binding": spec["recovery_binding"],
                "recipient_recovery_operation": spec.get("operation", ""),
                "recipient_recovery_retry_safe": "true",
            }

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program", side_effect=refused
        ):
            result = self.run_recovery()
            self.assertEqual(result.status, "completed")
            self.assertFalse(result.response_payload["result"]["recovered"])
            self.assertEqual(self.store.list_held_provider_target_reservations(), ())
            self.assertEqual(self.run_recovery().status, "replayed")
        self.assertEqual(sum(not call.get("inspect") for call in self.calls), 1)

    def test_first_owner_survives_terminal_failure_and_refuses_another_key(self) -> None:
        def refused(_root: Any, record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
            owner = record.execution["published_recovery_owner:" + spec["recovery_binding"]]
            self.assertEqual(owner["original_key"], self.adapter.original_key)
            self.assertEqual(owner["operation"], spec["operation"])
            checkpoint("original_attempt")
            return {
                "recipient_recovery_binding": spec["recovery_binding"],
                "recipient_recovery_operation": spec["operation"],
                "recipient_recovery_retry_safe": "true",
            }

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program", side_effect=refused
        ) as host:
            self.assertEqual(self.run_recovery().status, "completed")
            other = replace(self.adapter, original_key="another-key-same-plan")
            with self.assertRaises(ValueError):
                other.current()
            with self.assertRaises(ValueError):
                self.run_recovery(other)
            self.assertEqual(host.call_count, 2)  # apply plus fixed terminal read only
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_custody_append_collision_merges_without_discarding_current_proof(self) -> None:
        writer = self.store.write_production_recipient
        collided = False

        def collision(**kwargs: Any) -> None:
            nonlocal collided
            if not collided and any(
                key.startswith("published_recovery:") for key in kwargs["replacement"]["execution"]
            ):
                collided = True
                expected = kwargs["expected"]
                writer(
                    expected=expected,
                    replacement={
                        **expected,
                        "execution": {**expected["execution"], "unrelated-witness": {"done": True}},
                    },
                )
            writer(**kwargs)

        with (
            patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                side_effect=self.host,
            ),
            patch.object(self.store, "write_production_recipient", side_effect=collision),
        ):
            result = self.run_recovery()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.response_payload["result"]["recovered"])
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertTrue(saved.execution["unrelated-witness"]["done"])
        self.assertTrue(saved.published_recovery_proofs)

    def test_failed_proof_persistence_preserves_owner_and_excludes_other_key(self) -> None:
        writer = self.store.write_production_recipient

        def broken(**kwargs: Any) -> None:
            if any(
                key.startswith("published_recovery:") for key in kwargs["replacement"]["execution"]
            ):
                raise RuntimeError("database temporarily unavailable")
            writer(**kwargs)

        with (
            patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                side_effect=self.host,
            ),
            patch.object(self.store, "write_production_recipient", side_effect=broken),
        ):
            result = self.run_recovery()
        self.assertEqual(result.status, "reconcile_required")
        self.assertTrue(self.store.list_held_provider_target_reservations())
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)
        other = replace(self.adapter, original_key="other-key-after-storage-failure")
        with self.assertRaises(ValueError):
            other.current()
        with patch("control_plane.production_recipient_recovery.run_recipient_program") as host:
            self.assertEqual(self.run_recovery(other).status, "target_busy")
        host.assert_not_called()
        owner = saved.execution[
            "published_recovery_owner:" + self.adapter.request.reviewed_plan_sha256
        ]
        self.assertEqual(owner["original_key"], self.adapter.original_key)

    def assert_terminal_clock_refusal(self, *, future_completion: bool) -> None:
        evidence: dict[str, str] = {}

        def terminal(_root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
            checkpoint("terminal_capture")
            if not evidence:
                evidence.update(self.evidence(spec))
                if future_completion:
                    evidence["recipient_recovery_epoch"] = str(
                        int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp() * 1000)
                    )
                else:
                    published = datetime.fromisoformat(self.record.publication["published_at"])
                    evidence["recipient_recovery_started"] = str(
                        int(published.timestamp() * 1000) - 1
                    )
            return dict(evidence)

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=terminal,
        ):
            first = self.run_recovery()
        self.assertEqual(first.status, "completed")
        self.assertFalse(first.response_payload["result"]["recovered"])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)
        owner = saved.execution[
            "published_recovery_owner:" + self.adapter.request.reviewed_plan_sha256
        ]
        self.assertEqual(owner["original_key"], self.adapter.original_key)
        other = replace(self.adapter, original_key="another-key-after-clock-refusal")
        with self.assertRaises(ValueError):
            other.current()
        with self.assertRaises(ValueError):
            self.run_recovery(other)
        with (
            patch(
                "control_plane.production_recipient_published_recovery.datetime", wraps=datetime
            ) as clock,
            patch("control_plane.production_recipient_recovery.run_recipient_program") as transport,
        ):
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=2)
            replay = self.run_recovery()
        self.assertEqual(replay.status, "replayed")
        self.assertEqual(replay.response_payload, first.response_payload)
        transport.assert_not_called()

    def test_future_terminal_capture_completes_without_qualification(self) -> None:
        self.assert_terminal_clock_refusal(future_completion=True)

    def test_prepublication_terminal_capture_completes_without_qualification(self) -> None:
        self.assert_terminal_clock_refusal(future_completion=False)

    def test_stopped_create_custody_completes_and_replays_without_a_new_capture(self) -> None:
        retained: dict[str, str] = {}

        def stopped(_root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
            checkpoint("create_only_terminal")
            retained.update(
                {
                    "container": "lp-recovery-" + spec["operation"][:32],
                    "id": "",
                    "path": "/fixture/private-scratch",
                    "disposition": "stopped_create_unconfirmed",
                }
            )
            return {
                "recipient_recovery_binding": spec["recovery_binding"],
                "recipient_recovery_operation": spec["operation"],
                "recipient_recovery_retry_safe": "true",
                "recipient_recovery_retained": json.dumps(retained).encode().hex(),
            }

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program", side_effect=stopped
        ) as host:
            first = self.run_recovery()
            calls = host.call_count
            self.assertEqual(self.run_recovery().response_payload, first.response_payload)
            self.assertEqual(host.call_count, calls)
        self.assertEqual(first.status, "completed")
        self.assertFalse(first.response_payload["result"]["recovered"])
        self.assertEqual(first.response_payload["result"]["retained_scratch"], retained)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def test_second_generation_cannot_dispatch_while_first_target_is_held(self) -> None:
        other = replace(
            self.adapter,
            original_key="another-operation",
            request=self.adapter.request.model_copy(
                update={
                    "generation": "another-generation",
                }
            ),
        )
        outcomes = []

        def concurrent(root: Any, record: Any, spec: Any, program: Any, checkpoint: Any) -> Any:
            _, plan = other.current()
            other.request = other.request.model_copy(
                update={"reviewed_plan_sha256": plan.plan_sha256}
            )
            outcomes.append(self.run_recovery(other))
            return self.host(root, record, spec, program, checkpoint)

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=concurrent,
        ):
            self.assertEqual(self.run_recovery().status, "completed")
        self.assertEqual(outcomes[0].status, "target_busy")
