from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from control_plane import production_recipient_retirement as retirement
from control_plane.contracts.production_consumer_reads import (
    ConsumerReadEvidence,
    ConsumerTableRole,
    ProductionConsumerReadResult,
    ProductionConsumerReview,
    ReviewedOdooConsumer,
)
from control_plane.contracts.production_recipient import RecipientRecoveryPlan
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.dokploy import api
from control_plane.production_recipient_recovery import RecipientRecoveryAdapter
from control_plane.production_consumer_reads import QualifiedSourceCycle
from control_plane.production_recipient import custody, runtime_values
from control_plane.production_recipient_retirement_names import require_unretired_provider_name
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient_publication as publication_tests


class RecipientRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.publication = publication_tests.RecipientPublicationTests()
        with patch.object(self.publication, "customize_preparation", side_effect=self.customize):
            self.publication.setUp()
        self.addCleanup(self.publication.doCleanups)
        self.fixture = self.publication.fixture
        self.store = self.fixture.store
        self.now = datetime.now(timezone.utc)
        self.published = self.now - retirement.ROLLBACK_WINDOW - timedelta(minutes=2)
        with patch(
            "control_plane.production_recipient_publication.utc_now_timestamp",
            return_value=self.published.isoformat(),
        ):
            self.record = self.publication.publish()
        self.request = retirement.RecipientRetirementRequest(
            context=self.record.context,
            preparation_plan_sha256=self.record.plan_sha256,
            publication_sha256=self.record.publication["published_sha256"],
            recovery_generation="post-window",
            backup_record_id="current-backup",
            reason="Retire the exact old placement",
        )
        self.member = ReviewedOdooConsumer(product="consumer", environment="prod")
        review = ProductionConsumerReview(
            consumers=(self.member,),
            coordination_reference="reviewed-coordination",
            required_populated_roles=(ConsumerTableRole.INVOICE,),
        )
        self.state = SimpleNamespace(
            cycle=QualifiedSourceCycle("post-window-health", self.now, self.now, self.now),
            review=review,
        )
        self.consumers = ProductionConsumerReadResult(
            status="verified",
            observed_at=self.now.isoformat(),
            completed_at=self.now.isoformat(),
            publication_sha256=self.request.publication_sha256,
            consumers=(
                ConsumerReadEvidence(
                    consumer=self.member,
                    status="verified",
                    query="verified",
                    populated="verified",
                    destination="verified",
                ),
            ),
        )
        self.recovery = RecipientRecoveryPlan(
            product=self.record.product,
            context=self.record.context,
            preparation_plan_sha256=self.record.plan_sha256,
            publication_sha256=self.request.publication_sha256,
            plan_sha256="a" * 64,
            purpose="published",
            generation="post-window",
            recovered=True,
            captured_database_empty=False,
            capture_started_at=self.now.isoformat(),
            recovery_fresh_until=(self.now + timedelta(minutes=10)).isoformat(),
        )
        self.resources: list[dict[str, Any]] = [
            {
                "name": name,
                "identity": str(index + 1) * 64,
                "engine_identity": "3" * 64,
                "held": False,
            }
            for index, name in enumerate(("old-config", "old-database"))
        ]
        self.removed: set[str] = set()
        self.effects: list[dict[str, Any]] = []
        self.volume_calls: list[tuple[str, str]] = []
        self.deleted = False
        self.lose_delete_reply = False
        self.lose_volume_reply = False
        self.unknown_volume = False
        patches = (
            patch.object(retirement, "current_source", side_effect=lambda *a, **k: self.state),
            patch.object(
                retirement, "qualify_consumer_reads", side_effect=lambda **k: self.consumers
            ),
            patch.object(
                RecipientRecoveryAdapter,
                "current",
                side_effect=lambda: (self.record, self.recovery),
            ),
            patch.object(retirement, "require_retirement_protection"),
            patch.object(retirement, "_inventory_clear", return_value=True),
            patch.object(api, "list_dokploy_schedules", return_value=[]),
            patch.object(retirement, "inspect_retirement_resources", side_effect=self.inspect),
            patch.object(api, "fetch_dokploy_target_payload", side_effect=self.fetch),
            patch.object(api, "dokploy_request", side_effect=self.delete),
            patch.object(retirement, "_host_result", side_effect=self.volume),
        )
        for item in patches:
            self.enterContext(item)
        self.adapter = retirement.RecipientRetirementAdapter(
            self.fixture.root,
            self.store,
            self.record.product,
            self.request,
            self.record.actor,
            self.record.scope,
            "retirement-fixture",
            "retire-original",
            lambda *a: True,
        )
        record, manifest, authority, _ = self.adapter.current()
        self.adapter.request = self.request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": self.adapter.digest(record, manifest, authority),
            }
        )

    def customize(self) -> None:
        fixture = self.publication.fixture
        fixture.provider[fixture.old_id.target_id].update(
            composeType="docker-compose",
            autoDeploy=False,
            backups=[],
            composeFile='{"services":{"web":{}},"volumes":{"config":{"name":"old-config"},"database":{"name":"old-database"}}}',
        )

    def inspect(self, root: Path, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_absent": self.deleted,
            "containers": [],
            "volumes": [deepcopy(row) for row in self.resources if row["name"] not in self.removed],
            "engine_identity": "3" * 64,
        }

    def fetch(self, **kwargs: Any) -> Any:
        if kwargs["target_id"] == self.record.retained_target_id.target_id and self.deleted:
            raise DokployRequestFailed(
                method="GET", path="/fixture", detail="absent", status_code=404
            )
        return deepcopy(self.fixture.provider[kwargs["target_id"]])

    def delete(self, **kwargs: Any) -> Any:
        self.effects.append(kwargs["payload"])
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertEqual(saved.phase, "retiring_old")
        self.assertTrue(saved.execution["retirement_started_at"])
        self.assertFalse(kwargs["payload"]["deleteVolumes"])
        self.deleted = True
        if self.lose_delete_reply:
            self.lose_delete_reply = False
            raise TimeoutError("lost reply")
        return {"composeId": self.record.retained_target_id.target_id}

    def volume(
        self,
        root: Path,
        binding: Any,
        mode: str,
        key: str,
        lease: Any = None,
        *,
        retirement: Any = None,
    ) -> tuple[str, str]:
        self.volume_calls.append((mode, binding.volume_name))
        if self.unknown_volume:
            return "unknown", "unknown"
        if mode == "readback":
            return (
                ("absent", "removed")
                if binding.volume_name in self.removed
                else ("held", "never_started")
            )
        self.assertTrue(self.deleted)
        self.assertEqual(retirement["binding"], self.adapter.request.reviewed_plan_sha256)
        lease.checkpoint_effect("retirement_volume")
        self.removed.add(binding.volume_name)
        if self.lose_volume_reply:
            self.lose_volume_reply = False
            raise TimeoutError("lost volume reply")
        return "removed", "removed"

    def run_retirement(self, adapter: Any = None) -> Any:
        selected = adapter or self.adapter
        return run_durable_provider_operation(
            store=self.store,
            scope=selected.scope,
            route_path=retirement.RETIREMENT_ROUTE.format(product=selected.product),
            idempotency_key=selected.original_key,
            request_fingerprint="b" * 64,
            lease_owner="retirement-worker",
            response_trace_id=selected.trace_id,
            adapter=selected,
        )

    def test_exact_retirement_ends_fallback_preserves_custody_and_replays(self) -> None:
        original_recovery = deepcopy(self.record.recovery_proof)
        first = self.run_retirement()
        self.assertEqual(first.status, "completed")
        self.assertTrue(first.response_payload["result"]["completed"])
        saved = custody(self.store, self.record.product, self.record.context)
        assert saved is not None
        self.assertEqual(saved.recovery_proof, original_recovery)
        self.assertEqual(saved.publication, self.record.publication)
        for phase in ("retiring_old", "old_retired"):
            with self.subTest(phase=phase):
                values = runtime_values(
                    self.store,
                    self.fixture.root,
                    saved.model_copy(update={"phase": phase}),
                    published=True,
                )
                self.assertEqual(
                    {values[key] for key in saved.request.volume_keys.values()},
                    set(saved.volumes.values()),
                )
        self.assertEqual(self.removed, {row["name"] for row in self.resources})
        self.assertEqual(self.run_retirement().response_payload, first.response_payload)
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(ValueError):
            retirement.require_before_retirement(saved)
        with self.assertRaises(ValueError):
            require_unretired_provider_name(self.store, self.record.retained_target.target_name)
        require_unretired_provider_name(self.store, "a-new-project")
        with self.assertRaises(ValueError):
            replace(self.adapter, original_key="another-key").current()

    def test_changed_publication_foreign_actor_and_pre_window_refuse_before_effects(self) -> None:
        for request, actor in (
            (
                self.adapter.request.model_copy(update={"publication_sha256": "f" * 64}),
                self.record.actor,
            ),
            (self.adapter.request, "foreign-actor"),
        ):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                replace(self.adapter, request=request, actor=actor).current()
        with (
            patch.object(
                retirement, "ROLLBACK_WINDOW", retirement.ROLLBACK_WINDOW + timedelta(minutes=3)
            ),
            self.assertRaises(ValueError),
        ):
            self.adapter.current()
        self.assertFalse(self.effects)

    def test_missing_post_window_sync_consumer_or_recovery_refuses(self) -> None:
        self.state.cycle = replace(self.state.cycle, started=self.published)
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.state.cycle = replace(self.state.cycle, started=self.now)
        self.consumers = self.consumers.model_copy(update={"consumers": ()})
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.consumers = self.consumers.model_copy(
            update={
                "consumers": (
                    ConsumerReadEvidence(
                        consumer=self.member,
                        status="verified",
                        query="verified",
                        populated="verified",
                        destination="verified",
                    ),
                )
            }
        )
        self.recovery = self.recovery.model_copy(update={"recovered": False})
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.assertFalse(self.effects)

    def test_current_protection_and_provider_drift_refuse(self) -> None:
        with (
            patch.object(
                retirement,
                "require_retirement_protection",
                side_effect=ValueError("stale protection"),
            ),
            self.assertRaises(ValueError),
        ):
            self.adapter.current()
        self.fixture.provider[self.record.retained_target_id.target_id]["serverId"] = "foreign-host"
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.assertFalse(self.effects)

    def test_new_monitor_reference_protects_old_compose(self) -> None:
        original = self.record.publication["before"]["health_endpoints"][0]
        self.store.write_private_health_endpoint_record(
            PrivateHealthEndpointRecord.model_validate(
                {**original, "endpoint_key": "another-live-monitor"}
            )
        )
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.assertFalse(self.effects)

    def test_testing_settings_and_foreign_volume_ownership_retain_volumes(self) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="testing-context",
                instance="testing",
                env={"DB_VOLUME": "old-database"},
                updated_at=self.now.isoformat(),
            )
        )
        self.resources[0]["held"] = True
        record, manifest, authority, _ = self.adapter.current()
        self.adapter.request = self.adapter.request.model_copy(
            update={"reviewed_plan_sha256": self.adapter.digest(record, manifest, authority)}
        )
        result = self.run_retirement()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.response_payload["result"]["compose_removed"])
        self.assertEqual(
            {row["status"] for row in result.response_payload["result"]["volumes"]}, {"held"}
        )
        self.assertFalse(self.volume_calls)
        self.assertFalse(self.removed)

    def test_lost_compose_reply_is_observed_without_second_delete(self) -> None:
        self.lose_delete_reply = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertEqual(self.run_retirement().status, "completed")
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.removed), len(self.resources))

    def test_lost_volume_reply_adopts_exact_original_step_and_continues(self) -> None:
        self.lose_volume_reply = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertEqual(self.run_retirement().status, "completed")
        for row in self.resources:
            self.assertEqual(self.volume_calls.count(("apply", row["name"])), 1)
        self.assertEqual(len(self.effects), 1)

    def test_unknown_volume_never_repeats_removal_or_releases_original_custody(self) -> None:
        self.unknown_volume = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertEqual([mode for mode, name in self.volume_calls].count("apply"), 1)
        with self.assertRaises(ValueError):
            replace(self.adapter, original_key="replacement-key").current()
        self.assertEqual(len(self.effects), 1)

    def test_authority_drift_after_partial_completion_keeps_original_fence(self) -> None:
        self.lose_delete_reply = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        target = self.store.read_dokploy_target_record(
            context_name=self.record.context, instance_name="prod"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"target_name": "changed-current-placement"})
        )
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertFalse(self.volume_calls)
        self.assertEqual(len(self.effects), 1)
