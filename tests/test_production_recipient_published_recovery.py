"""Published recovery keeps historical bootstrap proof and owns its exact capture."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient_publication as publication_tests


class PublishedRecoveryTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.publication = publication_tests.RecipientPublicationTests()
        self.publication.database_url = self.database_url
        self.publication.setUp()
        self.addCleanup(self.publication.doCleanups)
        self.fixture = self.publication.fixture
        self.store = self.fixture.store
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

    def evidence(self, spec: dict[str, Any]) -> dict[str, str]:
        now = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        return {
            "recipient_recovery_ok": "true",
            "recipient_recovery_empty": "false",
            "recipient_recovery_binding": spec["recovery_binding"],
            "recipient_recovery_capture": "a" * 64,
            "recipient_recovery_source": "b" * 64,
            "recipient_recovery_data": "c" * 64,
            "recipient_recovery_epoch": now,
            "recipient_recovery_started": now,
            "recipient_recovery_duration_ms": "25",
        }

    def host(self, root: Any, record: Any, spec: Any, program: Any, checkpoint: Any) -> Any:
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

        class LaterClock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> "LaterClock":
                return cls.fromtimestamp(future.timestamp(), timezone.utc)

        with (
            patch("control_plane.production_recipient_published_recovery.datetime", LaterClock),
            patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                return_value=evidence,
            ) as host,
        ):
            # The checkpoint still belongs to this original operation.
            def witnessed(root: Any, record: Any, spec: Any, program: Any, checkpoint: Any) -> Any:
                checkpoint("historical_capture")
                return evidence

            host.side_effect = witnessed
            self.assertEqual(self.run_recovery().status, "completed")
            historical = self.adapter.current()[1]
        self.assertTrue(historical.recovered)
        with patch("control_plane.production_recipient_published_recovery.datetime", LaterClock):
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
        self.assertEqual(cast(dict[str, Any], spec["mounts"])["config"]["destination"], "/config")
        with self.assertRaises(ValueError):
            runtime_values(self.store, self.fixture.root, self.record)

    def test_authority_changes_between_last_read_and_atomic_write_are_refused(self) -> None:
        writer = self.store.write_production_recipient

        def racing_write(**kwargs: Any) -> None:
            self.store.write_environment_inventory(
                self.publication.publication.inventory.model_copy(update={"updated_at": "raced"})
            )
            writer(**kwargs)

        with (
            patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                side_effect=self.host,
            ),
            patch.object(self.store, "write_production_recipient", side_effect=racing_write),
        ):
            self.assertEqual(self.run_recovery().status, "reconcile_required")
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertFalse(saved.published_recovery_proofs)

    def test_lost_reply_adopts_only_terminal_host_witness_without_recapture(self) -> None:
        def uncertain(root: Any, record: Any, spec: Any, program: Any, checkpoint: Any) -> Any:
            self.calls.append(spec)
            if not spec.get("inspect"):
                checkpoint("capture_dispatched")
                raise RuntimeError("reply lost")
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
