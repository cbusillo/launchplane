from typing import Any
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import timedelta
import os
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_testing_abandonment import inspect_testing_abandonment
from control_plane.generic_web_testing_settlement import testing_attempt_was_settled
from control_plane.event_deploy_recovery_reference import resolve_event_deploy_recovery_coordinates
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope
from control_plane.provider_operations import (
    ProviderMutationUnknownError,
    build_provider_operation_key,
    run_durable_provider_operation,
)
from control_plane.provider_target_holds import ProviderHoldLaneSnapshot
from control_plane.workflows.generic_web_deploy import GenericWebPostDeployGuard
from control_plane.workflows.generic_web_application_snapshot import recorded_application_lifecycle
from control_plane.workflows.generic_web_execution import EXECUTION_EVIDENCE_KEY, execution_receipt
from tests import test_generic_web_execution_receipts as execution_fixture
from tests.test_generic_web_deploy_recovery import _invoke_recovery, _invoke_recovery_apply
from tests.test_service import _invoke_app
from tests.test_generic_web_deploy_recovery import _OPERATOR_TOKEN, _create_recovery_app
from tests.support.disposable_lanes import record_safety
from tests.support.auth import local_operator_policy
from tests import test_product_reconcile as reconciliation
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.workflows.generic_web_deploy_provider import (
    decode_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_deploy import GenericWebPostDeployContext
from typing import cast


class GenericWebTestingAbandonmentTests(unittest.TestCase):
    database_url = ""
    retain_event_request = True

    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ, {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "abandonment-fixture-root"}
            )
        )
        self.fixture = execution_fixture.ExecutionRecoveryTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.request = GenericWebDeployEnvelope.model_validate(fixture.request).model_dump(
            mode="json"
        )
        self.store = fixture.store
        fixture.app = _create_recovery_app(
            root=fixture.root,
            store=self.store,
            actions=("generic_web_deploy.execute", "product_environment.read"),
        )
        if self.store.database_dialect_name == "sqlite":
            self.enterContext(
                patch.object(
                    self.store,
                    "serialize_disposable_testing_resources",
                    side_effect=lambda: nullcontext(lambda: None),
                )
            )
        lane = fixture.profile.lanes[0].model_copy(
            update={
                "generic_web_extensions": GenericWebLaneExtensions(
                    verification_extension="verireel-testing"
                )
            }
        )
        fixture.profile = fixture.profile.model_copy(update={"lanes": (lane,)})
        self.store.write_product_profile_record(fixture.profile)
        self.key = f"{RECONCILE_SOURCE}:{fixture.profile.product}:{lane.context}:testing:original"

        def execute_verireel_testing_post_deploy(
            _root: object, _store: object, _context: object, guard: GenericWebPostDeployGuard
        ) -> None:
            guard.checkpoint_effect("post_deploy_fixture_signup")
            raise ProviderMutationUnknownError("The submitted fixture response was lost.")

        self.enterContext(
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
                execute_verireel_testing_post_deploy,
            )
        )
        adapter = fixture.normal_adapter()
        adapter._retain_event_request = self.retain_event_request
        outcome = run_durable_provider_operation(
            store=self.store,
            scope=reconcile_reservation_scope(fixture.profile.product),
            route_path="/v1/drivers/generic-web/deploy",
            idempotency_key=self.key,
            request_fingerprint=idempotency_request_fingerprint(
                route_path="/v1/drivers/generic-web/deploy", payload=fixture.request
            ),
            lease_owner="original-worker",
            response_trace_id="original-response",
            adapter=adapter,
        )
        self.assertEqual(outcome.status, "reconcile_required")
        self.assertTrue(self.store.list_deployment_records(), outcome.response_payload)
        assert outcome.record is not None
        self.original = outcome.record
        self.deployment = fixture.normal_record(outcome)
        fixture.provider.original_operation_key = build_provider_operation_key(
            scope=self.original.scope,
            route_path=self.original.route_path,
            idempotency_key=self.original.idempotency_key,
            request_fingerprint=self.original.request_fingerprint,
            reconciliation_key=self.original.reconciliation_key,
        )
        self.enterContext(
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=fixture.provider,
            )
        )

    def inspect(self) -> dict[str, Any]:
        status, result = _invoke_recovery(
            self.fixture.app,
            original_deploy=self.fixture.request,
            idempotency_key=self.key,
            reason="Abandon the unqualified testing fixture without claiming its outcome.",
        )
        self.assertEqual(status, 200, result)
        return result

    def apply(self, digest: str) -> tuple[int, dict[str, Any]]:
        return _invoke_recovery_apply(
            self.fixture.app,
            original_deploy=self.fixture.request,
            idempotency_key=self.key,
            reason="Abandon the unqualified testing fixture without claiming its outcome.",
            expected_recovery_digest=digest,
        )

    def record_legacy_fence(self) -> PublicIngressObservationRecord:
        from dataclasses import replace
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )
        from control_plane.workflows.public_ingress_monitor import (
            check_public_ingress_target,
            discover_public_ingress_monitor_targets,
            reconcile_public_ingress_incident,
        )

        checked_at = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(self.original.updated_at, field_name="updated_at")
            + timedelta(hours=1)
        )
        target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.check_name == "launchplane-deploy-fence"
        )
        observation = check_public_ingress_target(
            target=replace(target, provider_hold_snapshot=None, deploy_fence=self.original),
            checked_at=checked_at,
            timeout_seconds=1,
            http_get=Mock(side_effect=AssertionError("Unexpected HTTP probe")),
            tls_get=Mock(side_effect=AssertionError("Unexpected TLS probe")),
        )
        summary = (
            f"Deploy {self.key!r} started at {self.original.created_at} "
            f"and holds this target (reconcile_required, last at {self.original.provider_effect_phase}, "
            f"since {self.original.updated_at}). Every later deploy is refused until "
            "it is recovered with the generic-web deploy recovery dry-run."
        )
        observation = observation.model_copy(
            update={"targets": (observation.targets[0].model_copy(update={"summary": summary}),)}
        )
        incident = reconcile_public_ingress_incident(record_store=self.store, record=observation)[0]
        observation = observation.model_copy(
            update={
                "incident_id": incident.incident_id,
                "incident_event_id": incident.latest_material_event_id,
            }
        )
        self.store.write_public_ingress_observation_record(observation)
        return observation

    def test_legacy_opening_links_only_its_proved_completed_abandonment(self) -> None:
        from control_plane.contracts.product_incident_read_model import (
            build_product_environment_incident_detail,
        )

        observation = self.record_legacy_fence()
        coordinates = resolve_event_deploy_recovery_coordinates(
            store=self.store,
            product=self.fixture.profile.product,
            context=self.deployment.context,
            fingerprint=idempotency_request_fingerprint,
        )
        plan = self.inspect()
        self.assertEqual(self.apply(plan["recovery_digest"])[0], 202)
        detail = build_product_environment_incident_detail(
            record_store=self.store,
            product=observation.product,
            environment="testing",
            incident_id=observation.incident_id,
        )
        assert detail.opening_observation is not None
        self.assertEqual(
            detail.opening_observation.testing_recovery_reference, coordinates.reference
        )
        self.assertNotIn(self.key, detail.model_dump_json())

    def test_legacy_fence_recovers_only_with_complete_retained_abandonment_history(self) -> None:
        from dataclasses import replace
        from control_plane.provider_target_holds import project_provider_target_hold
        from control_plane.workflows.public_ingress_monitor import (
            discover_public_ingress_monitor_targets,
            check_public_ingress_target,
        )

        observation = self.record_legacy_fence()
        held_target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.check_name == observation.check_name
        )
        held_snapshot = held_target.provider_hold_snapshot
        assert held_snapshot is not None

        def render_unavailable(*, held: bool) -> tuple[Any, ...]:
            snapshot = replace(
                held_snapshot,
                holds=(project_provider_target_hold(self.original, None),) if held else (),
                unavailable_references=("provider-operation:unavailable",),
            )
            return check_public_ingress_target(
                target=replace(held_target, provider_hold_snapshot=snapshot),
                checked_at=observation.observed_at,
                timeout_seconds=1,
                http_get=Mock(side_effect=AssertionError("Unexpected HTTP probe")),
                tls_get=Mock(side_effect=AssertionError("Unexpected TLS probe")),
            ).targets

        plan = self.inspect()
        self.assertEqual(self.apply(plan["recovery_digest"])[0], 202)
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )
        from control_plane.contracts.public_ingress_monitoring import (
            public_ingress_material_fingerprint_sha256,
        )
        from control_plane.workflows.public_ingress_monitor import reconcile_public_ingress_incident
        from control_plane.provider_target_holds import (
            ProviderOperationAttentionReference,
            ProviderOperationReference,
        )

        assert observation.material_fingerprint is not None
        original_origin = self.store.read_provider_target_origin(self.original)
        assert original_origin is not None
        attention_fingerprint = observation.material_fingerprint.model_copy(
            update={
                "route_authority": observation.material_fingerprint.route_authority.model_copy(
                    update={
                        "target_keys": tuple(
                            sorted(
                                {
                                    *observation.material_fingerprint.route_authority.target_keys,
                                    ProviderOperationAttentionReference(
                                        self.original.record_id
                                    ).encode(),
                                    ProviderOperationReference(
                                        self.original.record_id, original_origin.operation_nonce
                                    ).encode(),
                                }
                            )
                        ),
                    }
                ),
            }
        )
        attention = observation.model_copy(
            update={
                "record_id": observation.record_id + "-attention",
                "observed_at": format_launchplane_mutation_timestamp(
                    parse_launchplane_mutation_timestamp(
                        observation.observed_at, field_name="observed_at"
                    )
                    + timedelta(seconds=1)
                ),
                "material_fingerprint": attention_fingerprint,
                "material_fingerprint_sha256": public_ingress_material_fingerprint_sha256(
                    attention_fingerprint
                ),
                "targets": render_unavailable(held=True),
            }
        )
        changed = reconcile_public_ingress_incident(record_store=self.store, record=attention)[0]
        self.store.write_public_ingress_observation_record(
            attention.model_copy(update={"incident_event_id": changed.latest_material_event_id})
        )
        fingerprint = attention_fingerprint.model_copy(
            update={
                "route_authority": attention_fingerprint.route_authority.model_copy(
                    update={
                        "target_keys": tuple(
                            sorted(
                                {
                                    *attention_fingerprint.route_authority.target_keys,
                                    "provider-operation:unavailable",
                                }
                            )
                        ),
                    }
                )
            }
        )
        missing = observation.model_copy(
            update={
                "record_id": observation.record_id + "-missing",
                "observed_at": format_launchplane_mutation_timestamp(
                    parse_launchplane_mutation_timestamp(
                        observation.observed_at, field_name="observed_at"
                    )
                    + timedelta(seconds=2)
                ),
                "material_fingerprint": fingerprint,
                "material_fingerprint_sha256": public_ingress_material_fingerprint_sha256(
                    fingerprint
                ),
                "targets": render_unavailable(held=False),
            }
        )
        updated = reconcile_public_ingress_incident(record_store=self.store, record=missing)[0]
        missing = missing.model_copy(update={"incident_event_id": updated.latest_material_event_id})
        self.store.write_public_ingress_observation_record(missing)
        target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.check_name == observation.check_name
        )
        result = check_public_ingress_target(
            target=target,
            checked_at=observation.observed_at,
            timeout_seconds=1,
            http_get=Mock(side_effect=AssertionError("Unexpected HTTP probe")),
            tls_get=Mock(side_effect=AssertionError("Unexpected TLS probe")),
        )
        self.assertEqual(result.status, "pass")
        from control_plane.workflows.public_ingress_monitor import run_public_ingress_monitor_once

        next_probe = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(observation.observed_at, field_name="observed_at")
            + timedelta(minutes=1)
        )
        with patch(
            "control_plane.workflows.public_ingress_monitor.discover_public_ingress_monitor_targets",
            return_value=(target,),
        ):
            published = run_public_ingress_monitor_once(
                record_store=self.store, checked_at=next_probe, notify=False
            )
        self.assertEqual(published.resolved_incident_count, 1)
        incident = self.store.read_public_ingress_incident_record(observation.incident_id)
        self.assertEqual(incident.resolution_reason, "recovered")

    def record_completed_later_fence(
        self,
    ) -> tuple[PublicIngressObservationRecord, LaunchplaneIdempotencyRecord]:
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )
        from control_plane.contracts.public_ingress_monitoring import (
            public_ingress_material_fingerprint_sha256,
        )
        from control_plane.workflows.public_ingress_monitor import (
            discover_public_ingress_monitor_targets,
            check_public_ingress_target,
            reconcile_public_ingress_incident,
        )

        opening = self.record_legacy_fence()
        plan = self.inspect()
        self.assertEqual(self.apply(plan["recovery_digest"])[0], 202)
        capsule = self.store.read_provider_target_origin(self.original)
        assert capsule is not None
        clock = parse_launchplane_mutation_timestamp(opening.observed_at, field_name="observed_at")
        created = format_launchplane_mutation_timestamp(clock + timedelta(minutes=1))
        with patch.object(self.store, "_database_mutation_timestamp", return_value=created):
            later = self.store.reserve_mutation(
                scope="fixture-operation",
                route_path="/fixture-operation",
                idempotency_key="later-operation",
                request_fingerprint="fixture-request",
                lease_owner="fixture-worker",
                lease_seconds=1,
                reconciliation_key="fixture-intent",
                provider_target_key=capsule.origin.target_key(),
                provider_target_origin=capsule.origin,
            ).record
        target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.check_name == opening.check_name
        )
        observed = format_launchplane_mutation_timestamp(clock + timedelta(minutes=2))
        record = check_public_ingress_target(
            target=target,
            checked_at=observed,
            timeout_seconds=1,
            http_get=Mock(side_effect=AssertionError("Unexpected HTTP probe")),
            tls_get=Mock(side_effect=AssertionError("Unexpected TLS probe")),
        )
        assert record.material_fingerprint is not None
        fingerprint = record.material_fingerprint.model_copy(
            update={
                "route_authority": record.material_fingerprint.route_authority.model_copy(
                    update={
                        "target_keys": tuple(
                            sorted(
                                {
                                    *record.material_fingerprint.route_authority.target_keys,
                                    "provider-operation:unavailable",
                                }
                            )
                        )
                    }
                )
            }
        )
        record = record.model_copy(
            update={
                "material_fingerprint": fingerprint,
                "material_fingerprint_sha256": public_ingress_material_fingerprint_sha256(
                    fingerprint
                ),
            }
        )
        incident = reconcile_public_ingress_incident(record_store=self.store, record=record)[0]
        self.store.write_public_ingress_observation_record(
            record.model_copy(
                update={
                    "incident_id": incident.incident_id,
                    "incident_event_id": incident.latest_material_event_id,
                }
            )
        )
        self.assertTrue(self.read_captured_fence_snapshot(record).unavailable_references)
        completed = later.model_copy(
            update={
                "state": "completed",
                "response_status_code": 500,
                "response_trace_id": "terminal-fixture-failure",
                "recorded_at": format_launchplane_mutation_timestamp(clock + timedelta(minutes=3)),
            }
        )
        self.store.write_idempotency_record(completed)
        return record, completed

    def read_captured_fence_snapshot(
        self, record: PublicIngressObservationRecord, *, extra: tuple[str, ...] = ()
    ) -> ProviderHoldLaneSnapshot:
        fingerprint = record.material_fingerprint
        assert fingerprint is not None
        return self.store.read_provider_hold_snapshot(
            product=record.product,
            context=record.context,
            instance="testing",
            references=(
                *(
                    value
                    for value in fingerprint.route_authority.target_keys
                    if value.startswith("provider-operation:")
                ),
                *extra,
            ),
        )

    def test_completed_exact_later_operation_settles_legacy_history_without_abandonment(
        self,
    ) -> None:
        record, _completed = self.record_completed_later_fence()
        snapshot = self.read_captured_fence_snapshot(record)
        self.assertFalse(snapshot.unavailable_references)
        self.assertFalse(snapshot.holds)

    def test_terminal_history_requires_current_epoch_custody_and_captured_attention(self) -> None:
        from copy import deepcopy
        from uuid import uuid4
        from sqlalchemy import select
        from control_plane.contracts.idempotency_record import (
            build_launchplane_mutation_reservation_id,
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )
        from control_plane.provider_target_holds import ProviderOperationAttentionReference
        from control_plane.storage.postgres import (
            LaunchplaneIdempotencyRow,
            ORIGINAL_MUTATION_INTENT_ROUTE,
        )

        record, completed = self.record_completed_later_fence()

        def snapshot(*extra: str) -> ProviderHoldLaneSnapshot:
            return self.read_captured_fence_snapshot(record, extra=extra)

        unrecorded = ProviderOperationAttentionReference(
            build_launchplane_mutation_reservation_id(
                scope=completed.scope,
                route_path=completed.route_path,
                idempotency_key=completed.idempotency_key + ":not-captured",
            )
        ).encode()
        self.assertIn(unrecorded, snapshot(unrecorded).unavailable_references)
        shifted = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(completed.created_at, field_name="created_at")
            + timedelta(seconds=1)
        )
        self.store.write_idempotency_record(
            completed.model_copy(update={"created_at": shifted, "updated_at": shifted})
        )
        try:
            self.assertTrue(snapshot().unavailable_references)
        finally:
            self.store.write_idempotency_record(completed)
        with self.store._session_factory() as session:
            row = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == completed.record_id,
                )
            )
            assert row is not None
            receipt = LaunchplaneIdempotencyRecord.model_validate(row.payload)
        for corruption in ("missing", "nonce", "scope"):
            with self.subTest(corruption=corruption):
                if corruption == "missing":
                    with self.store._session_factory.begin() as session:
                        session.delete(session.get(LaunchplaneIdempotencyRow, receipt.record_id))
                else:
                    payload = deepcopy(receipt.response_payload)
                    capsule = payload["provider_target_origin"]
                    if corruption == "nonce":
                        capsule["operation_nonce"] = str(uuid4())
                    else:
                        capsule["origin"]["product"] = "different-fixture-product"
                    self.store.write_idempotency_record(
                        receipt.model_copy(update={"response_payload": payload})
                    )
                try:
                    self.assertTrue(snapshot().unavailable_references)
                finally:
                    self.store.write_idempotency_record(receipt)
        self.assertFalse(snapshot().unavailable_references)

    def test_later_legacy_participant_without_archive_keeps_the_incident_open(self) -> None:
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        opening = self.record_legacy_fence()
        plan = self.inspect()
        self.assertEqual(self.apply(plan["recovery_digest"])[0], 202)
        later = opening.model_copy(
            update={
                "record_id": opening.record_id + "-later",
                "observed_at": format_launchplane_mutation_timestamp(
                    parse_launchplane_mutation_timestamp(
                        opening.observed_at, field_name="observed_at"
                    )
                    + timedelta(seconds=1)
                ),
                "targets": (
                    opening.targets[0].model_copy(
                        update={
                            "summary": opening.targets[0].summary.replace(
                                repr(self.key), repr(self.key + ":another")
                            )
                        }
                    ),
                ),
            }
        )
        self.store.write_public_ingress_observation_record(later)
        incident = self.store.read_public_ingress_incident_record(opening.incident_id)
        self.store.write_public_ingress_incident_record(
            incident.model_copy(
                update={
                    "latest_observation_id": later.record_id,
                    "latest_observed_at": later.observed_at,
                }
            )
        )
        snapshot = self.store.read_provider_hold_snapshot(
            product=opening.product,
            context=opening.context,
            instance="testing",
            references=("provider-operation:unavailable",),
        )
        self.assertEqual(snapshot.unavailable_references, ("provider-operation:unavailable",))

    def test_changed_abandonment_evidence_refuses_probe_publication(self) -> None:
        from control_plane.storage.postgres import LaunchplaneIdempotencyRow
        from control_plane.workflows.public_ingress_monitor import (
            discover_public_ingress_monitor_targets,
            check_public_ingress_target,
        )

        opening = self.record_legacy_fence()
        plan = self.inspect()
        self.assertEqual(self.apply(plan["recovery_digest"])[0], 202)
        target = next(
            target
            for target in discover_public_ingress_monitor_targets(self.store)
            if target.check_name == opening.check_name
        )
        observation = check_public_ingress_target(
            target=target,
            checked_at=opening.observed_at,
            timeout_seconds=1,
            http_get=Mock(side_effect=AssertionError("Unexpected HTTP probe")),
            tls_get=Mock(side_effect=AssertionError("Unexpected TLS probe")),
        )
        self.assertEqual(observation.status, "pass")
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneIdempotencyRow, self.original.record_id)
            assert row is not None
            payload = dict(row.payload)
            response = dict(payload["response_payload"])
            response["abandonment"] = {
                **response["abandonment"],
                "original_outcome": "claimed-success",
            }
            row.payload = {**payload, "response_payload": response}
        result = self.store.write_public_ingress_transition_with_outbox(
            observation=observation,
            incidents=(),
            expected_provider_hold_snapshot=target.provider_hold_snapshot,
        )
        self.assertEqual(result.status, "provider_source_changed")
        self.assertEqual(
            self.store.read_public_ingress_incident_record(opening.incident_id).status, "open"
        )

    def test_another_completed_incarnation_cannot_settle_the_legacy_opening(self) -> None:
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )
        from control_plane.event_deploy_recovery_reference import EventDeployRecoveryCoordinates
        from control_plane.storage.postgres import LaunchplaneIdempotencyRow

        opening = self.record_legacy_fence()
        plan = self.inspect()
        self.assertEqual(self.apply(plan["recovery_digest"])[0], 202)
        saved = self.store.read_idempotency_record_by_id(self.original.record_id)
        assert saved is not None
        changed_time = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(self.original.created_at, field_name="created_at")
            - timedelta(seconds=1)
        )
        original = self.original.model_copy(update={"created_at": changed_time})
        archive = {
            **saved.response_payload["abandonment"],
            "original": original.model_dump(mode="json"),
            "recovery_reference": EventDeployRecoveryCoordinates(
                original,
                GenericWebDeployEnvelope.model_validate(self.fixture.request),
                opening.context,
            ).reference,
        }
        changed = saved.model_copy(
            update={
                "created_at": changed_time,
                "response_payload": {**saved.response_payload, "abandonment": archive},
            }
        )
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneIdempotencyRow, changed.record_id)
            assert row is not None
            self.store._sync_idempotency_row(row, changed)
        self.assertEqual(self.store.read_testing_fence_recovery_reference(opening), "")
        snapshot = self.store.read_provider_hold_snapshot(
            product=opening.product,
            context=opening.context,
            instance="testing",
            references=("provider-operation:unavailable",),
        )
        self.assertEqual(snapshot.unavailable_references, ("provider-operation:unavailable",))

    def test_reviewed_abandonment_preserves_unknown_execution_and_lifecycle(self) -> None:
        plan = self.inspect()
        self.assertEqual(plan["proposed_action"], "abandon_testing_original")
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result"
        ) as deploy:
            status, result = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202, result)
        saved = self.store.read_idempotency_record(
            scope=self.original.scope,
            route_path=self.original.route_path,
            idempotency_key=self.key,
        )
        assert saved is not None
        self.assertEqual(saved.state, "completed")
        self.assertTrue(testing_attempt_was_settled(saved))
        self.assertEqual(
            saved.response_payload["abandonment"]["original"], self.original.model_dump(mode="json")
        )
        self.assertEqual(saved.response_payload["abandonment"]["original_outcome"], "unknown")
        after = self.store.read_deployment_record(self.deployment.record_id)
        self.assertEqual(execution_receipt(after), execution_receipt(self.deployment))
        self.assertEqual(
            recorded_application_lifecycle(after), recorded_application_lifecycle(self.deployment)
        )
        self.assertNotEqual(after.post_deploy_update.status, "pass")
        deploy.assert_not_called()

    def test_original_and_reference_replay_do_not_inspect_or_mutate_the_provider(self) -> None:
        coordinates = resolve_event_deploy_recovery_coordinates(
            store=self.store,
            product=self.fixture.profile.product,
            context=self.deployment.context,
            fingerprint=idempotency_request_fingerprint,
        )
        plan = self.inspect()
        status, first = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202, first)
        saved = self.store.read_idempotency_record(
            scope=self.original.scope, route_path=self.original.route_path, idempotency_key=self.key
        )
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
            side_effect=AssertionError(
                "A completed abandonment must replay without provider reads."
            ),
        ):
            status, replay = self.apply(plan["recovery_digest"])
            self.assertEqual(status, 202, replay)
            status, reference = _invoke_app(
                self.fixture.app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/apply",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload={
                    "product": self.fixture.profile.product,
                    "instance": "testing",
                    "recovery_reference": coordinates.reference,
                    "reason": "Abandon the unqualified testing fixture without claiming its outcome.",
                    "expected_recovery_digest": plan["recovery_digest"],
                },
            )
            self.assertEqual(status, 202, reference)
            status, readback = _invoke_app(
                self.fixture.app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/dry-run",
                authorization=f"Bearer {_OPERATOR_TOKEN}",
                payload={
                    "product": self.fixture.profile.product,
                    "instance": "testing",
                    "recovery_reference": coordinates.reference,
                    "reason": "Abandon the unqualified testing fixture without claiming its outcome.",
                },
            )
            self.assertEqual(status, 200, readback)
            self.assertEqual(readback["reservation_state"], "completed")
            self.assertEqual(readback["proposed_action"], "replay_completed")
            self.assertEqual(readback["recovery_digest"], first["recovery_digest"])
        self.assertEqual(reference["recovery_digest"], first["recovery_digest"])
        self.assertEqual(replay["recovery_digest"], first["recovery_digest"])
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.key,
            ),
            saved,
        )

    def test_production_adapter_cannot_use_testing_abandonment(self) -> None:
        adapter = self.fixture.normal_adapter()
        adapter._lane = adapter.lane.model_copy(update={"instance": "prod"})
        self.assertIsNone(
            inspect_testing_abandonment(
                self.store, adapter, self.original, self.fixture.provider.original_operation_key
            )
        )
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_legacy_original_without_saved_request_remains_held(self) -> None:
        legacy = GenericWebTestingAbandonmentTests()
        legacy.retain_event_request = False
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        self.assertEqual(legacy.inspect()["proposed_action"], "hold_unknown")

    def test_other_held_attempt_on_the_lane_refuses_abandonment(self) -> None:
        other = self.original.model_copy(
            update={"record_id": "other-held", "idempotency_key": self.key + ":other"}
        )
        for target in (self.original.provider_target_key, "another-target"):
            with (
                self.subTest(target=target),
                patch.object(
                    self.store,
                    "list_held_provider_target_reservations",
                    return_value=(
                        self.original,
                        other.model_copy(update={"provider_target_key": target}),
                    ),
                ),
            ):
                self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")

    def test_tampered_archive_cannot_prove_reference_readback(self) -> None:
        coordinates = resolve_event_deploy_recovery_coordinates(
            store=self.store,
            product=self.fixture.profile.product,
            context=self.deployment.context,
            fingerprint=idempotency_request_fingerprint,
        )
        plan = self.inspect()
        status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202)
        saved = self.store.read_abandoned_testing_original(
            self.fixture.profile.product, coordinates.reference
        )
        assert saved is not None
        archive = saved.response_payload["abandonment"]
        altered = saved.model_copy(
            update={
                "response_payload": {
                    **saved.response_payload,
                    "abandonment": {
                        **archive,
                        "original": {**archive["original"], "attempt": self.original.attempt + 1},
                    },
                }
            }
        )
        for changed in (
            altered,
            saved.model_copy(update={"provider_target_key": "changed-target"}),
        ):
            with self.subTest(record=changed.record_id):
                with patch.object(
                    self.store, "read_abandoned_testing_original", return_value=changed
                ):
                    status, result = _invoke_app(
                        self.fixture.app,
                        method="POST",
                        path="/v1/admin/generic-web/deploy-recovery/dry-run",
                        authorization=f"Bearer {_OPERATOR_TOKEN}",
                        payload={
                            "product": self.fixture.profile.product,
                            "instance": "testing",
                            "recovery_reference": coordinates.reference,
                            "reason": "Read the original disposition.",
                        },
                    )
                self.assertEqual(status, 409, result)

    def test_provider_reads_do_not_hold_global_authority_lock(self) -> None:
        plan = self.inspect()
        guarded = False

        @contextmanager
        def authority_guard() -> Iterator[Callable[[], None]]:
            nonlocal guarded
            guarded = True
            try:
                yield lambda: None
            finally:
                guarded = False

        observe = self.fixture.provider.observe_artifact_deploy

        def read_provider(**kwargs: Any) -> Any:
            self.assertFalse(guarded)
            return observe(**kwargs)

        with (
            patch.object(self.store, "serialize_disposable_testing_resources", authority_guard),
            patch.object(
                self.fixture.provider, "observe_artifact_deploy", side_effect=read_provider
            ),
        ):
            status, result = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 202, result)

    def test_lost_authority_lock_does_not_complete_original(self) -> None:
        plan = self.inspect()

        def lost() -> None:
            raise RuntimeError("Authority connection lost.")

        with patch.object(
            self.store, "serialize_disposable_testing_resources", return_value=nullcontext(lost)
        ):
            status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_historical_production_alias_keeps_the_original_held(self) -> None:
        target = self.store.read_provider_target_record(
            context_name=self.deployment.context, instance_name="testing"
        )
        targets = self.store.list_provider_target_records()
        with patch.object(
            self.store,
            "list_provider_target_records",
            return_value=(*targets, target.model_copy(update={"instance": "prod"})),
        ):
            self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_protective_observation_added_after_review_refuses_abandonment(self) -> None:
        plan = self.inspect()
        record_safety(
            self.store,
            self.fixture.profile.product,
            self.deployment.context,
            "testing",
            data="only_copy",
        )
        status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_expired_protective_reports_still_refuse_an_abandonment_plan(self) -> None:
        for facts in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
            {"production_resources": "separation_ready"},
        ):
            with self.subTest(facts=facts):
                record_safety(
                    self.store,
                    self.fixture.profile.product,
                    self.deployment.context,
                    "testing",
                    **facts,
                )
                observation = self.store.read_disposable_lane_safety(
                    self.fixture.profile.product, self.deployment.context, "testing"
                )
                assert observation is not None
                self.store.write_disposable_lane_safety(
                    observation.model_copy(
                        update={
                            "observed_at": observation.observed_at - timedelta(hours=1),
                            "expires_at": observation.expires_at - timedelta(hours=1),
                        }
                    )
                )
                self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_unproved_or_foreign_image_does_not_abandon_unknown_execution(self) -> None:
        observed = self.fixture.provider.observation
        for changes in (
            {"outcome": "unknown"},
            {"deployment_id": "foreign-image"},
            {"deployment_status": "error"},
        ):
            with self.subTest(changes=changes):
                self.fixture.provider.observation = observed.model_copy(update=changes)
                self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_active_execution_lock_refuses_abandonment(self) -> None:
        from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name

        plan = self.inspect()
        with self.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(self.original.provider_target_key)
        ) as check:
            self.assertIsNotNone(check)
            status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_missing_or_unfinished_provider_execution_is_not_abandonable(self) -> None:
        receipt = execution_receipt(self.deployment)
        assert receipt is not None
        read = self.store.read_deployment_record
        for entries in (None, (receipt.entries[-1].model_copy(update={"kind": "schedule_run"}),)):
            with self.subTest(entries=entries):
                evidence = dict(self.deployment.post_deploy_update.evidence)
                if entries is None:
                    evidence.pop(EXECUTION_EVIDENCE_KEY)
                else:
                    evidence[EXECUTION_EVIDENCE_KEY] = receipt.model_copy(
                        update={"entries": entries}
                    ).model_dump_json()
                changed = self.deployment.model_copy(
                    update={
                        "post_deploy_update": self.deployment.post_deploy_update.model_copy(
                            update={"evidence": evidence}
                        )
                    }
                )
                with patch.object(
                    self.store,
                    "read_deployment_record",
                    side_effect=lambda record_id: (
                        changed if record_id == changed.record_id else read(record_id)
                    ),
                ):
                    self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))

    def test_staff_hold_added_after_review_refuses_abandonment(self) -> None:
        plan = self.inspect()
        target = self.store.read_dokploy_target_record(
            context_name=self.deployment.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="Staff are using testing", purpose="staff_testing"
                            )
                        }
                    )
                }
            )
        )
        self.assertEqual(self.inspect()["proposed_action"], "hold_unknown")
        status, _ = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 409)
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))


class TestingAbandonmentReconcileTests(unittest.TestCase):
    def test_refused_artifact_is_checked_before_provider_resolution(self) -> None:
        scenario = reconciliation.ProductReconcileGenericWebTestingTests()
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        scenario.prepare_application_runtime_test()
        scenario.request()

        def refuse(_profile: object, plan: dict[str, object], _desired: object) -> bool:
            plan.update(action="wait", held=True, reason="artifact_scan_refused")
            return False

        with (
            patch("control_plane.product_reconcile._scan_deploy_artifact", side_effect=refuse),
            patch.object(
                scenario.application_inputs_provider,
                "resolve_deploy_target",
                side_effect=AssertionError("A refused image must not need the provider."),
            ),
        ):
            result = scenario.reconcile()
        self.assertEqual(result["reason"], "artifact_scan_refused")

    def test_normal_reconcile_qualifies_a_successor_with_the_captured_schedule_baseline(
        self,
    ) -> None:
        self.qualify_successor(abandon_during_reconcile=False)

    def test_abandonment_after_initial_read_starts_successor_in_the_same_pass(self) -> None:
        self.qualify_successor(abandon_during_reconcile=True)

    def test_refused_successor_scan_reports_its_own_key_without_deploying(self) -> None:
        self.qualify_successor(abandon_during_reconcile=False, refuse_successor=True)

    def qualify_successor(
        self, *, abandon_during_reconcile: bool, refuse_successor: bool = False
    ) -> None:
        scenario = reconciliation.ProductReconcileGenericWebTestingTests()
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        triggered = scenario.prepare_application_runtime_test()
        scenario.application_input_schedule()
        profile = scenario.store.read_product_profile_record("site")
        lane = profile.lanes[0].model_copy(
            update={
                "generic_web_extensions": profile.lanes[0].generic_web_extensions.model_copy(
                    update={"verification_extension": "verireel-testing"}
                )
            }
        )
        scenario.store.write_product_profile_record(
            profile.model_copy(update={"lanes": (lane, *profile.lanes[1:])})
        )
        calls: list[str] = []

        def execute_verireel_testing_post_deploy(*args: object) -> PostDeployUpdateEvidence:
            context = cast(GenericWebPostDeployContext, args[2])
            guard = cast(GenericWebPostDeployGuard, args[3])
            calls.append(context.deployment_record_id)
            guard.checkpoint_effect("post_deploy_fixture_signup")
            if len(calls) == 1:
                raise ProviderMutationUnknownError("The original fixture response was lost.")
            return cast(PostDeployUpdateEvidence, scenario._testing_qualification_evidence(*args))

        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_testing_post_deploy",
                execute_verireel_testing_post_deploy,
            ),
            patch(
                "control_plane.product_reconcile._current_testing_deployment_id", return_value=None
            ),
        ):
            scenario.request()
            first = scenario.run_once()
            self.assertEqual(first.state, "failed")
            (original,) = scenario.store.list_held_provider_target_reservations()
            before = scenario.store.read_deployment_record(calls[0])
            baseline = recorded_application_lifecycle(before)
            assert baseline is not None
            self.assertTrue(baseline.originals)
            disabled = dict(scenario.input_schedule)
            self.assertFalse(disabled["enabled"])
            snapshot = decode_generic_web_provider_reconciliation_target(
                original.reconciliation_key
            )
            app = _create_recovery_app(
                root=scenario.root,
                store=scenario.store,
                actions=("generic_web_deploy.execute",),
                contexts=("cm",),
                authz_policy=local_operator_policy(
                    actions=("generic_web_deploy.execute",), products=("site",), contexts=("cm",)
                ),
            )
            body = {
                "product": "site",
                "instance": "testing",
                "original_deploy": snapshot.original_event_deploy,
                "reason": "Abandon this fixture and let normal reconciliation continue.",
            }
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                    return_value=scenario.application_inputs_provider,
                ),
                patch.object(
                    scenario.store,
                    "serialize_disposable_testing_resources",
                    side_effect=lambda: nullcontext(lambda: None),
                ),
            ):
                status, plan = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization=f"Bearer {_OPERATOR_TOKEN}",
                    payload=body,
                    headers={"Idempotency-Key": original.idempotency_key},
                )
                self.assertEqual(status, 200, plan)
                self.assertEqual(plan["proposed_action"], "abandon_testing_original")

                def abandon() -> None:
                    status, applied = _invoke_app(
                        app,
                        method="POST",
                        path="/v1/admin/generic-web/deploy-recovery/apply",
                        authorization=f"Bearer {_OPERATOR_TOKEN}",
                        payload={**body, "expected_recovery_digest": plan["recovery_digest"]},
                        headers={"Idempotency-Key": original.idempotency_key},
                    )
                    self.assertEqual(status, 202, applied)
                    self.assertEqual(scenario.input_schedule, disabled)
                    self.assertEqual(
                        scenario.store.read_deployment_record(before.record_id), before
                    )

                lock = scenario.store.testing_replacement_execution_lock

                @contextmanager
                def abandon_before_lock(name: str) -> Iterator[Callable[[], None] | None]:
                    with patch.object(scenario.store, "testing_replacement_execution_lock", lock):
                        abandon()
                        with lock(name) as check:
                            yield check

                if not abandon_during_reconcile:
                    abandon()
                if refuse_successor:
                    from control_plane.artifact_scan import ArtifactScanResult

                    scenario.artifact_scanner.side_effect = lambda image: ArtifactScanResult(
                        image_reference=image,
                        status="fail",
                        reason="vulnerability",
                        scanned_at="2026-10-22T20:00:00Z",
                    )
                scenario.request()
                prior_scans = scenario.artifact_scanner.call_count
                with patch.object(
                    scenario.store,
                    "testing_replacement_execution_lock",
                    abandon_before_lock if abandon_during_reconcile else lock,
                ):
                    successor = scenario.reconcile()
                self.assertEqual(scenario.artifact_scanner.call_count, prior_scans + 1)
        self.assertNotEqual(successor["deploy_idempotency_key"], original.idempotency_key)
        if refuse_successor:
            self.assertEqual(successor["reason"], "artifact_scan_refused")
            self.assertTrue(successor["held"])
            self.assertEqual(calls, [before.record_id])
            self.assertEqual(scenario.input_schedule, disabled)
            return
        self.assertEqual(successor["post_deploy_status"], "pass")
        self.assertEqual(len(triggered), len(calls))
        self.assertEqual(len(set(calls)), len(calls))
        after = scenario.store.read_deployment_record(str(successor["deployment_record_id"]))
        self.assertEqual(calls, [before.record_id, after.record_id])
        next_baseline = recorded_application_lifecycle(after)
        assert next_baseline is not None and baseline is not None
        self.assertEqual(next_baseline.originals, baseline.originals)
        self.assertEqual(next_baseline.managed_names, baseline.managed_names)
        self.assertEqual(next_baseline.previous_artifact_id, baseline.previous_artifact_id)
        self.assertTrue(scenario.input_schedule["enabled"])
