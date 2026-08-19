import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
from unittest.mock import patch

from click import ClickException

from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
)
from control_plane.storage.postgres import (
    ExistingMutationReservationLookupResult,
    PostgresRecordStore,
)
from control_plane.workflows.generic_web_deploy import GenericWebDeployResult
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebLegacyDeploymentCorrelation,
    GenericWebLegacyDeploymentCorrelationEvidence,
    GenericWebProviderDeploymentObservation,
    GenericWebResolvedDeployTarget,
    build_generic_web_provider_reconciliation_key,
    build_generic_web_provider_target_key,
)
from tests.support.auth import StubVerifier, identity, local_operator_policy
from tests.support.profiles import product_profile_payload as _product_profile_payload
from tests.support.stores import sqlite_database_url as _sqlite_database_url
from tests.test_service import _invoke_app, create_launchplane_fastapi_test_app


_RECOVERY_ROUTE = "/v1/admin/generic-web/deploy-recovery/dry-run"
_PROVIDER_EVIDENCE_ROUTE = "/v1/admin/generic-web/deploy-recovery/provider-evidence"
_RECOVERY_APPLY_ROUTE = "/v1/admin/generic-web/deploy-recovery/apply"
_OPERATOR_TOKEN = "local-operator-token"


def _create_recovery_app(
    *,
    root: Path,
    store: PostgresRecordStore,
    actions: tuple[str, ...] = ("generic_web_deploy.execute",),
    contexts: tuple[str, ...] = ("sellyouroutboard-testing",),
    authz_policy: LaunchplaneAuthzPolicy | None = None,
) -> Any:
    return create_launchplane_fastapi_test_app(
        local_record_store_for_tests=store,
        state_dir=root / "state",
        verifier=StubVerifier(identity()),
        authz_policy=authz_policy
        or local_operator_policy(
            actions=actions,
            products=("sellyouroutboard",),
            contexts=contexts,
        ),
        control_plane_root_path=root,
        bearer_identity_config=BearerIdentityConfig(
            local_operator_token=_OPERATOR_TOKEN,
            local_operator_subject="local-owner-agent",
            local_operator_token_label="local-owner-write",
        ),
    )


def _invoke_recovery(
    app: Any,
    *,
    original_deploy: dict[str, object],
    idempotency_key: str,
    reason: str,
) -> tuple[int, dict[str, Any]]:
    return _invoke_app(
        app,
        method="POST",
        path=_RECOVERY_ROUTE,
        authorization=f"Bearer {_OPERATOR_TOKEN}",
        payload={
            "schema_version": 1,
            "product": "sellyouroutboard",
            "instance": "testing",
            "original_deploy": original_deploy,
            "reason": reason,
        },
        headers={"Idempotency-Key": idempotency_key},
    )


def _invoke_recovery_apply(
    app: Any,
    *,
    original_deploy: dict[str, object],
    idempotency_key: str,
    reason: str,
    expected_recovery_digest: str,
) -> tuple[int, dict[str, Any]]:
    return _invoke_app(
        app,
        method="POST",
        path=_RECOVERY_APPLY_ROUTE,
        authorization=f"Bearer {_OPERATOR_TOKEN}",
        payload={
            "schema_version": 1,
            "product": "sellyouroutboard",
            "instance": "testing",
            "original_deploy": original_deploy,
            "reason": reason,
            "expected_recovery_digest": expected_recovery_digest,
        },
        headers={"Idempotency-Key": idempotency_key},
    )


def _invoke_provider_evidence(
    app: Any,
    *,
    original_deploy: dict[str, object],
    idempotency_key: str,
    reason: str,
) -> tuple[int, dict[str, Any]]:
    return _invoke_app(
        app,
        method="POST",
        path=_PROVIDER_EVIDENCE_ROUTE,
        authorization=f"Bearer {_OPERATOR_TOKEN}",
        payload={
            "schema_version": 1,
            "product": "sellyouroutboard",
            "instance": "testing",
            "original_deploy": original_deploy,
            "reason": reason,
        },
        headers={"Idempotency-Key": idempotency_key},
    )


def _generic_web_deploy_result(
    *,
    deployment_record_id: str = "deployment-syo-testing",
    deploy_status: Literal["pass", "fail"] = "pass",
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped",
    error_message: str = "",
) -> GenericWebDeployResult:
    return GenericWebDeployResult(
        deployment_record_id=deployment_record_id,
        deploy_status=deploy_status,
        deploy_started_at="2026-05-26T02:00:00Z",
        deploy_finished_at="2026-05-26T02:05:00Z",
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="testing",
        target_name="syo-testing",
        target_id="app-syo-testing",
        target_category="application",
        provider_id="dokploy",
        provider_target_type="application",
        post_deploy_status=post_deploy_status,
        error_message=error_message,
    )


def _generic_web_recovery_original_deploy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "deploy": {
            "schema_version": 1,
            "product": "sellyouroutboard",
            "instance": "testing",
            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            "source_git_ref": "abc123",
        },
    }


def _generic_web_recovery_target(
    *, context: str = "sellyouroutboard-testing"
) -> GenericWebResolvedDeployTarget:
    return GenericWebResolvedDeployTarget(
        ship_request=ShipRequest(
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            context=context,
            instance="testing",
            source_git_ref="abc123",
            target_name="syo-testing",
            target_type="application",
            provider_id="dokploy",
            target_category="application",
            provider_target_type="application",
            deploy_mode="application",
            provider_deploy_mode="application",
            destination_health=HealthcheckEvidence(status="skipped"),
        ),
        resolved_target=ResolvedTargetEvidence(
            target_type="application",
            target_id="app-syo-testing",
            target_name="syo-testing",
        ),
        deploy_timeout_seconds=900,
    )


def _legacy_generic_web_reconciliation_key(
    target: GenericWebResolvedDeployTarget,
) -> str:
    ship_request = target.ship_request
    legacy_snapshot = {
        "schema_version": 1,
        "context": ship_request.context,
        "instance": ship_request.instance,
        "provider_id": ship_request.provider_id,
        "target_category": ship_request.target_category,
        "provider_target_type": ship_request.provider_target_type,
        "target_type": target.resolved_target.target_type,
        "target_id": target.resolved_target.target_id,
        "target_name": target.resolved_target.target_name,
        "deploy_mode": ship_request.deploy_mode,
        "provider_deploy_mode": ship_request.provider_deploy_mode,
        "deploy_timeout_seconds": target.deploy_timeout_seconds,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(legacy_snapshot, separators=(",", ":")).encode()
    ).decode("ascii")
    return f"generic-web-provider-target:{encoded.rstrip('=')}"


def _generic_web_recovery_reservation(
    *,
    original_deploy: dict[str, object],
    idempotency_key: str,
    state: Literal["running", "reconcile_required"] = "reconcile_required",
    context: str = "sellyouroutboard-testing",
    lease_expires_at: str = "2099-08-16T18:00:00Z",
    provider_effect_phase: str = "target_update",
    provider_target_key: str | None = None,
    legacy_snapshot_without_product: bool = False,
) -> LaunchplaneIdempotencyRecord:
    target = _generic_web_recovery_target(context=context)
    reconciliation_key = (
        _legacy_generic_web_reconciliation_key(target)
        if legacy_snapshot_without_product
        else build_generic_web_provider_reconciliation_key(
            target,
            product="sellyouroutboard",
        )
    )
    return LaunchplaneIdempotencyRecord(
        record_id=f"idempotency-{idempotency_key}",
        scope="legacy/github-actions/scope",
        route_path="/v1/drivers/generic-web/deploy",
        idempotency_key=idempotency_key,
        request_fingerprint=idempotency_request_fingerprint(
            route_path="/v1/drivers/generic-web/deploy",
            payload=original_deploy,
        ),
        state=state,
        lease_owner="legacy-worker",
        lease_expires_at=lease_expires_at if state == "running" else "",
        reconciliation_key=reconciliation_key,
        provider_target_key=(
            build_generic_web_provider_target_key(target)
            if provider_target_key is None
            else provider_target_key
        ),
        provider_effect_phase=provider_effect_phase,
        provider_effect_started_at=("2026-08-15T12:00:00Z" if provider_effect_phase else ""),
        created_at="2026-08-15T11:55:00Z",
        updated_at="2026-08-15T12:00:00Z",
    )


def _write_generic_web_recovery_reservation(
    store: PostgresRecordStore,
    reservation: LaunchplaneIdempotencyRecord,
) -> LaunchplaneIdempotencyRecord:
    reserved = store.reserve_mutation(
        scope=reservation.scope,
        route_path=reservation.route_path,
        idempotency_key=reservation.idempotency_key,
        request_fingerprint=reservation.request_fingerprint,
        lease_owner=reservation.lease_owner,
        reconciliation_key=reservation.reconciliation_key,
        provider_target_key=reservation.provider_target_key,
    ).record
    if reservation.provider_effect_phase:
        checkpointed = store.checkpoint_mutation_provider_effect(
            reservation=reserved,
            effect_phase=reservation.provider_effect_phase,
        )
        assert checkpointed.record is not None
        reserved = checkpointed.record
    if reservation.state == "reconcile_required":
        reconciled = store.mark_mutation_reconcile_required(
            reservation=reserved,
            reconciliation_key=reservation.reconciliation_key,
        )
        assert reconciled.record is not None
        reserved = reconciled.record
    return reserved


class _RecoveryObservationProvider:
    provider_id = "dokploy"
    delegated_executor = "test"

    def __init__(self, observation: GenericWebProviderDeploymentObservation) -> None:
        self.observation = observation
        self.observation_calls = 0

    def observe_artifact_deploy(self, **_kwargs: object) -> GenericWebProviderDeploymentObservation:
        self.observation_calls += 1
        return self.observation


class _FailingRecoveryObservationProvider(_RecoveryObservationProvider):
    def observe_artifact_deploy(self, **_kwargs: object) -> GenericWebProviderDeploymentObservation:
        self.observation_calls += 1
        raise ClickException("provider read failed")


def _legacy_correlation(
    *,
    deployment_id_sha256: str = "1" * 64,
    deployment_title_sha256: str = "2" * 64,
    artifact_reference_sha256: str = "3" * 64,
) -> GenericWebLegacyDeploymentCorrelation:
    return GenericWebLegacyDeploymentCorrelation(
        observation=GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="provider-legacy-deployment-secret",
            started_at="2026-08-15T12:00:02.000000Z",
            finished_at="2026-08-15T12:00:20.000000Z",
        ),
        digest_evidence=GenericWebLegacyDeploymentCorrelationEvidence(
            deployment_id_sha256=deployment_id_sha256,
            deployment_title_sha256=deployment_title_sha256,
            deployment_created_at="2026-08-15T12:00:01.000000Z",
            deployment_started_at="2026-08-15T12:00:02.000000Z",
            deployment_finished_at="2026-08-15T12:00:20.000000Z",
            artifact_reference_sha256=artifact_reference_sha256,
        ),
    )


class _LegacyRecoveryObservationProvider(_RecoveryObservationProvider):
    def __init__(
        self,
        correlations: tuple[GenericWebLegacyDeploymentCorrelation | BaseException, ...],
    ) -> None:
        super().__init__(GenericWebProviderDeploymentObservation(outcome="absent"))
        self.correlations = list(correlations)
        self.legacy_observation_calls = 0

    def observe_legacy_artifact_deploy(
        self, **_kwargs: object
    ) -> GenericWebLegacyDeploymentCorrelation:
        if self.observation_calls != self.legacy_observation_calls + 1:
            raise AssertionError("legacy observation ran before exact-title observation")
        self.legacy_observation_calls += 1
        if not self.correlations:
            raise AssertionError("unexpected legacy observation")
        correlation = self.correlations.pop(0)
        if isinstance(correlation, BaseException):
            raise correlation
        return correlation


class GenericWebDeployRecoveryHttpTests(unittest.TestCase):
    def test_generic_web_deploy_recovery_dry_run_replays_without_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "deploy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            }
            idempotency_key = "legacy-generic-web-deploy"
            reservation = LaunchplaneIdempotencyRecord(
                record_id="idempotency-legacy-generic-web-deploy",
                scope="legacy/github-actions/scope",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key=idempotency_key,
                request_fingerprint=idempotency_request_fingerprint(
                    route_path="/v1/drivers/generic-web/deploy",
                    payload=original_deploy,
                ),
                response_status_code=202,
                response_trace_id="trace-original-deploy",
                recorded_at="2026-08-15T12:00:00Z",
                response_payload={
                    "status": "accepted",
                    "trace_id": "trace-original-deploy",
                    "records": {"deployment_record_id": "deployment-original"},
                    "result": _generic_web_deploy_result(
                        deployment_record_id="deployment-original"
                    ).model_dump(mode="json"),
                },
            )
            store.write_idempotency_record(reservation)
            app = _create_recovery_app(root=root, store=store)
            before = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )

            with (
                patch.object(store, "reserve_mutation", side_effect=AssertionError("mutation")),
                patch.object(
                    store,
                    "write_deployment_record",
                    side_effect=AssertionError("deployment write"),
                ),
                patch.object(
                    store,
                    "write_environment_inventory",
                    side_effect=AssertionError("inventory write"),
                ),
                patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=AssertionError("idempotency write"),
                ),
            ):
                status_code, payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=idempotency_key,
                    reason="Inspect the legacy reservation before recovery.",
                )
                apply_status, apply_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=idempotency_key,
                    reason="Inspect the legacy reservation before recovery.",
                    expected_recovery_digest=payload["recovery_digest"],
                )

            after = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            deployments = store.list_deployment_records()
            inventory = store.list_environment_inventory()
            store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(apply_status, 202)
        self.assertEqual(apply_payload["recovery_action"], "replay_completed")
        self.assertEqual(apply_payload["recovery_digest"], payload["recovery_digest"])
        self.assertEqual(payload["proposed_action"], "replay_completed")
        self.assertEqual(payload["provider_outcome"], "not_inspected")
        self.assertEqual(payload["provider_status"], "pass")
        self.assertRegex(payload["recovery_digest"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(idempotency_key, serialized)
        self.assertNotIn(reservation.scope, serialized)
        self.assertNotIn("trace-original-deploy", serialized)
        self.assertEqual(after, before)
        self.assertEqual(deployments, ())
        self.assertEqual(inventory, ())

    def test_generic_web_deploy_recovery_missing_reservation_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = _create_recovery_app(root=root, store=store)
            status_code, payload = _invoke_recovery(
                app,
                original_deploy=_generic_web_recovery_original_deploy(),
                idempotency_key="missing-legacy-reservation",
                reason="Inspect a suspected legacy reservation.",
            )
            store.close()

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "reservation_not_found")

    def test_generic_web_deploy_recovery_denies_before_reservation_lookup(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = _create_recovery_app(
                root=root,
                store=store,
                actions=("generic_web_preview.execute",),
            )
            with patch.object(
                store,
                "lookup_existing_mutation_reservation",
                side_effect=AssertionError("lookup disclosure"),
            ):
                status_code, payload = _invoke_recovery(
                    app,
                    original_deploy=_generic_web_recovery_original_deploy(),
                    idempotency_key="legacy-deploy-key",
                    reason="Verify authorization before lookup.",
                )
            store.close()

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_deploy_recovery_rejects_conflict_and_ambiguity(self) -> None:
        for expected_code, scopes, stored_fingerprint in (
            ("reservation_conflict", ("scope-a",), "different-fingerprint"),
            ("reservation_ambiguous", ("scope-a", "scope-b"), "exact"),
        ):
            with (
                self.subTest(expected_code=expected_code),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                exact_fingerprint = idempotency_request_fingerprint(
                    route_path="/v1/drivers/generic-web/deploy",
                    payload=original_deploy,
                )
                for index, scope in enumerate(scopes):
                    store.reserve_mutation(
                        scope=scope,
                        route_path="/v1/drivers/generic-web/deploy",
                        idempotency_key="legacy-deploy-key",
                        request_fingerprint=(
                            exact_fingerprint
                            if stored_fingerprint == "exact"
                            else stored_fingerprint
                        ),
                        lease_owner=f"worker-{index}",
                    )
                app = _create_recovery_app(root=root, store=store)
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Verify exact cross-scope lookup.",
                    },
                    headers={"Idempotency-Key": "legacy-deploy-key"},
                )
                store.close()

            self.assertEqual(status_code, 409)
            self.assertEqual(payload["error"]["code"], expected_code)

    def test_generic_web_deploy_recovery_waits_at_active_lease_boundary_without_writes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _generic_web_recovery_reservation(
                original_deploy=original_deploy,
                idempotency_key="active-lease",
                state="running",
            )
            reservation = _write_generic_web_recovery_reservation(store, reservation)
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="unknown")
            )
            app = _create_recovery_app(root=root, store=store)
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                patch.object(
                    store, "write_deployment_record", side_effect=AssertionError("deployment write")
                ),
                patch.object(
                    store,
                    "write_environment_inventory",
                    side_effect=AssertionError("inventory write"),
                ),
                patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=AssertionError("idempotency write"),
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Verify active lease fencing.",
                    },
                    headers={"Idempotency-Key": reservation.idempotency_key},
                )
            provider.observation = GenericWebProviderDeploymentObservation(outcome="absent")
            boundary_lookup = ExistingMutationReservationLookupResult(
                status="found",
                record=reservation,
                observed_at=reservation.lease_expires_at,
            )
            with (
                patch.object(
                    store,
                    "lookup_existing_mutation_reservation",
                    return_value=boundary_lookup,
                ),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
            ):
                boundary_status_code, boundary_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Verify the exact lease expiry boundary.",
                    },
                    headers={"Idempotency-Key": reservation.idempotency_key},
                )
            store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["proposed_action"], "wait_for_active_lease")
        self.assertEqual(boundary_status_code, 200)
        self.assertEqual(boundary_payload["proposed_action"], "retry_original_operation")
        self.assertEqual(provider.observation_calls, 1)

    def test_generic_web_deploy_recovery_classifies_provider_observations_without_writes(
        self,
    ) -> None:
        present = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="deployment-123",
            started_at="2026-08-15T12:00:00Z",
            finished_at="2026-08-15T12:05:00Z",
        )
        for observation, expected_action, expected_retry_safe in (
            (present, "adopt_observed", False),
            (
                GenericWebProviderDeploymentObservation(outcome="absent"),
                "retry_original_operation",
                True,
            ),
            (
                GenericWebProviderDeploymentObservation(outcome="unknown"),
                "hold_unknown",
                False,
            ),
        ):
            with (
                self.subTest(outcome=observation.outcome),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                reservation = _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key=f"provider-{observation.outcome}",
                )
                reservation = _write_generic_web_recovery_reservation(store, reservation)
                provider = _RecoveryObservationProvider(observation)
                app = _create_recovery_app(root=root, store=store)
                with (
                    patch(
                        "control_plane.generic_web_deploy_provider_adapter."
                        "default_generic_web_deploy_provider",
                        return_value=provider,
                    ),
                    patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore."
                        "write_deployment_record",
                        side_effect=AssertionError("deployment write"),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore."
                        "write_environment_inventory",
                        side_effect=AssertionError("inventory write"),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore."
                        "write_idempotency_record",
                        side_effect=AssertionError("idempotency write"),
                    ),
                ):
                    status_code, payload = _invoke_app(
                        app,
                        method="POST",
                        path="/v1/admin/generic-web/deploy-recovery/dry-run",
                        authorization="Bearer local-operator-token",
                        payload={
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "original_deploy": original_deploy,
                            "reason": "Classify provider observation without mutation.",
                        },
                        headers={"Idempotency-Key": reservation.idempotency_key},
                    )
                store.close()

            self.assertEqual(status_code, 200)
            self.assertEqual(payload["proposed_action"], expected_action)
            self.assertEqual(payload["retry_safe"], expected_retry_safe)
            self.assertEqual(provider.observation_calls, 1)

    def test_generic_web_deploy_recovery_holds_on_provider_read_uncertainty(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="provider-read-failure",
                ),
            )
            provider = _FailingRecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="unknown")
            )
            app = _create_recovery_app(root=root, store=store)
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                patch.object(
                    store, "write_deployment_record", side_effect=AssertionError("deployment write")
                ),
                patch.object(
                    store,
                    "write_environment_inventory",
                    side_effect=AssertionError("inventory write"),
                ),
                patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=AssertionError("idempotency write"),
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Hold when provider observation is uncertain.",
                    },
                    headers={"Idempotency-Key": reservation.idempotency_key},
                )
            store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["provider_outcome"], "unknown")
        self.assertEqual(payload["proposed_action"], "hold_unknown")
        self.assertEqual(provider.observation_calls, 1)

    def test_provider_evidence_distinguishes_bounded_observations_without_writes(
        self,
    ) -> None:
        present = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="deployment-secret",
            started_at="2026-08-15T12:00:00Z",
            finished_at="2026-08-15T12:05:00Z",
            error_message="provider-secret-message",
        )
        cases = (
            (present, "target_update", "deployment_present", "success"),
            (
                GenericWebProviderDeploymentObservation(outcome="absent"),
                "target_update",
                "deployment_absent_before_effect",
                "",
            ),
            (
                GenericWebProviderDeploymentObservation(outcome="absent"),
                "deploy_trigger",
                "deployment_absent_after_effect",
                "",
            ),
            (
                GenericWebProviderDeploymentObservation(
                    outcome="unknown", deployment_status="running"
                ),
                "deploy_trigger",
                "provider_status_unknown",
                "running",
            ),
        )
        expected_keys = {
            "schema_version",
            "status",
            "mode",
            "reservation_state",
            "reservation_attempt",
            "observed_at",
            "reconciliation_key_sha256",
            "provider_target_key_sha256",
            "provider_effect_phase",
            "provider_evidence",
            "provider_status",
            "provider_read_error_class",
        }
        for observation, effect_phase, expected_evidence, expected_status in cases:
            with (
                self.subTest(expected_evidence=expected_evidence),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                reservation = _write_generic_web_recovery_reservation(
                    store,
                    _generic_web_recovery_reservation(
                        original_deploy=original_deploy,
                        idempotency_key=f"provider-evidence-{expected_evidence}",
                        provider_effect_phase=effect_phase,
                    ),
                )
                provider = _RecoveryObservationProvider(observation)
                app = _create_recovery_app(root=root, store=store)
                with (
                    patch(
                        "control_plane.generic_web_deploy_provider_adapter."
                        "default_generic_web_deploy_provider",
                        return_value=provider,
                    ),
                    patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore."
                        "write_deployment_record",
                        side_effect=AssertionError("deployment write"),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore."
                        "write_environment_inventory",
                        side_effect=AssertionError("inventory write"),
                    ),
                    patch(
                        "control_plane.storage.postgres.PostgresRecordStore."
                        "write_idempotency_record",
                        side_effect=AssertionError("idempotency write"),
                    ),
                ):
                    status_code, payload = _invoke_provider_evidence(
                        app,
                        original_deploy=original_deploy,
                        idempotency_key=reservation.idempotency_key,
                        reason="Inspect exact provider evidence without mutation.",
                    )
                store.close()

            self.assertEqual(status_code, 200)
            self.assertEqual(set(payload), expected_keys)
            self.assertEqual(payload["provider_evidence"], expected_evidence)
            self.assertEqual(payload["provider_status"], expected_status)
            self.assertEqual(payload["provider_read_error_class"], "")
            self.assertNotIn("retry_safe", payload)
            self.assertNotIn("recovery_digest", payload)
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("deployment-secret", serialized)
            self.assertNotIn("provider-secret-message", serialized)
            self.assertNotIn(reservation.idempotency_key, serialized)
            self.assertNotIn(reservation.scope, serialized)
            self.assertEqual(provider.observation_calls, 1)

    def test_provider_evidence_reports_bounded_provider_read_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="provider-evidence-read-failure",
                    provider_effect_phase="deploy_trigger",
                ),
            )
            provider = _FailingRecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="unknown")
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                status_code, payload = _invoke_provider_evidence(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Classify provider read failure without exposing details.",
                )
            store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["provider_evidence"], "provider_read_failed")
        self.assertEqual(payload["provider_read_error_class"], "provider_request_failed")
        self.assertNotIn("provider read failed", json.dumps(payload, sort_keys=True))

    def test_legacy_correlation_is_bounded_read_only_and_adoption_only(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="legacy-correlation-bounded",
                    provider_effect_phase="deploy_trigger",
                    legacy_snapshot_without_product=True,
                ),
            )
            provider = _LegacyRecoveryObservationProvider(
                (_legacy_correlation(), _legacy_correlation())
            )
            app = _create_recovery_app(root=root, store=store)
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore.write_deployment_record",
                    side_effect=AssertionError("deployment write"),
                ),
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore."
                    "write_environment_inventory",
                    side_effect=AssertionError("inventory write"),
                ),
                patch(
                    "control_plane.storage.postgres.PostgresRecordStore.write_idempotency_record",
                    side_effect=AssertionError("idempotency write"),
                ),
            ):
                evidence_status, evidence_payload = _invoke_provider_evidence(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Inspect bounded legacy correlation evidence.",
                )
                dry_status, dry_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Inspect bounded legacy correlation evidence.",
                )
            store.close()

        self.assertEqual(evidence_status, 200)
        self.assertEqual(evidence_payload["provider_evidence"], "deployment_correlated_legacy")
        self.assertEqual(evidence_payload["provider_status"], "done")
        self.assertEqual(dry_status, 200)
        self.assertEqual(dry_payload["provider_outcome"], "present")
        self.assertEqual(dry_payload["proposed_action"], "adopt_observed")
        self.assertFalse(dry_payload["retry_safe"])
        self.assertEqual(provider.observation_calls, 2)
        self.assertEqual(provider.legacy_observation_calls, 2)
        serialized = json.dumps(
            {"evidence": evidence_payload, "dry_run": dry_payload},
            sort_keys=True,
        )
        self.assertNotIn("provider-legacy-deployment-secret", serialized)
        self.assertNotIn("2026-08-15T12:00:01", serialized)
        self.assertNotIn("legacy/github-actions/scope", serialized)

    def test_legacy_fallback_requires_legacy_snapshot_and_actionable_lease(self) -> None:
        for name, legacy_snapshot, state, lease_expires_at, expected_exact_calls in (
            ("modern snapshot", False, "reconcile_required", "", 1),
            ("active lease", True, "running", "2099-08-16T18:00:00Z", 0),
        ):
            with (
                self.subTest(name=name),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                reservation = _write_generic_web_recovery_reservation(
                    store,
                    _generic_web_recovery_reservation(
                        original_deploy=original_deploy,
                        idempotency_key=f"legacy-gate-{name}",
                        state=cast(Literal["running", "reconcile_required"], state),
                        lease_expires_at=lease_expires_at,
                        provider_effect_phase="deploy_trigger",
                        legacy_snapshot_without_product=legacy_snapshot,
                    ),
                )
                provider = _LegacyRecoveryObservationProvider((_legacy_correlation(),))
                app = _create_recovery_app(root=root, store=store)
                with patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ):
                    status_code, payload = _invoke_recovery(
                        app,
                        original_deploy=original_deploy,
                        idempotency_key=reservation.idempotency_key,
                        reason="Verify legacy fallback gating.",
                    )
                store.close()

            self.assertEqual(status_code, 200)
            self.assertEqual(
                payload["proposed_action"],
                "hold_unknown" if name == "modern snapshot" else "wait_for_active_lease",
            )
            self.assertEqual(provider.observation_calls, expected_exact_calls)
            self.assertEqual(provider.legacy_observation_calls, 0)

    def test_legacy_correlation_error_holds_unknown(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="legacy-correlation-ambiguous",
                    provider_effect_phase="deploy_trigger",
                    legacy_snapshot_without_product=True,
                ),
            )
            provider = _LegacyRecoveryObservationProvider(
                (
                    ValueError("raw provider ambiguity secret"),
                    ValueError("raw provider ambiguity secret"),
                )
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                evidence_status, evidence_payload = _invoke_provider_evidence(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Hold ambiguous legacy evidence.",
                )
                status_code, payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Hold ambiguous legacy evidence.",
                )
            store.close()

        self.assertEqual(evidence_status, 200)
        self.assertEqual(evidence_payload["provider_evidence"], "provider_status_unknown")
        self.assertEqual(status_code, 200)
        self.assertEqual(payload["provider_outcome"], "unknown")
        self.assertEqual(payload["proposed_action"], "hold_unknown")
        self.assertFalse(payload["retry_safe"])
        self.assertNotIn("raw provider ambiguity secret", json.dumps(payload, sort_keys=True))

    def test_optional_legacy_capability_does_not_change_nonlegacy_recovery_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="nonlegacy-digest-stability",
                ),
            )
            without_capability = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="absent")
            )
            with_capability = _LegacyRecoveryObservationProvider((_legacy_correlation(),))
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=without_capability,
            ):
                first_status, first_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Verify nonlegacy digest stability.",
                )
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=with_capability,
            ):
                second_status, second_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Verify nonlegacy digest stability.",
                )
            store.close()

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first_payload["recovery_digest"], second_payload["recovery_digest"])
        self.assertEqual(with_capability.legacy_observation_calls, 0)

    def test_legacy_apply_reinspection_stales_changed_correlation_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="legacy-stale-apply",
                    provider_effect_phase="deploy_trigger",
                    legacy_snapshot_without_product=True,
                ),
            )
            provider = _LegacyRecoveryObservationProvider(
                (
                    _legacy_correlation(),
                    _legacy_correlation(deployment_id_sha256="4" * 64),
                )
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                dry_status, dry_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Review legacy correlation evidence.",
                )
                apply_status, apply_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Review legacy correlation evidence.",
                    expected_recovery_digest=dry_payload["recovery_digest"],
                )
            stored = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(apply_status, 409)
        self.assertEqual(apply_payload["error"]["code"], "stale_recovery_digest")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "reconcile_required")

    def test_legacy_apply_adopts_without_retrying_provider_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="legacy-adoption-only",
                    provider_effect_phase="deploy_trigger",
                    legacy_snapshot_without_product=True,
                ),
            )
            provider = _LegacyRecoveryObservationProvider(
                (_legacy_correlation(), _legacy_correlation())
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                reason = "Adopt exact bounded legacy correlation evidence."
                dry_status, dry_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                )
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "GenericWebDeployProviderMutationAdapter.apply",
                    side_effect=AssertionError("provider retry"),
                ),
            ):
                apply_status, apply_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                    expected_recovery_digest=dry_payload["recovery_digest"],
                )
            stored = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(dry_payload["proposed_action"], "adopt_observed")
        self.assertFalse(dry_payload["retry_safe"])
        self.assertEqual(apply_status, 202)
        self.assertEqual(apply_payload["recovery_action"], "adopt_observed")
        self.assertFalse(apply_payload["retry_safe"])
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "completed")
        serialized = json.dumps(apply_payload, sort_keys=True)
        self.assertNotIn("provider-legacy-deployment-secret", serialized)
        self.assertNotIn("2026-08-15T12:00:02", serialized)

    def test_provider_evidence_supports_dedicated_and_execute_authorization(self) -> None:
        for actions in (
            ("generic_web_deploy_recovery_provider_evidence.read",),
            ("generic_web_deploy.execute",),
        ):
            with (
                self.subTest(actions=actions),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                reservation = _write_generic_web_recovery_reservation(
                    store,
                    _generic_web_recovery_reservation(
                        original_deploy=original_deploy,
                        idempotency_key=f"provider-evidence-authz-{actions[0]}",
                    ),
                )
                app = _create_recovery_app(root=root, store=store, actions=actions)
                with patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=_RecoveryObservationProvider(
                        GenericWebProviderDeploymentObservation(outcome="absent")
                    ),
                ):
                    status_code, _payload = _invoke_provider_evidence(
                        app,
                        original_deploy=original_deploy,
                        idempotency_key=reservation.idempotency_key,
                        reason="Verify provider evidence authorization compatibility.",
                    )
                store.close()

            self.assertEqual(status_code, 200)

    def test_provider_evidence_dedicated_authorization_supports_schema_v2(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="provider-evidence-authz-schema-v2",
                ),
            )
            app = _create_recovery_app(
                root=root,
                store=store,
                authz_policy=LaunchplaneAuthzPolicy(
                    schema_version=2,
                    local_operators=(
                        LocalOperatorPolicyRule(
                            subjects=("local-owner-agent",),
                            token_labels=("local-owner-write",),
                            products=("sellyouroutboard",),
                            contexts=("sellyouroutboard-testing",),
                            instances=("testing",),
                            actions=("generic_web_deploy_recovery_provider_evidence.read",),
                        ),
                    ),
                ),
            )
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=_RecoveryObservationProvider(
                    GenericWebProviderDeploymentObservation(outcome="absent")
                ),
            ):
                status_code, _payload = _invoke_provider_evidence(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Verify schema-v2 dedicated provider evidence authorization.",
                )
            store.close()

        self.assertEqual(status_code, 200)

    def test_provider_evidence_denies_before_reservation_lookup(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = _create_recovery_app(
                root=root,
                store=store,
                actions=("generic_web_preview.execute",),
            )
            with patch.object(
                store,
                "lookup_existing_mutation_reservation",
                side_effect=AssertionError("lookup disclosure"),
            ):
                status_code, payload = _invoke_provider_evidence(
                    app,
                    original_deploy=_generic_web_recovery_original_deploy(),
                    idempotency_key="provider-evidence-denied",
                    reason="Verify denial before reservation lookup.",
                )
            store.close()

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_deploy_recovery_digest_ignores_observed_at(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="stable-digest",
                ),
            )
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="absent")
            )
            app = _create_recovery_app(root=root, store=store)
            lookup_a = ExistingMutationReservationLookupResult(
                status="found",
                record=reservation,
                observed_at="2026-08-16T17:00:00Z",
            )
            lookup_b = ExistingMutationReservationLookupResult(
                status="found",
                record=reservation,
                observed_at="2026-08-16T17:05:00Z",
            )
            with (
                patch.object(
                    store,
                    "lookup_existing_mutation_reservation",
                    side_effect=(lookup_a, lookup_b),
                ),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
            ):
                first_status, first_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Check digest stability.",
                )
                second_status, second_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Check digest stability.",
                )
            store.close()

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertNotEqual(first_payload["observed_at"], second_payload["observed_at"])
        self.assertEqual(first_payload["recovery_digest"], second_payload["recovery_digest"])

    def test_generic_web_deploy_recovery_apply_rejects_stale_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="stale-digest",
                ),
            )
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="absent")
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                status_code, payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason="Reject stale recovery digest.",
                    expected_recovery_digest="0" * 64,
                )
            stored = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertEqual(status_code, 409)
        self.assertEqual(payload["error"]["code"], "stale_recovery_digest")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "reconcile_required")

    def test_generic_web_deploy_recovery_apply_adopts_observed_without_raw_leakage(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="adopt-observed",
                ),
            )
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="provider-deployment-123",
                    started_at="2026-08-15T12:00:00Z",
                    finished_at="2026-08-15T12:05:00Z",
                )
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                reason = "Review observed provider recovery."
                dry_status, dry_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                )
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                patch.object(
                    store,
                    "release_reserved_mutation",
                    side_effect=AssertionError("release"),
                ),
                patch.object(
                    store,
                    "supersede_expired_reconciled_mutation_and_reserve",
                    side_effect=AssertionError("supersession"),
                ),
            ):
                apply_status, apply_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                    expected_recovery_digest=dry_payload["recovery_digest"],
                )
            stored = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            deployments = store.list_deployment_records()
            store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(apply_status, 202)
        self.assertEqual(apply_payload["recovery_action"], "adopt_observed")
        self.assertEqual(apply_payload["recovery_digest"], dry_payload["recovery_digest"])
        self.assertEqual(provider.observation_calls, 2)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "completed")
        self.assertEqual(
            stored.response_payload["recovery"],
            {
                "schema_version": 1,
                "recovery_digest": dry_payload["recovery_digest"],
                "recovery_action": "adopt_observed",
            },
        )
        self.assertEqual(stored.response_payload["result"]["deploy_status"], "pass")
        self.assertTrue(stored.response_payload["records"]["deployment_record_id"])
        self.assertEqual(len(deployments), 1)
        serialized = json.dumps(apply_payload, sort_keys=True)
        self.assertNotIn(reservation.scope, serialized)
        self.assertNotIn(reservation.idempotency_key, serialized)
        self.assertNotIn("provider-deployment-123", serialized)

    def test_generic_web_deploy_recovery_apply_retries_and_replays_recovered_metadata(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="retry-original",
                ),
            )
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="absent")
            )
            app = _create_recovery_app(root=root, store=store)
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "default_generic_web_deploy_provider",
                return_value=provider,
            ):
                reason = "Review retry recovery."
                dry_status, dry_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                )
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "execute_generic_web_deploy_result",
                    return_value=(
                        {"deployment_record_id": "deployment-retry-recovery"},
                        {
                            "deployment_record_id": "deployment-retry-recovery",
                            "deploy_status": "pass",
                            "deploy_started_at": "2026-08-15T12:10:00Z",
                            "deploy_finished_at": "2026-08-15T12:15:00Z",
                            "product": "sellyouroutboard",
                            "context": "sellyouroutboard-testing",
                            "instance": "testing",
                            "post_deploy_status": "skipped",
                            "provider_effect_attempted": False,
                        },
                    ),
                ),
                patch.object(store, "reserve_mutation", side_effect=AssertionError("reserve")),
                patch.object(
                    store,
                    "release_reserved_mutation",
                    side_effect=AssertionError("release"),
                ),
            ):
                apply_status, apply_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                    expected_recovery_digest=dry_payload["recovery_digest"],
                )
                replay_status, replay_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                    expected_recovery_digest=dry_payload["recovery_digest"],
                )
            stored = store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            )
            store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(apply_status, 202)
        self.assertEqual(replay_status, 202)
        self.assertEqual(apply_payload["recovery_action"], "retry_original_operation")
        self.assertEqual(replay_payload["recovery_action"], "retry_original_operation")
        self.assertEqual(replay_payload["recovery_digest"], dry_payload["recovery_digest"])
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "completed")
        self.assertEqual(
            stored.response_payload["recovery"],
            {
                "schema_version": 1,
                "recovery_digest": dry_payload["recovery_digest"],
                "recovery_action": "retry_original_operation",
            },
        )
        self.assertEqual(
            stored.response_payload["result"]["deployment_record_id"],
            "deployment-retry-recovery",
        )

    def test_generic_web_deploy_recovery_apply_transitions_expired_running_before_retry(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = _write_generic_web_recovery_reservation(
                store,
                _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key="expired-running-retry",
                    state="running",
                ),
            )
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="absent")
            )
            app = _create_recovery_app(root=root, store=store)
            lookup = ExistingMutationReservationLookupResult(
                status="found",
                record=reservation,
                observed_at=reservation.lease_expires_at,
            )
            with (
                patch.object(store, "lookup_existing_mutation_reservation", return_value=lookup),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
            ):
                reason = "Review expired running retry recovery."
                dry_status, dry_payload = _invoke_recovery(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                )
            provider.observation = GenericWebProviderDeploymentObservation(outcome="absent")
            transitions: list[str] = []
            original_mark_reconcile = store.mark_mutation_reconcile_required
            original_retry_reconciled = store.retry_reconciled_mutation

            def mark_reconcile_side_effect(**call_kwargs: object) -> object:
                transitions.append("mark")
                return original_mark_reconcile(
                    reservation=cast(LaunchplaneIdempotencyRecord, call_kwargs["reservation"]),
                    reconciliation_key=cast(str, call_kwargs["reconciliation_key"]),
                )

            def retry_reconciled_side_effect(**call_kwargs: object) -> object:
                transitions.append("retry")
                candidate_reservation = cast(
                    LaunchplaneIdempotencyRecord, call_kwargs["reservation"]
                )
                lease_owner = cast(str, call_kwargs["lease_owner"])
                if "lease_seconds" not in call_kwargs:
                    return original_retry_reconciled(
                        reservation=candidate_reservation,
                        lease_owner=lease_owner,
                    )
                return original_retry_reconciled(
                    reservation=candidate_reservation,
                    lease_owner=lease_owner,
                    lease_seconds=cast(int, call_kwargs["lease_seconds"]),
                )

            with (
                patch.object(store, "lookup_existing_mutation_reservation", return_value=lookup),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch.object(
                    store,
                    "mark_mutation_reconcile_required",
                    side_effect=mark_reconcile_side_effect,
                ),
                patch.object(
                    store,
                    "retry_reconciled_mutation",
                    side_effect=retry_reconciled_side_effect,
                ),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "execute_generic_web_deploy_result",
                    return_value=(
                        {"deployment_record_id": "deployment-expired-running-recovery"},
                        {
                            "deployment_record_id": "deployment-expired-running-recovery",
                            "deploy_status": "pass",
                            "deploy_started_at": "2026-08-15T12:20:00Z",
                            "deploy_finished_at": "2026-08-15T12:25:00Z",
                            "product": "sellyouroutboard",
                            "context": "sellyouroutboard-testing",
                            "instance": "testing",
                            "post_deploy_status": "skipped",
                            "provider_effect_attempted": False,
                        },
                    ),
                ),
            ):
                apply_status, apply_payload = _invoke_recovery_apply(
                    app,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    reason=reason,
                    expected_recovery_digest=dry_payload["recovery_digest"],
                )
            store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(apply_status, 202)
        self.assertEqual(apply_payload["recovery_action"], "retry_original_operation")
        self.assertEqual(transitions[:2], ["mark", "retry"])

    def test_generic_web_deploy_recovery_rejects_stored_target_identity_before_provider_read(
        self,
    ) -> None:
        for identity_kind in (
            "provider_target",
            "malformed_reconciliation",
            "instance_mismatch",
            "product_mismatch",
            "empty_product",
        ):
            with (
                self.subTest(identity_kind=identity_kind),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                reservation = _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key=f"identity-{identity_kind}",
                    provider_target_key=(
                        "generic-web-provider-target:" + "0" * 64
                        if identity_kind == "provider_target"
                        else None
                    ),
                )
                if identity_kind == "malformed_reconciliation":
                    reservation = reservation.model_copy(
                        update={"reconciliation_key": "malformed-reconciliation-key"}
                    )
                elif identity_kind == "instance_mismatch":
                    mismatched_target = _generic_web_recovery_target()
                    mismatched_target.ship_request.instance = "prod"
                    reservation = reservation.model_copy(
                        update={
                            "reconciliation_key": build_generic_web_provider_reconciliation_key(
                                mismatched_target,
                                product="sellyouroutboard",
                            ),
                            "provider_target_key": build_generic_web_provider_target_key(
                                mismatched_target
                            ),
                        }
                    )
                elif identity_kind == "product_mismatch":
                    reservation = reservation.model_copy(
                        update={
                            "reconciliation_key": build_generic_web_provider_reconciliation_key(
                                _generic_web_recovery_target(),
                                product="different-product",
                            )
                        }
                    )
                elif identity_kind == "empty_product":
                    reservation = reservation.model_copy(
                        update={
                            "reconciliation_key": build_generic_web_provider_reconciliation_key(
                                _generic_web_recovery_target()
                            )
                        }
                    )
                reservation = _write_generic_web_recovery_reservation(store, reservation)
                provider = _RecoveryObservationProvider(
                    GenericWebProviderDeploymentObservation(outcome="unknown")
                )
                app = _create_recovery_app(root=root, store=store)
                with patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ):
                    status_code, payload = _invoke_app(
                        app,
                        method="POST",
                        path="/v1/admin/generic-web/deploy-recovery/dry-run",
                        authorization="Bearer local-operator-token",
                        payload={
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "original_deploy": original_deploy,
                            "reason": "Reject invalid stored reconciliation identity.",
                        },
                        headers={"Idempotency-Key": reservation.idempotency_key},
                    )
                store.close()

            self.assertEqual(status_code, 409)
            self.assertEqual(payload["error"]["code"], "reservation_target_conflict")
            self.assertEqual(provider.observation_calls, 0)

    def test_generic_web_deploy_recovery_accepts_origin_main_legacy_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            target = _generic_web_recovery_target()
            reservation = _generic_web_recovery_reservation(
                original_deploy=original_deploy,
                idempotency_key="origin-main-legacy-snapshot",
            ).model_copy(
                update={
                    "reconciliation_key": _legacy_generic_web_reconciliation_key(target),
                    "provider_target_key": build_generic_web_provider_target_key(target),
                }
            )
            reservation = _write_generic_web_recovery_reservation(store, reservation)
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="absent")
            )
            app = _create_recovery_app(root=root, store=store)
            with (
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
                patch.object(store, "reserve_mutation", side_effect=AssertionError("mutation")),
                patch.object(
                    store,
                    "write_deployment_record",
                    side_effect=AssertionError("deployment write"),
                ),
                patch.object(
                    store,
                    "write_environment_inventory",
                    side_effect=AssertionError("inventory write"),
                ),
                patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=AssertionError("idempotency write"),
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Classify the origin/main legacy reservation safely.",
                    },
                    headers={"Idempotency-Key": reservation.idempotency_key},
                )
            store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["provider_outcome"], "absent")
        self.assertTrue(payload["retry_safe"])
        self.assertEqual(payload["proposed_action"], "retry_original_operation")
        self.assertEqual(provider.observation_calls, 1)

    def test_generic_web_deploy_recovery_reauthorizes_and_reports_stored_context(self) -> None:
        for allowed_contexts, expected_status in (
            (("sellyouroutboard-current",), 403),
            (("sellyouroutboard-current", "sellyouroutboard-testing"), 200),
        ):
            with (
                self.subTest(expected_status=expected_status),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                profile_payload = _product_profile_payload()
                lanes = cast(tuple[dict[str, object], ...], profile_payload["lanes"])
                lanes[0]["context"] = "sellyouroutboard-current"
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
                original_deploy = _generic_web_recovery_original_deploy()
                reservation = _generic_web_recovery_reservation(
                    original_deploy=original_deploy,
                    idempotency_key=f"context-drift-{expected_status}",
                )
                reservation = _write_generic_web_recovery_reservation(store, reservation)
                provider = _RecoveryObservationProvider(
                    GenericWebProviderDeploymentObservation(outcome="absent")
                )
                app = _create_recovery_app(
                    root=root,
                    store=store,
                    contexts=allowed_contexts,
                )
                with patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ):
                    status_code, payload = _invoke_app(
                        app,
                        method="POST",
                        path="/v1/admin/generic-web/deploy-recovery/dry-run",
                        authorization="Bearer local-operator-token",
                        payload={
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "original_deploy": original_deploy,
                            "reason": "Verify stored context authority after profile drift.",
                        },
                        headers={"Idempotency-Key": reservation.idempotency_key},
                    )
                store.close()

            self.assertEqual(status_code, expected_status)
            if expected_status == 403:
                self.assertEqual(payload["error"]["code"], "authorization_denied")
                self.assertEqual(provider.observation_calls, 0)
            else:
                self.assertEqual(payload["context"], "sellyouroutboard-testing")
                self.assertEqual(payload["proposed_action"], "retry_original_operation")
                self.assertEqual(provider.observation_calls, 1)

    def test_generic_web_deploy_recovery_rejects_malformed_running_lease_as_hold_unknown(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            valid = _generic_web_recovery_reservation(
                original_deploy=original_deploy,
                idempotency_key="malformed-lease",
                state="running",
            )
            malformed = valid.model_copy(update={"lease_expires_at": "not-a-timestamp"})
            provider = _RecoveryObservationProvider(
                GenericWebProviderDeploymentObservation(outcome="unknown")
            )
            app = _create_recovery_app(root=root, store=store)
            lookup = ExistingMutationReservationLookupResult(
                status="found",
                record=malformed,
                observed_at="2026-08-16T17:00:00Z",
            )
            with (
                patch.object(store, "lookup_existing_mutation_reservation", return_value=lookup),
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "default_generic_web_deploy_provider",
                    return_value=provider,
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Reject malformed lease timing.",
                    },
                    headers={"Idempotency-Key": valid.idempotency_key},
                )
            store.close()

        self.assertEqual(status_code, 409)
        self.assertEqual(payload["error"]["code"], "hold_unknown")
        self.assertEqual(provider.observation_calls, 0)

    def test_generic_web_deploy_recovery_completed_context_must_be_exact(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = LaunchplaneIdempotencyRecord(
                record_id="idempotency-completed-inexact",
                scope="legacy/github-actions/scope",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key="completed-inexact",
                request_fingerprint=idempotency_request_fingerprint(
                    route_path="/v1/drivers/generic-web/deploy",
                    payload=original_deploy,
                ),
                response_status_code=202,
                response_trace_id="trace-completed-inexact",
                recorded_at="2026-08-15T12:00:00Z",
                response_payload={"result": {"deploy_status": "pass"}},
            )
            store.write_idempotency_record(reservation)
            app = _create_recovery_app(root=root, store=store)
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/dry-run",
                authorization="Bearer local-operator-token",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "original_deploy": original_deploy,
                    "reason": "Require exact completed operation context.",
                },
                headers={"Idempotency-Key": reservation.idempotency_key},
            )
            store.close()

        self.assertEqual(status_code, 409)
        self.assertEqual(payload["error"]["code"], "reservation_target_conflict")

    def test_generic_web_deploy_recovery_completed_identity_must_be_nonblank(self) -> None:
        for field_name in ("product", "context", "instance"):
            with (
                self.subTest(field_name=field_name),
                TemporaryDirectory() as temporary_directory_name,
            ):
                root = Path(temporary_directory_name)
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(root / "launchplane.sqlite3")
                )
                store.ensure_schema()
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
                )
                original_deploy = _generic_web_recovery_original_deploy()
                completed_result = _generic_web_deploy_result().model_dump(mode="json")
                completed_result[field_name] = "   "
                reservation = LaunchplaneIdempotencyRecord(
                    record_id=f"idempotency-completed-blank-{field_name}",
                    scope="legacy/github-actions/scope",
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key=f"completed-blank-{field_name}",
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path="/v1/drivers/generic-web/deploy",
                        payload=original_deploy,
                    ),
                    response_status_code=202,
                    response_trace_id=f"trace-completed-blank-{field_name}",
                    recorded_at="2026-08-15T12:00:00Z",
                    response_payload={"result": completed_result},
                )
                store.write_idempotency_record(reservation)
                app = _create_recovery_app(root=root, store=store)
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/admin/generic-web/deploy-recovery/dry-run",
                    authorization="Bearer local-operator-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "original_deploy": original_deploy,
                        "reason": "Reject a blank completed operation identity.",
                    },
                    headers={"Idempotency-Key": reservation.idempotency_key},
                )
                store.close()

            self.assertEqual(status_code, 409)
            self.assertEqual(payload["error"]["code"], "reservation_target_conflict")

    def test_generic_web_deploy_recovery_completed_response_reports_stored_context_after_drift(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            profile_payload = _product_profile_payload()
            lanes = cast(tuple[dict[str, object], ...], profile_payload["lanes"])
            lanes[0]["context"] = "sellyouroutboard-current"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            original_deploy = _generic_web_recovery_original_deploy()
            reservation = LaunchplaneIdempotencyRecord(
                record_id="idempotency-completed-context-drift",
                scope="legacy/github-actions/scope",
                route_path="/v1/drivers/generic-web/deploy",
                idempotency_key="completed-context-drift",
                request_fingerprint=idempotency_request_fingerprint(
                    route_path="/v1/drivers/generic-web/deploy",
                    payload=original_deploy,
                ),
                response_status_code=202,
                response_trace_id="trace-completed-context-drift",
                recorded_at="2026-08-15T12:00:00Z",
                response_payload={
                    "result": _generic_web_deploy_result(
                        deployment_record_id="deployment-completed-context-drift"
                    ).model_dump(mode="json")
                },
            )
            store.write_idempotency_record(reservation)
            app = _create_recovery_app(
                root=root,
                store=store,
                contexts=("sellyouroutboard-current", "sellyouroutboard-testing"),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/admin/generic-web/deploy-recovery/dry-run",
                authorization="Bearer local-operator-token",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "original_deploy": original_deploy,
                    "reason": "Report authoritative completed operation context.",
                },
                headers={"Idempotency-Key": reservation.idempotency_key},
            )
            store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["proposed_action"], "replay_completed")
