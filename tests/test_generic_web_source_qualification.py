"""Release candidates and direct promotions require the same complete testing evidence."""

import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused

from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.release_review import (
    ReleaseEvidenceUnavailable,
    ReleaseReviewStore,
    release_version,
)
from control_plane.workflows.generic_web_promotion import execute_generic_web_prod_promotion
from tests.test_generic_web_promotion import _request, _store_with_production


# This branch-only fake supplies profile/inventory/deployment reads, not the
# unrelated Odoo and decision-publication methods of the full review protocol.
class GenericWebSourceQualificationTests(unittest.TestCase):
    def test_cached_candidate_profile_cannot_reuse_a_removed_rebound_or_replaced_source(
        self,
    ) -> None:
        for change in ("missing_profile", "rebound_lane", "removed_lane", "driver_changed"):
            with self.subTest(change=change):
                store = _store_with_production()
                profile = store.profile
                lane = profile.lanes[0]
                inventory = store.read_environment_inventory(
                    context_name=lane.context, instance_name=lane.instance
                )
                assert inventory.runtime_identity is not None
                store.write_qualified_testing_inventory(
                    inventory.model_copy(
                        update={
                            "source_git_ref": "b" * 40,
                            "runtime_identity": inventory.runtime_identity.model_copy(
                                update={"source_git_ref": "b" * 40}
                            ),
                        }
                    )
                )
                self.assertEqual(
                    release_version(
                        store=cast(ReleaseReviewStore, cast(object, store)),
                        profile=profile,
                        instance="testing",
                    ).source_commit,
                    "b" * 40,
                )
                if change == "missing_profile":
                    with patch.object(
                        store,
                        "read_product_profile_record",
                        side_effect=FileNotFoundError("missing product"),
                    ):
                        with self.assertRaises(ReleaseEvidenceUnavailable):
                            release_version(
                                store=cast(ReleaseReviewStore, cast(object, store)),
                                profile=profile,
                                instance="testing",
                            )
                    continue
                store.profile = profile.model_copy(
                    update={
                        "driver_id": "missing-driver"
                        if change == "driver_changed"
                        else profile.driver_id,
                        "lanes": tuple(
                            item.model_copy(update={"context": item.context + "-replacement"})
                            if change == "rebound_lane" and item.instance == "testing"
                            else item
                            for item in profile.lanes
                            if not (change == "removed_lane" and item.instance == "testing")
                        ),
                    }
                )
                with self.assertRaises(ReleaseEvidenceUnavailable):
                    release_version(
                        store=cast(ReleaseReviewStore, cast(object, store)),
                        profile=profile,
                        instance="testing",
                    )

    def test_candidates_and_direct_promotions_reject_unqualified_referenced_evidence(self) -> None:
        cases = (
            "missing_record",
            "record_id",
            "record_context",
            "record_instance",
            "record_artifact",
            "record_source",
            "deploy_failed",
            "post_deploy_failed",
            "health_failed",
            "record_identity_missing",
            "inventory_context",
            "inventory_instance",
            "inventory_source",
            "inventory_artifact",
            "inventory_identity_missing",
            "identity_product",
            "identity_context",
            "identity_instance",
            "identity_record",
            "identity_artifact",
            "identity_source",
            "identity_environment",
            "inventory_target",
            "record_target_id",
            "record_target_provider",
            "record_target_missing",
        )
        for case in cases:
            with self.subTest(case=case):
                store = _store_with_production()
                lane = store.profile.lanes[0]
                inventory = store.read_environment_inventory(
                    context_name=lane.context, instance_name="testing"
                )
                assert inventory.runtime_identity is not None
                inventory = inventory.model_copy(
                    update={
                        "source_git_ref": "b" * 40,
                        "runtime_identity": inventory.runtime_identity.model_copy(
                            update={"source_git_ref": "b" * 40}
                        ),
                    }
                )
                store.write_qualified_testing_inventory(inventory)
                self.assertEqual(
                    release_version(
                        store=cast(ReleaseReviewStore, cast(object, store)),
                        profile=store.profile,
                        instance="testing",
                    ).source_commit,
                    inventory.source_git_ref,
                )
                record = store.read_deployment_record(inventory.deployment_record_id)
                assert record.runtime_identity is not None and record.deployed_target is not None
                assert inventory.runtime_identity is not None
                if case == "missing_record":
                    del store.deployments[record.record_id]
                elif case.startswith("record_") and case in {
                    "record_id",
                    "record_context",
                    "record_instance",
                    "record_source",
                    "record_artifact",
                }:
                    field = {
                        "record_id": "record_id",
                        "record_context": "context",
                        "record_instance": "instance",
                        "record_source": "source_git_ref",
                        "record_artifact": "artifact_identity",
                    }[case]
                    value: object = (
                        ArtifactIdentityReference(artifact_id="another-image")
                        if field == "artifact_identity"
                        else "another"
                    )
                    store.deployments[record.record_id] = record.model_copy(update={field: value})
                elif case == "deploy_failed":
                    store.write_deployment_record(
                        record.model_copy(
                            update={"deploy": record.deploy.model_copy(update={"status": "fail"})}
                        )
                    )
                elif case == "post_deploy_failed":
                    store.write_deployment_record(
                        record.model_copy(
                            update={
                                "post_deploy_update": PostDeployUpdateEvidence(
                                    attempted=True, status="fail"
                                )
                            }
                        )
                    )
                elif case == "health_failed":
                    store.write_deployment_record(
                        record.model_copy(
                            update={"destination_health": HealthcheckEvidence(status="fail")}
                        )
                    )
                elif case == "record_identity_missing":
                    store.write_deployment_record(
                        record.model_copy(update={"runtime_identity": None})
                    )
                elif case in {
                    "inventory_context",
                    "inventory_instance",
                    "inventory_source",
                    "inventory_artifact",
                    "inventory_identity_missing",
                }:
                    field = {
                        "inventory_context": "context",
                        "inventory_instance": "instance",
                        "inventory_source": "source_git_ref",
                        "inventory_artifact": "artifact_identity",
                        "inventory_identity_missing": "runtime_identity",
                    }[case]
                    value = (
                        None
                        if field == "runtime_identity"
                        else ArtifactIdentityReference(artifact_id="another-image")
                        if field == "artifact_identity"
                        else "another"
                    )
                    store.inventories[(lane.context, lane.instance)] = inventory.model_copy(
                        update={field: value}
                    )
                elif case.startswith("identity_"):
                    field = {
                        "identity_product": "product",
                        "identity_context": "context",
                        "identity_instance": "instance",
                        "identity_record": "deployment_record_id",
                        "identity_artifact": "artifact_id",
                        "identity_source": "source_git_ref",
                        "identity_environment": "environment_kind",
                    }[case]
                    identity = record.runtime_identity.model_copy(update={field: "another"})
                    store.write_deployment_record(
                        record.model_copy(update={"runtime_identity": identity})
                    )
                    store.write_environment_inventory(
                        inventory.model_copy(update={"runtime_identity": identity})
                    )
                elif case == "inventory_target":
                    store.write_environment_inventory(
                        inventory.model_copy(
                            update={
                                "deploy": inventory.deploy.model_copy(
                                    update={"target_name": "another"}
                                )
                            }
                        )
                    )
                elif case == "record_target_missing":
                    # Real historical payloads without either target evidence
                    # form cannot manufacture authority from a passing inventory.
                    payload = record.model_dump(mode="json")
                    payload.pop("deployed_target", None)
                    payload.pop("resolved_target", None)
                    store.write_deployment_record(type(record).model_validate(payload))
                elif case.startswith("record_target_"):
                    field = "target_id" if case == "record_target_id" else "provider_id"
                    store.write_deployment_record(
                        record.model_copy(
                            update={
                                "deployed_target": record.deployed_target.model_copy(
                                    update={field: "another"}
                                )
                            }
                        )
                    )
                else:
                    self.fail(case)
                with self.assertRaises(ReleaseEvidenceUnavailable):
                    release_version(
                        store=cast(ReleaseReviewStore, cast(object, store)),
                        profile=store.profile,
                        instance="testing",
                    )
                provider = Mock()
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    execute_generic_web_prod_promotion(
                        control_plane_root=Path("."),
                        record_store=store,
                        request=_request(source_git_ref=inventory.source_git_ref, dry_run=True),
                        deploy_provider=provider,
                    )
                self.assertEqual(provider.mock_calls, [])
                self.assertEqual(store.promotions, {})

    def test_production_baseline_does_not_require_the_new_testing_qualification(self) -> None:
        store = _store_with_production()
        production = store.read_environment_inventory(
            context_name=store.profile.lanes[0].context, instance_name="prod"
        )
        assert production.runtime_identity is not None
        production = production.model_copy(
            update={
                "runtime_identity": production.runtime_identity.model_copy(
                    update={"source_git_ref": "a" * 40}
                )
            }
        )
        store.write_environment_inventory(production)
        store.deployments.clear()
        version = release_version(
            store=cast(ReleaseReviewStore, cast(object, store)),
            profile=store.profile,
            instance="prod",
        )
        assert production.runtime_identity is not None
        self.assertEqual(version.artifact_id, production.runtime_identity.artifact_id)

    def test_provider_specific_target_type_keeps_legacy_application_compatibility(self) -> None:
        store = _store_with_production()
        lane = store.profile.lanes[0]
        inventory = store.read_environment_inventory(
            context_name=lane.context, instance_name=lane.instance
        )
        record = store.read_deployment_record(inventory.deployment_record_id)
        assert record.deployed_target is not None
        target = record.deployed_target.model_copy(
            update={
                "provider_id": "fake-cloud",
                "target_category": "service",
                "provider_target_type": "managed-service",
                "display_name": "Friendly managed service label",
            }
        )
        deploy = record.deploy.model_copy(
            update={
                "provider_id": target.provider_id,
                "target_category": target.target_category,
                "provider_target_type": target.provider_target_type,
            }
        )
        store.write_deployment_record(
            record.model_copy(update={"deploy": deploy, "deployed_target": target})
        )
        store.write_environment_inventory(inventory.model_copy(update={"deploy": deploy}))
        result = execute_generic_web_prod_promotion(
            control_plane_root=Path("."), record_store=store, request=_request(dry_run=True)
        )
        self.assertEqual(result.promotion_status, "pending")
