"""Service-owned event coordinates with real temporary reservation storage."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from types import SimpleNamespace
from unittest.mock import patch

from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.event_testing_deploy import event_testing_deploy_request
from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope
from control_plane.storage.postgres import (
    PostgresRecordStore,
    ExistingMutationReservationLookupResult,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
    build_generic_web_provider_reconciliation_key,
)
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url
from tests.test_generic_web_deploy_recovery import (
    _create_recovery_app,
    _generic_web_recovery_reservation,
    _generic_web_recovery_target,
    _write_generic_web_recovery_reservation,
    _RecoveryObservationProvider,
)
from tests.test_service import _invoke_app


class EventDeployRecoveryReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(self.root / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.product = "sellyouroutboard"
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(product_profile_payload())
        )
        self.manifest = ArtifactIdentityManifest(
            artifact_id="recorded-build",
            source_commit="abc123",
            enterprise_base_digest="",
            image=ArtifactImageReference(
                repository="ghcr.io/cbusillo/sellyouroutboard", digest="sha256:abc123"
            ),
        )
        self.store.write_artifact_manifest(self.manifest)
        self.original = event_testing_deploy_request(
            product=self.product,
            image_reference=f"{self.manifest.image.repository}@{self.manifest.image.digest}",
            source_commit=self.manifest.source_commit,
        ).model_dump(mode="json")
        self.reservation = self.reserve(
            "first",
            state="running"
            if self._testMethodName == "test_active_lease_waits_without_provider_read"
            else "reconcile_required",
        )
        self.app = _create_recovery_app(
            root=self.root,
            store=self.store,
            actions=("product_environment.read", "generic_web_deploy.execute"),
        )
        self.path = f"/v1/admin/generic-web/deploy-recovery/{self.product}/testing"

    def reserve(
        self,
        suffix: str,
        *,
        context: str = "sellyouroutboard-testing",
        state: str = "reconcile_required",
    ) -> Any:
        reservation = _generic_web_recovery_reservation(
            original_deploy=self.original,
            idempotency_key=f"{RECONCILE_SOURCE}:{self.product}:{context}:testing:{suffix}",
        )
        reservation = reservation.model_copy(
            update={
                "scope": reconcile_reservation_scope(self.product),
                "reconciliation_key": build_generic_web_provider_reconciliation_key(
                    _generic_web_recovery_target(),
                    product=self.product,
                    original_event_deploy=None
                    if self._testMethodName == "test_legacy_saved_plan_requires_exact_fingerprint"
                    else self.original,
                ),
            }
        )
        if state == "running":
            reservation = reservation.model_copy(update={"state": "running"})
        return _write_generic_web_recovery_reservation(self.store, reservation)

    def read(self, app: Any = None) -> tuple[int, dict[str, Any]]:
        return _invoke_app(
            app or self.app,
            method="GET",
            path=self.path,
            authorization="Bearer local-operator-token",
        )

    def review(
        self,
        reference: str,
        *,
        digest: str = "",
        product: str = "sellyouroutboard",
        reason: str = "Inspect held testing deployment.",
    ) -> tuple[int, dict[str, Any]]:
        payload = {
            "product": product,
            "instance": "testing",
            "recovery_reference": reference,
            "reason": reason,
        }
        if digest:
            payload["expected_recovery_digest"] = digest
        return _invoke_app(
            self.app,
            method="POST",
            path="/v1/admin/generic-web/deploy-recovery/" + ("apply" if digest else "dry-run"),
            authorization="Bearer local-operator-token",
            payload=payload,
        )

    def test_read_and_unknown_dry_run_do_not_write_or_disclose_coordinates(self) -> None:
        provider = _RecoveryObservationProvider(
            GenericWebProviderDeploymentObservation(outcome="unknown")
        )
        before = self.store.list_held_provider_target_reservations()
        with (
            patch.object(self.store, "reserve_mutation", side_effect=AssertionError("write")),
            patch.object(
                self.store, "write_environment_inventory", side_effect=AssertionError("write")
            ),
            patch.object(
                self.store, "write_deployment_record", side_effect=AssertionError("write")
            ),
            patch.object(
                self.store, "mark_mutation_reconcile_required", side_effect=AssertionError("write")
            ),
            patch.object(
                self.store, "adopt_reconciled_mutation", side_effect=AssertionError("write")
            ),
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=provider,
            ),
        ):
            code, read = self.read()
            self.assertEqual(code, 200, read)
            self.assertEqual(provider.observation_calls, 0)
            code, plan = self.review(read["recovery_reference"])
            self.assertEqual(code, 200, plan)
            self.assertEqual(plan["proposed_action"], "hold_unknown")
            code, result = self.review(read["recovery_reference"], digest=plan["recovery_digest"])
            self.assertEqual(code, 409, result)
            self.assertEqual(result["error"]["code"], "recovery_not_actionable")
        self.assertEqual(before, self.store.list_held_provider_target_reservations())
        serialized = json.dumps([read, plan, result])
        for sensitive in (
            self.reservation.idempotency_key,
            self.reservation.reconciliation_key,
            "app-syo-testing",
            self.manifest.source_commit,
            "original_deploy",
        ):
            self.assertNotIn(sensitive, serialized)

    def test_missing_and_ambiguous_reservations_refuse(self) -> None:
        with patch.object(self.store, "list_held_provider_target_reservations", return_value=()):
            self.assertEqual(self.read()[0], 404)
        self.path = "/v1/admin/generic-web/deploy-recovery/missing/testing"
        code, _ = self.read()
        self.assertIn(code, (403, 404, 409))
        self.path = f"/v1/admin/generic-web/deploy-recovery/{self.product}/testing"
        second = self.reservation.model_copy(update={"record_id": "second", "scope": "other-scope"})
        _write_generic_web_recovery_reservation(
            self.store,
            second.model_copy(update={"provider_target_key": "different-provider-target"}),
        )
        code, result = self.read()
        self.assertEqual(code, 409, result)

    def test_unauthorized_read_and_apply_refuse_before_coordinate_lookup(self) -> None:
        denied = _create_recovery_app(
            root=self.root, store=self.store, actions=("generic_web_preview.execute",)
        )
        with patch.object(
            self.store,
            "list_held_provider_target_reservations",
            side_effect=AssertionError("disclosure"),
        ):
            self.assertEqual(self.read(denied)[0], 403)
        code, read = self.read()
        self.assertEqual(code, 200)
        self.app = _create_recovery_app(
            root=self.root, store=self.store, actions=("product_environment.read",)
        )
        with patch.object(
            self.store,
            "list_held_provider_target_reservations",
            side_effect=AssertionError("disclosure"),
        ):
            self.assertEqual(self.review(read["recovery_reference"], digest="a" * 64)[0], 403)

    def test_changed_reservation_and_fabricated_reference_refuse(self) -> None:
        code, read = self.read()
        self.assertEqual(code, 200, read)
        changed = self.reservation.model_copy(update={"provider_effect_phase": "deploy_trigger"})
        with (
            patch.object(
                self.store, "list_held_provider_target_reservations", return_value=(changed,)
            ),
            patch.object(
                self.store,
                "lookup_existing_mutation_reservation",
                return_value=ExistingMutationReservationLookupResult(
                    status="found", record=changed, observed_at=changed.updated_at
                ),
            ),
        ):
            self.assertEqual(self.review(read["recovery_reference"])[0], 409)
        self.assertEqual(self.review("event-deploy-" + "a" * 64)[0], 409)

    def test_changed_request_and_cross_lane_refuse(self) -> None:
        _, read = self.read()
        changed = self.reservation.model_copy(update={"request_fingerprint": "changed"})
        with (
            patch.object(
                self.store, "list_held_provider_target_reservations", return_value=(changed,)
            ),
            patch.object(
                self.store,
                "lookup_existing_mutation_reservation",
                return_value=ExistingMutationReservationLookupResult(
                    status="found", record=changed, observed_at=changed.updated_at
                ),
            ),
        ):
            self.assertEqual(self.review(read["recovery_reference"])[0], 409)
        self.assertEqual(
            self.review(read["recovery_reference"], product="different-product")[0], 409
        )

    def test_active_lease_waits_without_provider_read(self) -> None:
        # A live worker's lease cannot be adopted, even through the service reference.
        code, read = self.read()
        self.assertEqual(code, 200, read)
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            side_effect=AssertionError("provider read"),
        ):
            code, plan = self.review(read["recovery_reference"])
            self.assertEqual(code, 200, plan)
            self.assertEqual(plan["proposed_action"], "wait_for_active_lease")

    def test_reviewed_reference_adopts_exact_observation_and_rejects_stale_evidence(self) -> None:
        provider = _RecoveryObservationProvider(
            GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="provider-deployment",
                started_at="2026-08-15T12:00:00Z",
                finished_at="2026-08-15T12:05:00Z",
            )
        )
        _, read = self.read()
        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=provider,
            ),
            patch.object(
                self.store, "reserve_mutation", side_effect=AssertionError("new reservation")
            ),
        ):
            code, plan = self.review(read["recovery_reference"])
            self.assertEqual(code, 200, plan)
            self.assertEqual(plan["proposed_action"], "adopt_observed")
            code, refused = self.review(
                read["recovery_reference"], digest=plan["recovery_digest"], reason="Changed reason"
            )
            self.assertEqual(code, 409, refused)
            self.assertEqual(refused["error"]["code"], "stale_recovery_digest")
            provider.observation = provider.observation.model_copy(
                update={"deployment_id": "different-deployment"}
            )
            self.assertEqual(
                self.review(read["recovery_reference"], digest=plan["recovery_digest"])[0], 409
            )
            provider.observation = provider.observation.model_copy(
                update={"deployment_id": "provider-deployment"}
            )
            code, applied = self.review(read["recovery_reference"], digest=plan["recovery_digest"])
            self.assertEqual(code, 202, applied)
            self.assertEqual(applied["recovery_action"], "adopt_observed")
        stored = self.store.read_idempotency_record(
            scope=self.reservation.scope,
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
        )
        assert stored is not None
        self.assertEqual(stored.state, "completed")
        self.assertTrue(self.store.list_deployment_records())
        self.assertEqual(
            self.review(read["recovery_reference"], digest=plan["recovery_digest"])[0], 404
        )

    def test_reference_snapshot_change_between_resolution_and_inspection_refuses(self) -> None:
        _, read = self.read()
        lookup = self.store.lookup_existing_mutation_reservation(
            route_path=self.reservation.route_path,
            idempotency_key=self.reservation.idempotency_key,
            request_fingerprint=self.reservation.request_fingerprint,
        )
        assert lookup.record is not None
        changed = ExistingMutationReservationLookupResult(
            status="found",
            record=lookup.record.model_copy(update={"provider_effect_phase": "deploy_trigger"}),
            observed_at=lookup.observed_at,
        )
        with (
            patch.object(
                self.store, "lookup_existing_mutation_reservation", side_effect=(lookup, changed)
            ),
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                side_effect=AssertionError("provider read"),
            ),
        ):
            code, refused = self.review(read["recovery_reference"])
            self.assertEqual(code, 409, refused)
            self.assertEqual(refused["error"]["code"], "recovery_reference_changed")

    def test_workflow_identity_cannot_acquire_reference(self) -> None:
        with patch.object(
            self.store,
            "list_held_provider_target_reservations",
            side_effect=AssertionError("disclosure"),
        ):
            code, result = _invoke_app(
                self.app, method="GET", path=self.path, authorization="Bearer valid-token"
            )
            self.assertEqual(code, 403, result)

    def test_legacy_saved_plan_requires_exact_fingerprint(self) -> None:
        self.assertEqual(self.read()[0], 409)
        deploy = self.original["deploy"]
        assert isinstance(deploy, dict)
        plan = {
            "desired_artifact_id": deploy["artifact_id"],
            "desired_commit": deploy["source_git_ref"],
        }
        with patch.object(
            self.store,
            "read_product_reconcile_request",
            return_value=SimpleNamespace(last_plan=plan),
        ):
            code, read = self.read()
            self.assertEqual(code, 200, read)
        plan["desired_commit"] = "newer-build"
        with patch.object(
            self.store,
            "read_product_reconcile_request",
            return_value=SimpleNamespace(last_plan=plan),
        ):
            self.assertEqual(self.review(read["recovery_reference"])[0], 409)
