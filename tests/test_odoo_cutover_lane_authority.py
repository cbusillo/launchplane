"""Onboarding and legacy replacement must retain two-slot traffic ownership."""

from datetime import UTC, datetime
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.product_onboarding import build_product_profile_record
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementPlan,
    OdooStableTargetRuntimeSnapshot,
    OdooTargetReplacementStageError,
    build_odoo_stable_target_replacement_plan,
    execute_odoo_stable_target_replacement_apply,
)
from tests.test_product_onboarding import _manifest_payload
from tests.test_odoo_stable_target_replacement import (
    _Store,
    _inventory,
    _profile,
    _target_record,
    _target_id_record,
)


def lane_authority() -> OdooCutoverLane:
    return OdooCutoverLane(
        route_id="fixture",
        host="fixture.invalid",
        database="fixture",
        database_uuid="fixture-uuid",
        prepared_origin="http://prepared:8080",
        slots=(
            OdooSlotCoordinates(slot_id="blue", compose_id="blue", private_url="http://blue:8069"),
            OdooSlotCoordinates(
                slot_id="green", compose_id="green", private_url="http://green:8069"
            ),
        ),
        writers=(),
    )


class OdooLaneAuthorityTests(unittest.TestCase):
    def test_reonboarding_preserves_only_the_matching_lane(self) -> None:
        manifest = ProductOnboardingManifest.model_validate(
            {**_manifest_payload(), "driver_id": "odoo"}
        )
        profile = build_product_profile_record(
            manifest=manifest, updated_at=datetime.now(UTC).isoformat()
        )
        profile.lanes[0].odoo_cutover = lane_authority()
        refreshed = build_product_profile_record(
            manifest=manifest, existing_profile=profile, updated_at=datetime.now(UTC).isoformat()
        )
        self.assertEqual(refreshed.lanes[0].odoo_cutover, profile.lanes[0].odoo_cutover)
        self.assertIsNone(refreshed.lanes[1].odoo_cutover)
        changed_lane = manifest.lanes[0].model_copy(update={"context": "another-context"})
        changed_manifest = manifest.model_copy(update={"lanes": (changed_lane, manifest.lanes[1])})
        changed = build_product_profile_record(
            manifest=changed_manifest,
            existing_profile=profile,
            updated_at=datetime.now(UTC).isoformat(),
        )
        self.assertIsNone(changed.lanes[0].odoo_cutover)

    def test_replacement_blocks_configured_and_recorded_slots_before_provider_access(self) -> None:
        for configured in (True, False):
            with self.subTest(configured=configured):
                profile = _profile()
                inventory = _inventory()
                if configured:
                    profile.lanes[0].odoo_cutover = lane_authority()
                else:
                    inventory.runtime_identity = RuntimeIdentity(
                        context=inventory.context,
                        instance=inventory.instance,
                        deployment_record_id=inventory.deployment_record_id,
                        artifact_id="fixture",
                        source_git_ref=inventory.source_git_ref,
                        image_reference="example.invalid/fixture@sha256:" + "a" * 64,
                        slot="green",
                    )
                store = _Store(
                    profile=profile,
                    inventory=inventory,
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                )
                provider = Mock(side_effect=AssertionError("provider must remain untouched"))
                checkpoint = Mock()
                with patch(
                    "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                    provider,
                ):
                    plan = build_odoo_stable_target_replacement_plan(
                        control_plane_root=Path("."),
                        record_store=store,
                        request=OdooStableTargetReplacementRequest(
                            product=profile.product, instance=profile.lanes[0].instance
                        ),
                        dokploy_request=provider,
                    )
                    self.assertEqual(plan.plan_status, "blocked")
                    self.assertEqual(plan.blocker_codes, ("two_slot_cutover_required",))
                    with self.assertRaises(OdooTargetReplacementStageError) as refused:
                        execute_odoo_stable_target_replacement_apply(
                            control_plane_root=Path("."),
                            record_store=store,
                            request=OdooStableTargetReplacementApplyRequest(
                                product=profile.product, instance=profile.lanes[0].instance
                            ),
                            dokploy_request=provider,
                            provider_effect_checkpoint=checkpoint,
                        )
                    self.assertEqual(
                        refused.exception.code, "plan_not_ready.two_slot_cutover_required"
                    )
                provider.assert_not_called()
                checkpoint.assert_not_called()
                self.assertFalse(store.deployment_records)
                self.assertFalse(store.environment_inventories)

    def test_apply_rechecks_lane_after_planning(self) -> None:
        for configured in (True, False):
            with self.subTest(configured=configured):
                profile = _profile()
                inventory = _inventory()
                store = _Store(
                    profile=profile,
                    inventory=inventory,
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                )
                ready = OdooStableTargetReplacementPlan(
                    plan_status="ready",
                    product=profile.product,
                    context=profile.lanes[0].context,
                    instance=profile.lanes[0].instance,
                    strategy="recreate-in-place",
                    target_record_found=True,
                    target_id_record_found=True,
                    inventory_found=True,
                    expected_next_target_name="fixture",
                    current_target=OdooStableTargetRuntimeSnapshot(
                        target_type="compose", target_id="fixture", target_name="fixture"
                    ),
                )
                if configured:
                    profile.lanes[0].odoo_cutover = lane_authority()
                else:
                    inventory.runtime_identity = RuntimeIdentity(
                        context=inventory.context,
                        instance=inventory.instance,
                        deployment_record_id=inventory.deployment_record_id,
                        artifact_id="fixture",
                        source_git_ref=inventory.source_git_ref,
                        image_reference="example.invalid/fixture@sha256:" + "a" * 64,
                        slot="green",
                    )
                provider = Mock()
                with patch(
                    "control_plane.workflows.odoo_stable_target_replacement.build_odoo_stable_target_replacement_plan",
                    return_value=ready,
                ):
                    with self.assertRaises(OdooTargetReplacementStageError) as refused:
                        execute_odoo_stable_target_replacement_apply(
                            control_plane_root=Path("."),
                            record_store=store,
                            request=OdooStableTargetReplacementApplyRequest(
                                product=profile.product, instance=profile.lanes[0].instance
                            ),
                            dokploy_request=provider,
                        )
                self.assertEqual(refused.exception.code, "plan_not_ready.two_slot_cutover_required")
                provider.assert_not_called()
                self.assertFalse(store.deployment_records)
