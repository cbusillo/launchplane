"""Decision snapshots must survive private maintenance, retry and switch-back."""

import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch, MagicMock

from control_plane.contracts.odoo_cutover import (
    OdooCutoverPlan,
    OdooSlot,
    OdooWebsiteSettingsReceipt,
)
from control_plane.contracts.odoo_website_settings import (
    OdooWebsiteSettings,
    OdooWebsiteSettingsIntent,
)
from control_plane.dokploy.slot_maintenance import (
    maintenance_payload,
    payload_digest,
    run_planned_slot_maintenance,
)
from control_plane.dokploy.slot_website_settings import run_slot_website_settings
from control_plane.odoo_cutover_storage import DeploymentCutoverStore
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.workflows.odoo_cutover import execute_odoo_cutover, rollback_compatible_cutover
from tests.test_odoo_cutover import IsolatedCutoverProvider


def snapshot(plan: OdooCutoverPlan, *, production: bool = False) -> OdooWebsiteSettingsIntent:
    return OdooWebsiteSettingsIntent(
        artifact_id=(plan.production if production else plan.candidate).artifact_id,
        settings=OdooWebsiteSettings(
            website_id=1,
            public_origin="https://" + plan.host,
            google_analytics_key="G-PREVIOUS" if production else "G-CANDIDATE",
            public_indexing_enabled=True,
        ),
    )


class WebsiteProvider(IsolatedCutoverProvider):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.cutover = OdooCutoverPlan.model_validate(
            {**self.cutover.model_dump(), "website_settings": snapshot(self.cutover)}
        )
        self.bad_receipt = False
        self.website_receipts: list[OdooWebsiteSettingsIntent] = []

    def website_settings(
        self, plan: OdooCutoverPlan, slot: OdooSlot, intent: OdooWebsiteSettingsIntent
    ) -> OdooWebsiteSettingsReceipt:
        self.actions.append("website:" + slot.slot_id)
        self.website_receipts.append(intent)
        return OdooWebsiteSettingsReceipt(
            binding=slot.binding,
            image=slot.image,
            settings_sha256=intent.settings.digest,
            startup_settings_sha256=intent.settings.digest,
            retained_startup_settings_sha256=intent.settings.digest,
            committed=True,
        )

    def recover_and_resume(self, site: Any, pause_id: str, expected: Any) -> Any:
        from control_plane.contracts.prepared_public_site import PublicRecoveryObservation

        if self.target.binding != expected:
            raise ValueError("wrong serving target")
        self.handoff_writers(self.cutover, self.target)
        return PublicRecoveryObservation(
            binding=expected,
            pause_id=pause_id,
            serving_evidence_id="fixture-serving",
            writer_owners=tuple((writer.writer_id, 1) for writer in self.cutover.writers),
        )

    def maintenance(self, plan: OdooCutoverPlan) -> Any:
        receipt = super().maintenance(plan)
        assert plan.website_settings is not None
        if not self.bad_receipt:
            receipt = receipt.model_copy(
                update={
                    "website_settings": self.website_settings(plan, plan.new, plan.website_settings)
                }
            )
        return receipt


class CutoverWebsiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.provider = WebsiteProvider(Path(self.directory.name) / "site")
        from control_plane.storage.filesystem import FilesystemRecordStore

        self.records = FilesystemRecordStore(state_dir=Path(self.directory.name) / "records")
        plan = self.provider.cutover
        deployment = DeploymentRecord(
            record_id=plan.new.runtime_identity.deployment_record_id,
            context=plan.new.runtime_identity.context,
            instance=plan.new.binding.environment_id,
            source_git_ref=plan.candidate.source_commit,
            deploy=DeploymentEvidence(
                target_name="fixture", target_type="compose", deploy_mode="two-slot"
            ),
        )
        self.store = DeploymentCutoverStore(self.records, deployment)
        self.record_id = "website-cutover"
        self.addCleanup(self.provider.engine.dispose)

    def run_cutover(self) -> Any:
        return execute_odoo_cutover(
            self.provider.cutover, self.store, self.provider, record_id=self.record_id
        )

    def test_snapshot_changes_bind_native_plan_digest_and_reject_wrong_artifact(self) -> None:
        plan = self.provider.cutover
        different = plan.model_copy(update={"website_settings": snapshot(plan, production=True)})
        self.assertNotEqual(
            payload_digest(maintenance_payload(plan)),
            payload_digest(maintenance_payload(different)),
        )
        with self.assertRaises(ValueError):
            OdooCutoverPlan.model_validate(different.model_dump())

    def test_missing_receipt_blocks_startup_readiness_and_traffic_then_recovers(self) -> None:
        self.provider.bad_receipt = True
        with self.assertRaises(ValueError):
            self.run_cutover()
        self.assertNotIn("readiness", self.provider.actions)
        self.assertNotIn("switch", self.provider.actions)
        self.provider.bad_receipt = False
        result = self.run_cutover()
        self.assertEqual(result.step, "complete")
        self.assertEqual(self.provider.maintenance_count, 1)
        self.assertLess(
            self.provider.actions.index("website:new"), self.provider.actions.index("readiness")
        )

    def test_recovery_cannot_skip_missing_persisted_receipt(self) -> None:
        self.provider.fail_ready = True
        with self.assertRaises(ValueError):
            self.run_cutover()
        record = self.store.load(self.record_id)
        self.store.save(record.model_copy(update={"maintenance": None}))
        self.provider.fail_ready = False
        with self.assertRaises(ValueError):
            self.run_cutover()
        self.assertNotIn("switch", self.provider.actions)

    def test_historical_switch_back_disables_collection_before_switch_and_preserves_consent(
        self,
    ) -> None:
        self.run_cutover()
        self.provider.actions.clear()
        self.provider.uncertain_switch = True
        with self.assertRaises(TimeoutError):
            rollback_compatible_cutover(self.store, self.provider, self.record_id)
        result = rollback_compatible_cutover(self.store, self.provider, self.record_id)
        intent = self.provider.website_receipts[-1]
        self.assertEqual(intent.artifact_id, self.provider.cutover.production.artifact_id)
        self.assertEqual(intent.settings.operation, "disable_collection")
        self.assertFalse(intent.settings.public_indexing_enabled)
        self.assertFalse(intent.settings.google_analytics_key)
        self.assertIsNone(intent.settings.cookies_bar)
        self.assertIsNone(intent.settings.block_third_party_domains)
        assert result.rollback_website_settings is not None
        self.assertEqual(result.rollback_website_settings.settings_sha256, intent.settings.digest)
        self.assertLess(
            self.provider.actions.index("website:old"), self.provider.actions.index("switch")
        )
        self.assertEqual(self.provider.actions.count("switch"), 1)

    def test_retained_snapshot_is_used_instead_of_forward_values(self) -> None:
        plan = self.provider.cutover
        retained = snapshot(plan, production=True)
        self.provider.cutover = plan.model_copy(update={"production_website_settings": retained})
        self.run_cutover()
        rollback_compatible_cutover(self.store, self.provider, self.record_id)
        self.assertEqual(self.provider.website_receipts[-1], retained)

    def test_native_runner_requires_startup_readback_and_committed_digest(self) -> None:
        plan = self.provider.cutover
        intent = plan.website_settings
        assert intent is not None
        calls: list[str] = []

        def startup(*_args: Any) -> tuple[str, str]:
            calls.append("startup")
            return (intent.settings.digest, intent.settings.digest)

        def execute(argv: tuple[str, ...], stdin: str | None) -> str:
            if argv[1] == "inspect":
                return plan.new.image
            calls.append("native")
            assert stdin is not None
            raw = json.loads(stdin)

            # Execute the actual transport program, with a child-process fixture.
            def child(args: Any, **kwargs: Any) -> Any:
                payload = json.loads(
                    base64.b64decode(kwargs["env"]["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"])
                )
                self.assertEqual(
                    payload["website_settings"], intent.settings.model_dump(mode="json")
                )
                self.assertEqual(payload["config_parameters"][0]["key"], "unrelated")
                from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PARAMETER

                self.assertEqual(
                    payload["config_parameters"][-1]["key"], WEBSITE_INDEXING_PARAMETER
                )
                from types import SimpleNamespace

                return SimpleNamespace(
                    returncode=0,
                    stdout=("website_settings_sha256=" + intent.settings.digest + "\n").encode(),
                    stderr=b"",
                )

            from io import StringIO
            from contextlib import redirect_stdout

            output = StringIO()
            existing = base64.b64encode(
                json.dumps(
                    {
                        "config_parameters": [
                            {"key": "unrelated", "value": {"source": "literal", "value": "kept"}}
                        ]
                    }
                ).encode()
            ).decode()
            connection = MagicMock()
            connection.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (
                plan.new.runtime_identity.database_uuid,
            )
            from types import SimpleNamespace

            sql = SimpleNamespace(connect=lambda **_kwargs: connection)
            with (
                patch.dict("sys.modules", {"psycopg2": sql}),
                patch("sys.stdin", StringIO(json.dumps(raw))),
                patch("subprocess.run", child),
                patch.dict(
                    os.environ,
                    {
                        "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": existing,
                        "ODOO_DB_HOST": "fixture",
                        "ODOO_DB_USER": "fixture",
                        "ODOO_DB_PASSWORD": "inert",
                    },
                ),
                redirect_stdout(output),
            ):
                exec(compile(argv[-1], "<slot-website-settings>", "exec"), {})
            return output.getvalue()

        receipt = run_slot_website_settings(
            plan,
            plan.new,
            intent,
            runner_container="runner",
            execute=execute,
            configure_startup=startup,
        )
        self.assertEqual(receipt.settings_sha256, intent.settings.digest)
        self.assertEqual(calls, ["startup", "native"])
        calls.clear()
        with self.assertRaises(ValueError):
            run_slot_website_settings(
                plan,
                plan.new,
                intent,
                runner_container="runner",
                execute=execute,
                configure_startup=lambda *_args: (intent.settings.digest, ""),
            )
        self.assertNotIn("native", calls)
        calls.clear()
        with self.assertRaises(ValueError):
            run_slot_website_settings(
                plan,
                plan.new,
                intent,
                runner_container="runner",
                execute=execute,
                configure_startup=lambda *_args: ("", ""),
            )
        self.assertNotIn("native", calls)
        with self.assertRaises(ValueError):
            run_planned_slot_maintenance(plan, runner_container="runner", execute=execute)

    def test_wrong_native_receipt_keeps_candidate_startup_guard_closed(self) -> None:
        plan = self.provider.cutover
        stamp_writes: list[str | None] = []
        digest = payload_digest(maintenance_payload(plan))
        native = {
            "state": "passed",
            "plan_sha256": digest,
            "database": plan.new.binding.database,
            "candidate_artifact_id": plan.candidate.artifact_id,
            "candidate_image": plan.new.image,
            "candidate_manifest_sha256": plan.compatibility.candidate_manifest_sha256,
            "odoo_processes": 1,
            "install_modules": [],
            "update_modules": [],
            "updated_by_odoo": [],
            "module_states": {},
        }

        def transport(argv: tuple[str, ...], stdin: str | None) -> str:
            if argv[1] == "inspect":
                return plan.new.image
            if argv[-1].endswith("web-ready"):
                stamp_writes.append(stdin)
                return ""
            assert stdin is not None
            if "payload" in json.loads(stdin):
                return json.dumps({"receipt": native, "credential_commit_id": "committed-fixture"})
            return json.dumps({"settings_sha256": ""})

        assert plan.website_settings is not None
        settings_digest = plan.website_settings.settings.digest
        with self.assertRaises(ValueError):
            run_planned_slot_maintenance(
                plan,
                runner_container="runner",
                execute=transport,
                configure_startup=lambda *_args: (settings_digest, settings_digest),
            )
        self.assertFalse(stamp_writes)

    def test_deployment_bridge_updates_verified_baseline_without_replacing_prepared_intent(
        self,
    ) -> None:
        from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
        from control_plane.workflows.odoo_cutover_deployment import (
            execute_odoo_cutover_deployment,
            execute_odoo_cutover_rollback_deployment,
        )

        plan = self.provider.cutover
        pending = snapshot(plan).model_copy(update={"artifact_id": "future-artifact"})
        self.records.write_odoo_instance_override_record(
            OdooInstanceOverrideRecord(
                context=plan.new.runtime_identity.context,
                instance=plan.new.binding.environment_id,
                website_settings=pending,
                updated_at="2026-10-15T15:00:00Z",
            )
        )
        result = execute_odoo_cutover_deployment(
            plan=plan,
            record_store=self.records,
            provider=self.provider,
            context=plan.new.runtime_identity.context,
            instance=plan.new.binding.environment_id,
            checkpoint=lambda _step: None,
        )
        applied = self.records.read_odoo_instance_override_record(
            context_name=plan.new.runtime_identity.context,
            instance_name=plan.new.binding.environment_id,
        )
        self.assertEqual(applied.website_settings, pending)
        self.assertEqual(applied.last_apply.website_settings, plan.website_settings)
        self.assertTrue(applied.last_apply.website_settings_verified)
        execute_odoo_cutover_rollback_deployment(
            deployment=self.records.read_deployment_record(result.deployment_record_id),
            record_store=self.records,
            provider=self.provider,
            artifact_id=plan.production.artifact_id,
        )
        restored = self.records.read_odoo_instance_override_record(
            context_name=plan.new.runtime_identity.context,
            instance_name=plan.new.binding.environment_id,
        )
        self.assertEqual(restored.website_settings, pending)
        self.assertEqual(restored.last_apply.website_settings, plan.rollback_website_settings)
        self.assertTrue(restored.last_apply.website_settings_verified)

    def test_failed_readiness_keeps_public_and_writers_paused_until_snapshot_rollback(self) -> None:
        self.provider.fail_ready = True
        with self.assertRaises(ValueError):
            self.run_cutover()
        failed = self.store.load(self.record_id)
        self.assertEqual(self.provider.target.slot_id, "prepared")
        assert failed.pause is not None
        self.assertEqual(failed.pause.state, "drained")
        self.assertTrue(failed.website_settings_attempted)
        self.provider.fail_ready = False
        result = rollback_compatible_cutover(self.store, self.provider, self.record_id)
        self.assertEqual(result.step, "rolled_back")
        assert result.rollback_pause is not None
        self.assertEqual(result.rollback_pause.state, "complete")
        self.assertEqual(self.provider.target, self.provider.cutover.old)

    def test_abort_before_maintenance_resumes_old_without_disabling_collection(self) -> None:
        with patch.object(self.provider, "backup", side_effect=ValueError("fixture refusal")):
            with self.assertRaises(ValueError):
                self.run_cutover()
        self.assertEqual(self.provider.target.slot_id, "prepared")
        result = rollback_compatible_cutover(self.store, self.provider, self.record_id)
        self.assertEqual(result.step, "rolled_back")
        self.assertFalse(result.website_settings_attempted)
        self.assertFalse(self.provider.website_receipts)
        self.assertIsNone(result.rollback_website_settings)
        assert result.pause is not None
        self.assertEqual(result.pause.state, "complete")

    def test_early_refusal_preserves_verified_production_baseline(self) -> None:
        from control_plane.contracts.odoo_instance_override_record import (
            OdooInstanceOverrideRecord,
            OdooOverrideApplyResult,
        )
        from control_plane.workflows.odoo_cutover_deployment import execute_odoo_cutover_deployment

        plan = self.provider.cutover
        baseline = snapshot(plan, production=True)
        self.records.write_odoo_instance_override_record(
            OdooInstanceOverrideRecord(
                context=plan.new.runtime_identity.context,
                instance=plan.new.binding.environment_id,
                updated_at="2026-10-15T15:00:00Z",
                website_settings=plan.website_settings,
                last_apply=OdooOverrideApplyResult(website_settings=baseline),
            )
        )
        with patch.object(
            self.provider, "prepare_candidate", side_effect=ValueError("fixture refusal")
        ):
            with self.assertRaises(ValueError):
                execute_odoo_cutover_deployment(
                    plan=plan,
                    record_store=self.records,
                    provider=self.provider,
                    context=plan.new.runtime_identity.context,
                    instance=plan.new.binding.environment_id,
                    checkpoint=lambda _step: None,
                )
        result = self.records.read_odoo_instance_override_record(
            context_name=plan.new.runtime_identity.context,
            instance_name=plan.new.binding.environment_id,
        )
        self.assertEqual(result.last_apply.website_settings, baseline)
        self.assertTrue(result.last_apply.website_settings_verified)
