"""Behavior and persisted-restart proof on isolated authoritative website data."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_cutover import (
    OdooCutoverPlan,
    OdooMaintenanceReceipt,
    OdooRecoveryTuple,
    OdooRouteObservation,
    OdooSlot,
    OdooSlotReadiness,
)
from control_plane.contracts.prepared_public_site import PublicRecoveryObservation
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import DeploymentEvidence, HealthcheckEvidence
from control_plane.odoo_cutover_storage import DeploymentCutoverStore
from control_plane.prepared_public_site import prepare_public_site
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_cutover import execute_odoo_cutover, rollback_compatible_cutover
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_release_compatibility import artifact, file


def release_plan(fixture: WebsiteFixture, database_changing: bool = False) -> OdooCutoverPlan:
    path = "addons/site/views/page.xml" if database_changing else "addons/site/static/site.css"
    kind = "database_data" if database_changing else "static"
    old = artifact("old", files=(file(path, kind),))
    new = artifact("new", files=(file(path, kind, digest="b"),))
    new.image.digest = "sha256:" + "b" * 64
    fixture.binding = fixture.binding.model_copy(
        update={
            "artifact_id": old.artifact_id,
            "image_digest": old.image.digest,
        }
    )
    fixture.plan = fixture.plan.model_copy(update={"binding": fixture.binding})
    slots = []
    for name, manifest in (("old", old), ("new", new)):
        slots.append(
            OdooSlot(
                slot_id=name,
                compose_id=name + "-project",
                private_url="http://" + name + ":8069",
                binding=fixture.binding.model_copy(
                    update={
                        "artifact_id": manifest.artifact_id,
                        "image_digest": manifest.image.digest,
                    }
                ),
                image=f"{manifest.image.repository}@{manifest.image.digest}",
                runtime_identity=RuntimeIdentity(
                    context="fixture",
                    instance="isolated",
                    deployment_record_id=name,
                    artifact_id=manifest.artifact_id,
                    source_git_ref=manifest.source_commit,
                    slot=name,
                    release_id=fixture.binding.release_id if name == "new" else "old-release",
                    database_uuid="fixture-uuid",
                    image_reference=f"{manifest.image.repository}@{manifest.image.digest}",
                    image_digest=manifest.image.digest,
                ),
            )
        )
    return OdooCutoverPlan(
        release_id=fixture.binding.release_id,
        route_id="fixture-route",
        host="fixture.invalid",
        production=old,
        candidate=new,
        compatibility=classify_release_database_compatibility(production=old, candidate=new),
        old=slots[0],
        new=slots[1],
        writers=fixture.plan.writers,
    )


class IsolatedCutoverProvider(WebsiteFixture):
    def __init__(self, root: Path, *, database_changing: bool = False) -> None:
        root.mkdir(parents=True, exist_ok=True)
        super().__init__(root)
        self.cutover = release_plan(self, database_changing)
        self.target = self.cutover.old
        self.actions: list[str] = []
        self.maintenance_count = 0
        self.fail_ready = False
        self.uncertain_switch = False
        self.uncertain_resume = False
        self.maintenance_receipt: OdooMaintenanceReceipt | None = None
        self.owners: dict[str, str] = {writer.writer_id: "old" for writer in self.plan.writers}
        self.drained = True

    def exclusive(self, plan: OdooCutoverPlan) -> Any:
        return nullcontext()

    def current_route(self, plan: OdooCutoverPlan) -> OdooRouteObservation:
        return OdooRouteObservation(
            route_id=plan.route_id,
            host=plan.host,
            target_url=self.target.private_url,
            slot_id=self.target.slot_id,
            binding=self.target.binding,
            evidence_id="fixture-applied-route",
            applied=True,
            authoritative_router_count=1,
            public_health=HealthcheckEvidence(
                verified=True,
                status="pass",
                urls=("http://fixture.invalid/launchplane/health",),
                timeout_seconds=10,
                observed_runtime_identity=self.target.runtime_identity,
                runtime_identity_status="match",
            ),
        )

    def prepare_candidate(self, plan: OdooCutoverPlan) -> None:
        self.actions.append("prepare")
        if self.target != plan.old:
            raise ValueError("preparation must leave old serving")

    def prepare_public(self, plan: OdooCutoverPlan) -> Any:
        self.actions.append("public")
        return prepare_public_site(self.plan, self)

    def publish(self, site: Any, pause_id: str) -> str:
        result = super().publish(site, pause_id)
        self.target = self.cutover.old.model_copy(
            update={
                "slot_id": "prepared",
                "private_url": "http://prepared:8080",
            }
        )
        return result

    def backup(self, plan: OdooCutoverPlan) -> OdooRecoveryTuple:
        self.actions.append("backup")
        if plan.compatibility.classification == "database_changing":
            if self.fenced != {writer.writer_id for writer in self.plan.writers}:
                raise RuntimeError("backup recovery point requires drained writers")
        return OdooRecoveryTuple(
            binding=plan.old.binding,
            image=plan.old.image,
            logical_record_id="logical-fixture",
            infrastructure_record_id="independent-fixture",
            database_dump_sha256="c" * 64,
            filestore_archive_sha256="d" * 64,
            recovery_point_at=datetime.now(UTC),
            coherence_verified=True,
            restore_verified=True,
            independent_backup_verified=True,
        )

    def maintenance(self, plan: OdooCutoverPlan) -> OdooMaintenanceReceipt:
        if self.maintenance_receipt:
            return self.maintenance_receipt
        self.actions.append("maintenance")
        self.maintenance_count += 1
        if plan.compatibility.classification == "database_changing":
            self.update_database()
        self.maintenance_receipt = OdooMaintenanceReceipt(
            release_id=plan.release_id,
            binding=plan.new.binding,
            image=plan.new.image,
            candidate_manifest_sha256=plan.compatibility.candidate_manifest_sha256,
            credential_commit_id="fixture-committed-guard",
            credential_commit_verified_before_registry=True,
            install_modules=plan.compatibility.install_modules,
            update_modules=plan.compatibility.update_modules,
            module_readback=True,
            evidence_id="fixture-targeted-maintenance",
        )
        return self.maintenance_receipt

    def readiness(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooSlotReadiness:
        self.actions.append("readiness")
        if self.fail_ready:
            raise ValueError("fixture candidate render failed")
        return OdooSlotReadiness(
            binding=slot.binding,
            slot_id=slot.slot_id,
            image=slot.image,
            host=plan.host,
            observed_at=datetime.now(UTC),
            evidence_id="fixture-private-probe",
            registry=True,
            pending_modules=False,
            render=True,
            assets=True,
            browser=True,
            side_effects_fenced=True,
            retained_previous_assets=True,
            runtime_identity=slot.runtime_identity,
        )

    def switch(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooRouteObservation:
        self.actions.append("switch")
        self.target = slot
        if self.uncertain_switch:
            self.uncertain_switch = False
            raise TimeoutError("fixture publication acknowledgement lost")
        return self.current_route(plan)

    def handoff_writers(self, plan: OdooCutoverPlan, slot: OdooSlot) -> None:
        self.actions.append("handoff")
        self.owners = {writer.writer_id: slot.slot_id for writer in plan.writers}

    def recover_and_resume(
        self, site: Any, pause_id: str, expected: Any
    ) -> PublicRecoveryObservation:
        if pause_id in self.resume_receipts:
            return self.resume_receipts[pause_id]
        self.handoff_writers(self.cutover, self.target)
        self.resume_count += 1
        self.fenced.clear()
        result = PublicRecoveryObservation(
            binding=expected,
            pause_id=pause_id,
            serving_evidence_id="fixture-resumed-serving",
            writer_owners=tuple(
                (writer.writer_id, self.owner_count) for writer in self.plan.writers
            ),
        )
        self.resume_receipts[pause_id] = result
        if self.uncertain_resume:
            self.uncertain_resume = False
            raise TimeoutError("fixture resumption acknowledgement lost")
        return result

    def drain_old(self, plan: OdooCutoverPlan) -> bool:
        self.actions.append("drain")
        return self.drained


def cutover_store(root: Path, *, database: bool = False) -> DeploymentCutoverStore:
    store = (
        PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root}/state.sqlite")
        if database
        else FilesystemRecordStore(root / "state")
    )
    if isinstance(store, PostgresRecordStore):
        store.ensure_schema()
    return DeploymentCutoverStore(
        store,
        DeploymentRecord(
            record_id="fixture-deployment",
            context="fixture",
            instance="isolated",
            source_git_ref="a" * 40,
            deploy=DeploymentEvidence(
                target_name="fixture-slot", target_type="compose", deploy_mode="two-slot"
            ),
        ),
    )


class OdooCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.provider = IsolatedCutoverProvider(self.root)
        self.addCleanup(self.provider.engine.dispose)
        self.store = cutover_store(self.root)
        self.record_id = "f" * 32

    def run_release(self, **arguments: Any) -> Any:
        return execute_odoo_cutover(
            self.provider.cutover, self.store, self.provider, record_id=self.record_id, **arguments
        )

    def database_changing(self) -> None:
        self.provider.engine.dispose()
        self.provider = IsolatedCutoverProvider(self.root / "db-change", database_changing=True)
        self.addCleanup(self.provider.engine.dispose)

    def test_compatible_keeps_old_through_preparation_and_can_switch_back(self) -> None:
        result = self.run_release()
        self.assertEqual(result.step, "complete")
        self.assertIsNone(result.pause)
        self.assertEqual(result.maintenance.update_modules, ())
        self.assertEqual(
            self.provider.owners, {writer.writer_id: "new" for writer in result.plan.writers}
        )
        restored = rollback_compatible_cutover(self.store, self.provider, self.record_id)
        assert restored.route is not None
        self.assertEqual(restored.route.binding, result.plan.old.binding)
        self.assertEqual(set(self.provider.owners.values()), {"old"})

    def test_uncertain_compatible_rollback_reads_authority_without_second_switch(self) -> None:
        self.run_release()
        self.provider.uncertain_switch = True
        with self.assertRaises(TimeoutError):
            rollback_compatible_cutover(self.store, self.provider, self.record_id)
        self.assertEqual(self.store.load(self.record_id).step, "rolling_back")
        switches = self.provider.actions.count("switch")
        result = rollback_compatible_cutover(self.store, self.provider, self.record_id)
        self.assertEqual(result.step, "rolled_back")
        self.assertEqual(self.provider.actions.count("switch"), switches)

    def test_corrupted_resume_cannot_skip_backup_or_writer_pause(self) -> None:
        self.database_changing()
        self.provider.fail_ready = True
        with self.assertRaises(ValueError):
            self.run_release()
        record = self.store.load(self.record_id)
        for changes in ({"backup": None}, {"pause": None}):
            self.store.save(record.model_copy(update=changes))
            with self.assertRaises(ValueError):
                self.run_release()
            self.assertNotIn("switch", self.provider.actions)

    def test_deployment_bridge_persists_runtime_identity_and_slot_rollback(self) -> None:
        from control_plane.workflows.odoo_cutover_deployment import (
            execute_odoo_cutover_deployment,
            execute_odoo_cutover_rollback_deployment,
        )

        records = self.store.store
        result = execute_odoo_cutover_deployment(
            plan=self.provider.cutover,
            record_store=records,
            provider=self.provider,
            context="fixture",
            instance="isolated",
            checkpoint=lambda _step: None,
        )
        deployment = records.read_deployment_record(result.deployment_record_id)
        self.assertEqual(deployment.runtime_identity, self.provider.cutover.new.runtime_identity)
        self.assertEqual(deployment.deploy.status, "pass")
        write = records.write_deployment_record

        def crash_after_switch(record: DeploymentRecord) -> object:
            if record.record_id.endswith("-switch-back"):
                raise RuntimeError("fixture crash after persisted switch-back")
            return write(record)

        with (
            patch.object(records, "write_deployment_record", side_effect=crash_after_switch),
            self.assertRaisesRegex(RuntimeError, "fixture crash"),
        ):
            execute_odoo_cutover_rollback_deployment(
                deployment=deployment,
                record_store=records,
                provider=self.provider,
                artifact_id=self.provider.cutover.production.artifact_id,
            )
        deployment = records.read_deployment_record(result.deployment_record_id)
        assert deployment.odoo_cutover is not None
        self.assertEqual(deployment.odoo_cutover.step, "rolled_back")
        switches = self.provider.actions.count("switch")
        rollback = execute_odoo_cutover_rollback_deployment(
            deployment=deployment,
            record_store=records,
            provider=self.provider,
            artifact_id=self.provider.cutover.production.artifact_id,
        )
        old = records.read_deployment_record(rollback.deployment_record_id)
        assert deployment.odoo_cutover.rollback_started_at is not None
        self.assertEqual(
            old.deploy.started_at, deployment.odoo_cutover.rollback_started_at.isoformat()
        )
        self.assertEqual(old.deploy.finished_at, deployment.odoo_cutover.updated_at.isoformat())
        self.assertEqual(self.provider.actions.count("switch"), switches)
        self.assertEqual(old.runtime_identity, self.provider.cutover.old.runtime_identity)
        assert old.artifact_identity is not None
        self.assertEqual(
            old.artifact_identity.artifact_id, self.provider.cutover.production.artifact_id
        )

    def test_failed_readiness_keeps_old_and_no_writer_handoff(self) -> None:
        self.provider.fail_ready = True
        with self.assertRaises(ValueError):
            self.run_release()
        self.assertEqual(self.provider.target, self.provider.cutover.old)
        self.assertNotIn("handoff", self.provider.actions)
        self.assertEqual(self.store.load(self.record_id).step, "ready")

    def test_uncertain_switch_reads_back_after_controller_restart_without_replay(self) -> None:
        self.provider.uncertain_switch = True
        with self.assertRaises(TimeoutError):
            self.run_release()
        self.assertEqual(self.store.load(self.record_id).step, "switching")
        self.store = cutover_store(self.root)
        result = self.run_release()
        self.assertEqual(result.step, "complete")
        self.assertEqual(self.provider.actions.count("switch"), 1)
        self.assertEqual(self.provider.maintenance_count, 1)

    def test_incomplete_plan_and_wrong_slot_refuse_before_any_effect(self) -> None:
        plan = self.provider.cutover.model_dump()
        plan["new"]["compose_id"] = plan["old"]["compose_id"]
        with self.assertRaises(ValueError):
            OdooCutoverPlan.model_validate(plan)
        plan = self.provider.cutover.model_dump()
        plan["compatibility"]["module_plan_complete"] = False
        with self.assertRaises(ValueError):
            OdooCutoverPlan.model_validate(plan)
        self.assertEqual(self.provider.actions, [])

    def test_stale_readiness_and_foreign_identity_never_switch(self) -> None:
        original = self.provider.readiness

        def stale(plan: Any, slot: Any) -> Any:
            return original(plan, slot).model_copy(
                update={"observed_at": datetime.now(UTC) - timedelta(minutes=2)}
            )

        self.provider.readiness = stale  # type: ignore[method-assign]
        with self.assertRaises(ValueError):
            self.run_release()
        self.assertNotIn("switch", self.provider.actions)

    def test_database_changing_full_pause_covers_update_readiness_switch_and_resume(self) -> None:
        self.database_changing()
        self.store = cutover_store(self.root, database=True)
        result = self.run_release()
        self.assertEqual(result.step, "complete")
        self.assertEqual(result.pause.state, "complete")
        self.assertGreater(result.pause.duration_seconds, 0)
        self.assertLessEqual(result.pause.began_at, result.backup.recovery_point_at)
        self.assertEqual(result.pause.recovery_binding, result.plan.new.binding)
        self.assertEqual(result.maintenance.update_modules, ("site",))
        with self.assertRaisesRegex(ValueError, "coherent tuple"):
            rollback_compatible_cutover(self.store, self.provider, self.record_id)

    def test_drain_failure_retains_prepared_serving_and_never_runs_maintenance(self) -> None:
        self.database_changing()
        self.provider.active["queue"] = 1
        with self.assertRaises(ValueError):
            self.run_release()
        record = self.store.load(self.record_id)
        assert record.pause is not None
        self.assertEqual(record.pause.state, "fencing")
        self.assertIsNone(record.pause.ended_at)
        self.assertEqual(self.provider.target.slot_id, "prepared")
        self.assertEqual(self.provider.maintenance_count, 0)
        self.provider.active["queue"] = 0
        self.assertEqual(self.run_release().step, "complete")

    def test_uncertain_writer_resumption_reuses_owner_receipt_after_restart(self) -> None:
        self.database_changing()
        self.provider.uncertain_resume = True
        with self.assertRaises(TimeoutError):
            self.run_release()
        record = self.store.load(self.record_id)
        assert record.pause is not None
        self.assertEqual(record.pause.state, "resuming")
        self.assertIsNone(record.pause.ended_at)
        self.assertEqual(self.run_release().step, "complete")
        self.assertEqual(self.provider.resume_count, 1)

    def test_inflight_old_connections_do_not_invent_completion(self) -> None:
        self.provider.drained = False
        with self.assertRaises(ValueError):
            self.run_release()
        record = self.store.load(self.record_id)
        self.assertEqual(record.step, "draining")
        self.assertIsNone(record.ended_at)
        self.provider.drained = True
        self.assertEqual(self.run_release().step, "complete")
