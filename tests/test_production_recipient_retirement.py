from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import json
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
from control_plane.production_recipient import custody, runtime_values, read_provider_compose
from control_plane.production_recipient_retirement_names import require_unretired_provider_name
from control_plane.production_recipient_retirement_names import (
    provider_name_guard,
    require_available_name,
)
from control_plane.dokploy_inventory import TARGET_COLLECTIONS
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.retained_volume_sweep_execution import compose_volume_names, _inventory_clear
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient_publication as publication_tests


class RecipientRetirementTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.publication = publication_tests.RecipientPublicationTests()
        self.publication.database_url = self.database_url
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
            patch.object(
                retirement,
                "require_retirement_protection",
                return_value=(self.now + timedelta(minutes=10)).timestamp(),
            ),
            patch.object(api, "fetch_dokploy_projects", side_effect=self.projects),
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
        project = fixture.context + "-prod-a1b2c3"
        fixture.old = fixture.old.model_copy(update={"target_name": project})
        fixture.store.write_dokploy_target_record(fixture.old)
        fixture.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=fixture.old, target_id_record=fixture.old_id
            )
        )
        fixture.provider[fixture.old_id.target_id].update(
            appName=project,
            composeType="docker-compose",
            autoDeploy=False,
            backups=[],
            mounts=[],
            composeFile=json.dumps(fixture.document),
            env="DB_VOLUME=old-database\nCONFIG_VOLUME=old-config",
        )

    def projects(self, **_kwargs: Any) -> tuple[dict[str, Any], ...]:
        environment: dict[str, Any] = {kind: [] for kind in TARGET_COLLECTIONS}
        environment["env"] = ""
        environment["compose"] = [
            {"composeId": identifier}
            for identifier in self.fixture.provider
            if not (self.deleted and identifier == self.record.retained_target_id.target_id)
        ]
        return ({"name": self.record.product, "env": "", "environments": [environment]},)

    def inspect(self, _root: Path, _spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_absent": self.deleted,
            "containers": [],
            "volumes": [deepcopy(row) for row in self.resources if row["name"] not in self.removed],
            "engine_identity": "3" * 64,
            "boot_id": "fixture-boot",
            "monotonic": 100,
            "received_at": datetime.now(timezone.utc).timestamp(),
        }

    def fetch(self, **kwargs: Any) -> Any:
        if kwargs["target_id"] == self.record.retained_target_id.target_id and self.deleted:
            raise DokployRequestFailed(
                method="GET", path="/fixture", detail="absent", status_code=404
            )
        payload = deepcopy(self.fixture.provider[kwargs["target_id"]])
        payload.setdefault("mounts", [])
        return payload

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
        _root: Path,
        binding: Any,
        mode: str,
        _key: str,
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
        assert retirement is not None and lease is not None
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
            request_fingerprint=retirement.retirement_request_fingerprint(
                selected.product, selected.request
            ),
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

    def test_read_authorized_other_actor_can_plan_but_cannot_apply(self) -> None:
        planned = replace(
            self.adapter,
            actor="another-reader",
            scope="another-scope",
            original_key="",
            request=self.adapter.request.model_copy(
                update={"mode": "dry-run", "reviewed_plan_sha256": ""}
            ),
        )
        record, manifest, authority, _ = planned.current()
        self.assertTrue(
            planned.plan(record, manifest, planned.digest(record, manifest, authority)).plan_sha256
        )
        with self.assertRaises(ValueError):
            replace(planned, request=planned.request.model_copy(update={"mode": "apply"})).current()
        self.assertFalse(self.effects)
        self.unknown_volume = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        record, manifest, authority, _ = planned.current()
        result = planned.plan(record, manifest, planned.digest(record, manifest, authority))
        self.assertTrue(any(row.status == "unknown" for row in result.volumes))

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

    def test_competing_key_cannot_enter_while_original_delete_is_running(self) -> None:
        original_delete = self.delete

        def competing(**kwargs: Any) -> Any:
            self.assertEqual(
                self.run_retirement(
                    replace(self.adapter, original_key="competing-retirement")
                ).status,
                "target_busy",
            )
            return original_delete(**kwargs)

        with patch.object(api, "dokploy_request", side_effect=competing):
            self.assertEqual(self.run_retirement().status, "completed")
        self.assertEqual(len(self.effects), 1)

    def test_reviewed_environment_volume_names_require_unique_qualified_inputs(self) -> None:
        payload = self.fixture.provider[self.record.retained_target_id.target_id]
        document = read_provider_compose(payload["composeFile"])
        self.assertEqual(
            set(compose_volume_names(document, payload)), {row["name"] for row in self.resources}
        )
        for environment in (
            "DB_VOLUME=old-database",
            "DB_VOLUME=old-database\nCONFIG_VOLUME=one\nCONFIG_VOLUME=two",
            'DB_VOLUME=old-database\nCONFIG_VOLUME="$UNRESOLVED"',
        ):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                compose_volume_names(document, {**payload, "env": environment})

    def test_other_compose_resolved_volume_reference_is_held(self) -> None:
        payload = self.fixture.provider[self.record.target_id]
        key = self.record.request.volume_keys["database"]
        values = api.parse_dokploy_env_text(payload["env"])
        values[key] = "old-database"
        from control_plane.dokploy.environment import serialize_dokploy_env_text

        payload["env"] = serialize_dokploy_env_text(values, target_type="compose")
        result = self.run_retirement()
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.removed, {"old-config"})
        self.assertEqual(
            {row["status"] for row in result.response_payload["result"]["volumes"]},
            {"held", "absent"},
        )

    def test_other_compose_environment_alias_protects_old_project_files(self) -> None:
        self.fixture.provider["other-compose"] = {
            "composeId": "other-compose",
            "appName": "independent-project",
            "mounts": [],
            "env": "OLD_PROJECT="
            + self.record.retained_target.target_name
            + "\nUNRELATED=preserve",
            "composeFile": json.dumps(
                {"services": {"worker": {"env_file": "/etc/dokploy/compose/${OLD_PROJECT}/.env"}}}
            ),
        }
        with self.assertRaises(ValueError):
            self.adapter.current()
        self.assertFalse(self.effects)

    def test_shared_provider_environment_protects_old_project_files(self) -> None:
        self.fixture.provider["other-compose"] = {
            "composeId": "other-compose",
            "appName": "independent-project",
            "mounts": [],
            "env": "OLD_PROJECT=${{project.OLD_PROJECT}}",
            "composeFile": json.dumps(
                {"services": {"worker": {"env_file": "/etc/dokploy/compose/${OLD_PROJECT}/.env"}}}
            ),
        }
        for scope in ("project", "environment"):

            def projects(**kwargs: Any) -> tuple[dict[str, Any], ...]:
                rows = self.projects(**kwargs)
                owner = rows[0] if scope == "project" else rows[0]["environments"][0]
                owner["env"] = "OLD_PROJECT=" + self.record.retained_target.target_name
                return rows

            with (
                self.subTest(scope=scope),
                patch.object(api, "fetch_dokploy_projects", side_effect=projects),
                self.assertRaises(ValueError),
            ):
                self.adapter.current()
        self.assertFalse(self.effects)

    def test_missing_shared_provider_environment_refuses_retirement(self) -> None:
        for scope in ("project", "environment"):

            def projects(**kwargs: Any) -> tuple[dict[str, Any], ...]:
                rows = self.projects(**kwargs)
                owner = rows[0] if scope == "project" else rows[0]["environments"][0]
                del owner["env"]
                return rows

            with (
                self.subTest(scope=scope),
                patch.object(api, "fetch_dokploy_projects", side_effect=projects),
                self.assertRaises(ValueError),
            ):
                self.adapter.current()
        self.assertFalse(self.effects)

    def test_new_stopped_host_mount_after_admission_is_protected(self) -> None:
        original_delete = self.delete

        def shared(**kwargs: Any) -> Any:
            result = original_delete(**kwargs)
            self.resources[0]["held"] = True
            return result

        with patch.object(api, "dokploy_request", side_effect=shared):
            self.assertEqual(self.run_retirement().status, "completed")
        self.assertEqual(self.removed, {"old-database"})
        self.assertNotIn(("apply", "old-config"), self.volume_calls)

    def test_rebind_before_retirement_commit_is_detected_before_delete(self) -> None:
        original_write = self.store.write_production_recipient

        def rebind(**kwargs: Any) -> None:
            if (
                kwargs["expected"]["phase"] == "published"
                and kwargs["replacement"]["phase"] == "retiring_old"
            ):
                current = self.store.read_provider_target_record(
                    context_name=self.record.context, instance_name="prod"
                )
                self.store.write_provider_target_record(
                    current.model_copy(
                        update={
                            "context": "other-context",
                            "instance": "testing",
                            "target_id": self.record.retained_target_id.target_id,
                        }
                    )
                )
            original_write(**kwargs)

        with patch.object(self.store, "write_production_recipient", side_effect=rebind):
            self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertFalse(self.effects)

    def test_testing_volume_reference_added_during_provider_reads_is_protected(self) -> None:
        original_inventory = _inventory_clear
        inserted = False

        def add_reference(*args: Any, **kwargs: Any) -> bool:
            nonlocal inserted
            clear = original_inventory(*args, **kwargs)
            record = custody(self.store, self.record.product, self.record.context)
            if (
                not inserted
                and args[1].volume_name == "old-config"
                and record is not None
                and any(key.startswith("retirement_permit:") for key in record.execution)
            ):
                inserted = True
                self.store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="new-testing-reference",
                        instance="testing",
                        env={"DB_VOLUME": "old-config"},
                        updated_at=self.now.isoformat(),
                    )
                )
            return clear

        with patch.object(retirement, "_inventory_clear", side_effect=add_reference):
            self.assertEqual(self.run_retirement().status, "reconcile_required")
            self.assertTrue(inserted)
            self.assertNotIn("old-config", self.removed)
            self.assertEqual(self.run_retirement().status, "completed")
        self.assertEqual(self.removed, {"old-database"})

    def test_project_reference_added_during_provider_reads_prevents_compose_delete(self) -> None:
        original_inventory = _inventory_clear
        inserted = False

        def add_reference(*args: Any, **kwargs: Any) -> bool:
            nonlocal inserted
            clear = original_inventory(*args, **kwargs)
            record = custody(self.store, self.record.product, self.record.context)
            if not inserted and record is not None and record.phase == "retiring_old":
                inserted = True
                self.store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="new-testing-reference",
                        instance="testing",
                        env={"RETAINED_COMPOSE": record.retained_target_id.target_id},
                        updated_at=self.now.isoformat(),
                    )
                )
            return clear

        with patch.object(retirement, "_inventory_clear", side_effect=add_reference):
            self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertTrue(inserted)
        self.assertFalse(self.effects)

    def test_protection_expiring_during_provider_reads_prevents_compose_delete(self) -> None:
        original_inventory = _inventory_clear
        observed_time = [self.now]

        def expire_protection(*args: Any, **kwargs: Any) -> bool:
            clear = original_inventory(*args, **kwargs)
            record = custody(self.store, self.record.product, self.record.context)
            if record is not None and record.phase == "retiring_old":
                observed_time[0] = self.now + timedelta(seconds=3)
            return clear

        with (
            patch.object(retirement, "datetime", wraps=datetime) as clock,
            patch.object(
                retirement,
                "require_retirement_protection",
                return_value=(self.now + timedelta(seconds=2)).timestamp(),
            ),
            patch.object(retirement, "_inventory_clear", side_effect=expire_protection),
        ):
            clock.now.side_effect = lambda *a, **k: observed_time[0]
            self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertFalse(self.effects)

    def test_post_marker_direct_and_bundle_target_id_rebinds_refuse(self) -> None:
        self.assertEqual(self.run_retirement().status, "completed")
        old = self.record.retained_target_id.model_copy(
            update={"context": "other-context", "instance": "testing"}
        )
        with self.assertRaises(ValueError):
            self.store.write_dokploy_target_id_record(old)
        with self.assertRaises(ValueError):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(dokploy_target_ids=(old,))
            )
        current = self.store.read_provider_target_record(
            context_name=self.record.context, instance_name="prod"
        )
        self.store.write_provider_target_record(
            current.model_copy(update={"display_name": self.record.product})
        )

    def test_only_exact_or_generatable_old_provider_names_are_reserved(self) -> None:
        self.assertEqual(self.run_retirement().status, "completed")
        project = self.record.retained_target.target_name
        require_unretired_provider_name(self.store, self.record.product)
        with provider_name_guard(self.store, self.record.product):
            pass
        with (
            self.assertRaises(ValueError),
            provider_name_guard(self.store, project.rsplit("-", 1)[0]),
        ):
            self.fail("A creation base capable of reusing the retired project was admitted")
        payload = deepcopy(
            self.store.read_production_recipient(self.record.product, self.record.context)
        )
        assert payload is not None
        payload["execution"]["retirement_manifest"]["project"] = "app-" + project
        with patch.object(
            self.store, "list_production_recipient_payloads", return_value=(payload,)
        ):
            with (
                self.assertRaises(ValueError),
                provider_name_guard(self.store, "", generated_prefix="app-"),
            ):
                self.fail("Provider-generated names bypassed retained namespace custody")
            with provider_name_guard(self.store, "independent-explicit-base"):
                pass

    def test_retirement_reserves_old_target_without_holding_replacement_release_target(
        self,
    ) -> None:
        self.unknown_volume = True
        result = self.run_retirement()
        self.assertEqual(result.status, "reconcile_required")
        origin = self.adapter.provider_target_origin()
        self.assertEqual(origin.target_id, self.record.retained_target_id.target_id)
        self.assertNotEqual(origin.target_id, self.record.target_id)
        self.assertFalse(
            self.store.list_held_provider_target_reservations(
                provider_target_key=origin.model_copy(
                    update={"target_id": self.record.target_id}
                ).target_key()
            )
        )

    def test_pre_start_refusal_releases_reservation_and_same_key_can_retry(self) -> None:
        original = self.adapter.references
        count = 0

        def references(*args: Any) -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError("transient before start")
            original(*args)

        with (
            patch.object(type(self.adapter), "references", side_effect=references),
            self.assertRaises(ValueError),
        ):
            self.run_retirement()
        self.assertFalse(self.effects)
        self.assertEqual(self.run_retirement().status, "completed")

    def test_started_but_undispatched_can_continue_with_same_key(self) -> None:
        original = self.adapter.start

        def interrupted(*args: Any) -> Any:
            original(*args)
            raise TimeoutError("worker stopped before dispatch")

        with patch.object(type(self.adapter), "start", side_effect=interrupted):
            self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertFalse(self.effects)
        self.assertEqual(self.run_retirement().status, "completed")

    def test_fresh_evidence_can_replace_expired_references_under_original_key(self) -> None:
        self.lose_delete_reply = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.adapter.request = self.adapter.request.model_copy(
            update={
                "recovery_generation": "refreshed-recovery",
                "backup_record_id": "refreshed-protection",
            }
        )
        record, manifest, authority, _ = self.adapter.current()
        self.assertEqual(
            self.adapter.digest(record, manifest, authority),
            self.adapter.request.reviewed_plan_sha256,
        )
        self.assertEqual(self.run_retirement().status, "completed")

    def test_fenced_no_effect_volume_attempt_can_continue_under_original_key(self) -> None:
        original = self.volume
        first = True

        def transient(*args: Any, **kwargs: Any) -> Any:
            nonlocal first
            if args[2] == "apply" and first:
                first = False
                raise ValueError("guard refused before host dispatch")
            return original(*args, **kwargs)

        with patch.object(retirement, "_host_result", side_effect=transient):
            self.assertEqual(self.run_retirement().status, "reconcile_required")
            self.assertEqual(self.run_retirement().status, "completed")
        self.assertEqual(self.removed, {row["name"] for row in self.resources})

    def test_old_metadata_absence_without_host_absence_stays_unknown_without_redelete(self) -> None:
        self.lose_delete_reply = True
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.deleted = False
        self.assertEqual(self.run_retirement().status, "reconcile_required")
        self.assertEqual(len(self.effects), 1)
        self.assertFalse(self.volume_calls)

    def test_reserved_old_name_and_identity_cannot_be_rebound_after_completion(self) -> None:
        self.assertEqual(self.run_retirement().status, "completed")
        with (
            self.assertRaises(ValueError),
            provider_name_guard(self.store, self.record.retained_target.target_name),
        ):
            self.fail("retired name admitted")
        current = self.store.read_provider_target_record(
            context_name=self.record.context, instance_name="prod"
        )
        with self.assertRaises(ValueError):
            self.store.write_provider_target_record(
                current.model_copy(update={"target_id": self.record.retained_target_id.target_id})
            )
        require_available_name(({"execution": {}, "legacy": "unrelated"},), "new-project")

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


class RetirementProtectionTests(unittest.TestCase):
    def test_existing_backup_guard_and_post_window_capture_both_apply(self) -> None:
        import click
        from tests.test_production_promotion_backup import ProductionPromotionBackupTests
        from control_plane.workflows.production_promotion_backup import (
            GENERIC_WEB_PROMOTION_BACKUP_ACTION,
        )

        fixture = ProductionPromotionBackupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.capture(GENERIC_WEB_PROMOTION_BACKUP_ACTION)
        operation = store.list_verireel_prod_backup_gate_operation_records()[0]
        capture = store.read_backup_gate_record(operation.backup_record_id)
        record: Any = SimpleNamespace(product=operation.product, context=capture.context)
        request: Any = SimpleNamespace(backup_record_id=capture.record_id)
        oldest = min(
            datetime.fromisoformat(capture.evidence[key])
            for key in ("snapshot_finished_at", "independent_backup_finished_at")
        )
        deadline = retirement.require_retirement_protection(
            store, record, request, oldest - timedelta(seconds=1)
        )
        policy = store.read_production_backup_policy_record(capture.evidence["policy_record_id"])
        self.assertGreater(deadline, oldest.timestamp())
        self.assertLessEqual(
            deadline,
            datetime.fromisoformat(capture.evidence["snapshot_finished_at"]).timestamp()
            + policy.fast_snapshot.max_evidence_age_seconds,
        )
        self.assertLessEqual(
            deadline,
            datetime.fromisoformat(capture.evidence["independent_backup_finished_at"]).timestamp()
            + policy.independent_backup.max_evidence_age_seconds,
        )
        with self.assertRaises(ValueError):
            retirement.require_retirement_protection(
                store, record, request, oldest + timedelta(seconds=1)
            )
        store.write_backup_gate_record(capture.model_copy(update={"status": "fail"}))
        with self.assertRaises(click.ClickException):
            retirement.require_retirement_protection(
                store, record, request, oldest - timedelta(seconds=1)
            )
