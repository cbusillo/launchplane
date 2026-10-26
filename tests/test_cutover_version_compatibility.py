from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooCutoverRecord, OdooSlot
from control_plane.contracts.generic_web_cutover import (
    GenericWebCutoverPlan,
    GenericWebCutoverRecord,
)
from control_plane.contracts.prepared_public_site import PreparedPublicSite
from control_plane.dokploy.slot_maintenance import slot_run_stamp
from control_plane.dokploy.slot_startup_authority import plan_digest
from control_plane.prepared_public_site import prepare_public_site, snapshot_digest
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan
from tests.test_generic_web_cutover import plan as web_plan


class AddedPlanDefault(OdooCutoverPlan):
    additive_default: str = "unchanged behavior"


class AddedSlotDefault(OdooSlot):
    additive_default: bool = False


class AddedBundleDefault(PreparedPublicSite):
    additive_default: int = 0


class CutoverVersionCompatibilityTests(unittest.TestCase):
    def test_additive_defaults_do_not_change_recovery_authority_startup_stamp_or_bundle(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            newer = AddedPlanDefault.model_validate(plan.model_dump())
            self.assertEqual(plan_digest(plan), plan_digest(newer))
            slot = AddedSlotDefault.model_validate(plan.new.model_dump())
            self.assertEqual(
                slot_run_stamp(plan), slot_run_stamp(plan.model_copy(update={"new": slot}))
            )
            site = prepare_public_site(fixture.plan, fixture)
            newer_site = AddedBundleDefault.model_validate(site.model_dump())
            self.assertEqual(snapshot_digest(site), snapshot_digest(newer_site))
            changed = plan.model_copy(update={"release_id": "another-operation"})
            self.assertNotEqual(plan_digest(plan), plan_digest(changed))

    def test_stored_classification_survives_classifier_change_but_cannot_be_new_admission(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            odoo = release_plan(fixture)
            now = datetime.now(UTC)
            for plan, record_type, plan_type, classifier in (
                (odoo, OdooCutoverRecord, OdooCutoverPlan, "odoo_cutover"),
                (web_plan(), GenericWebCutoverRecord, GenericWebCutoverPlan, "generic_web_cutover"),
            ):
                with self.subTest(kind=classifier):
                    record = record_type.model_validate(
                        dict(
                            record_id="original",
                            plan=plan.model_dump(),
                            began_at=now,
                            updated_at=now,
                        )
                    )
                    saved = record.model_dump_json()
                    with patch(
                        "control_plane.contracts."
                        + classifier
                        + ".classify_release_database_compatibility",
                        side_effect=ValueError("classifier semantics changed"),
                    ):
                        restored = record_type.model_validate_json(saved)
                        self.assertEqual(restored.plan, plan)
                        with self.assertRaises(ValueError):
                            plan_type.model_validate(restored.plan.model_dump())
