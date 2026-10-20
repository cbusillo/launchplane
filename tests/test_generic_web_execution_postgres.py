"""Original execution publication fences on the real PostgreSQL storage path."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.idempotency_record import format_launchplane_mutation_timestamp
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.provider_operations import (
    build_provider_operation_key,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.evidence_ingestion import apply_deployment_evidence
from control_plane.workflows.generic_web_deploy import build_observed_generic_web_deploy
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
)
from control_plane.workflows.generic_web_execution import (
    EXECUTION_EVIDENCE_KEY,
    ExecutionReceipt,
    evidence_digest,
)
from control_plane.workflows.inventory import build_environment_inventory
from tests.test_generic_web_deploy import _FakeGenericWebDeployProvider, _profile, _request
from tests.test_postgres_integration import _head_postgres_database


class OriginalExecutionPostgresTests(unittest.TestCase):
    def test_replaced_lease_rejects_actual_generic_adapter_publication(self) -> None:
        with (
            _head_postgres_database() as url,
            closing(PostgresRecordStore(database_url=url)) as first,
            closing(PostgresRecordStore(database_url=url)) as second,
        ):
            profile = _profile()
            first.write_product_profile_record(profile)
            provider = _FakeGenericWebDeployProvider(
                observation=GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="image",
                    started_at=datetime.now(UTC).isoformat(),
                    finished_at=datetime.now(UTC).isoformat(),
                )
            )
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=Path("."),
                record_store=first,
                deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
                profile=profile,
                lane=profile.lanes[0],
                trace_id="old-worker",
                deploy_provider=provider,
            )
            target = adapter._resolve_deploy_target()
            first.write_dokploy_target_record(
                DokployTargetRecord(
                    context=profile.lanes[0].context,
                    instance="testing",
                    target_type="application",
                    target_name=target.resolved_target.target_name,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            first.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=profile.lanes[0].context,
                    instance="testing",
                    target_id=target.resolved_target.target_id,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            first.write_provider_target_record(
                ProviderTargetRecord(
                    context=profile.lanes[0].context,
                    instance="testing",
                    provider_id=provider.provider_id,
                    target_category="application",
                    provider_target_type="application",
                    target_id=target.resolved_target.target_id,
                    display_name="fixture",
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            at_publication, release = Event(), Event()
            now = [datetime.now(UTC)]
            publish = first.publish_generic_web_testing_result
            record_ids: list[str] = []

            def pause_write(**kwargs: Any) -> None:
                record = kwargs["deployment"]
                if record.deploy.status == "pass":
                    record_ids.append(record.record_id)
                    at_publication.set()
                    if not release.wait(15):
                        raise RuntimeError("Coordinator did not replace the lease")
                publish(**kwargs)

            def run() -> Any:
                return run_durable_provider_operation(
                    store=first,
                    scope="fixture",
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="original",
                    request_fingerprint="original-request",
                    lease_owner="old-worker",
                    response_trace_id="original",
                    adapter=adapter,
                    lease_seconds=300,
                    heartbeat_interval_seconds=60,
                )

            with (
                patch.object(
                    PostgresRecordStore,
                    "_database_mutation_timestamp",
                    side_effect=lambda _session: format_launchplane_mutation_timestamp(now[0]),
                ),
                patch.object(first, "publish_generic_web_testing_result", side_effect=pause_write),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                old = pool.submit(run)
                try:
                    self.assertTrue(at_publication.wait(15))
                    reservation = second.read_idempotency_record(
                        scope="fixture",
                        route_path="/v1/drivers/generic-web/deploy",
                        idempotency_key="original",
                    )
                    assert reservation is not None
                    now[0] += timedelta(seconds=301)
                    reconciled = second.mark_mutation_reconcile_required(
                        reservation=reservation, reconciliation_key=reservation.reconciliation_key
                    )
                    assert reconciled.record is not None
                    replacement = second.retry_reconciled_mutation(
                        reservation=reconciled.record,
                        lease_owner="replacement",
                        preserve_effect_checkpoint=True,
                    )
                    self.assertEqual(replacement.status, "acquired")
                finally:
                    release.set()
                outcome = old.result(timeout=15)
            self.assertEqual(outcome.status, "reconcile_required")
            self.assertEqual(first.read_deployment_record(record_ids[0]).deploy.status, "pending")
            current = second.read_idempotency_record(
                scope="fixture",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key="original",
            )
            assert current is not None
            self.assertEqual(current.lease_owner, "replacement")
            self.assertEqual(current.state, "running")

    def test_automatic_observer_compares_records_after_supported_ingestion_correction(self) -> None:
        with (
            _head_postgres_database() as url,
            closing(PostgresRecordStore(database_url=url)) as first,
            closing(PostgresRecordStore(database_url=url)) as second,
        ):
            profile = _profile()
            first.write_product_profile_record(profile)
            provider = _FakeGenericWebDeployProvider(
                observation=GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="image",
                    started_at=datetime.now(UTC).isoformat(),
                    finished_at=datetime.now(UTC).isoformat(),
                )
            )
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=Path("."),
                record_store=first,
                deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
                profile=profile,
                lane=profile.lanes[0],
                trace_id="observer",
                deploy_provider=provider,
            )
            target = adapter._resolve_deploy_target()
            reserved = first.reserve_mutation(
                scope="fixture",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key="original",
                request_fingerprint="original-request",
                lease_owner="original",
                reconciliation_key=adapter.reconciliation_key(),
                provider_target_key=adapter.target_key(),
            )
            assert reserved.record is not None
            reservation = reserved.record
            operation = build_provider_operation_key(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
                reconciliation_key=reservation.reconciliation_key,
            )
            record, _records, _result = build_observed_generic_web_deploy(
                record_store=first,
                profile=profile,
                lane=profile.lanes[0],
                resolved_deploy_target=target,
                observation=provider.observation,
                deployment_record_id=adapter._deployment_record_id(operation),
                post_deploy_unobserved=False,
            )
            record.post_deploy_update.evidence[EXECUTION_EVIDENCE_KEY] = ExecutionReceipt(
                inventory_baseline_digest=evidence_digest(None)
            ).model_dump_json()
            with first.provider_evidence_guard(lambda: reservation):
                first.write_deployment_record(record)
                first.write_environment_inventory(
                    build_environment_inventory(
                        deployment_record=record, updated_at=record.deploy.finished_at
                    )
                )
            marked = first.mark_mutation_reconcile_required(
                reservation=reservation, reconciliation_key=reservation.reconciliation_key
            )
            assert marked.record is not None
            observed, release = Event(), Event()
            adoption = first.adopt_reconciled_mutation

            def pause_adoption(**kwargs: Any) -> Any:
                self.assertTrue(kwargs["expected_binding_evidence"])
                observed.set()
                if not release.wait(15):
                    raise RuntimeError("Coordinator did not correct evidence")
                return adoption(**kwargs)

            def run() -> Any:
                return run_durable_provider_operation(
                    store=first,
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    request_fingerprint=reservation.request_fingerprint,
                    lease_owner="observer",
                    response_trace_id="observer",
                    adapter=adapter,
                )

            with (
                patch.object(first, "adopt_reconciled_mutation", side_effect=pause_adoption),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                waiting = pool.submit(run)
                try:
                    self.assertTrue(observed.wait(15))
                    corrected = record.model_copy(
                        update={
                            "post_deploy_update": record.post_deploy_update.model_copy(
                                update={
                                    "attempted": True,
                                    "status": "fail",
                                    "detail": "Corrected qualification",
                                }
                            )
                        }
                    )
                    apply_deployment_evidence(record_store=second, deployment_record=corrected)
                    self.assertEqual(
                        second.read_idempotency_record(
                            scope=reservation.scope,
                            route_path=reservation.route_path,
                            idempotency_key=reservation.idempotency_key,
                        ),
                        marked.record,
                    )
                finally:
                    release.set()
                outcome = waiting.result(timeout=15)
            self.assertEqual(outcome.status, "reconcile_required")
            self.assertEqual(
                first.read_deployment_record(record.record_id).post_deploy_update.status, "fail"
            )
