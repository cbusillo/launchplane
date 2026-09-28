"""Close-out recovery for a deploy that took effect without a provider record.

A current-format reservation past ``deploy_trigger`` whose exact provider
deployment is absent may be closed out only when the target is configured for
the original image and that exact image is what runs. These tests prove the
match, each mismatch, and missing evidence through the real HTTP routes and
storage, plus the pure evaluation and the Dokploy read.
"""

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from click import ClickException

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import runtime_evidence as dokploy_runtime_evidence
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
    GenericWebRuntimeArtifactObservation,
    evaluate_generic_web_runtime_close_out,
)
from tests.support.profiles import product_profile_payload as _product_profile_payload
from tests.support.stores import sqlite_database_url as _sqlite_database_url
from tests.test_generic_web_deploy_recovery import (
    _create_recovery_app,
    _generic_web_recovery_reservation,
    _generic_web_recovery_target,
    _invoke_recovery,
    _invoke_recovery_apply,
    _RecoveryObservationProvider,
    _write_generic_web_recovery_reservation,
)


_ORIGINAL_IMAGE = "ghcr.io/cbusillo/sellyouroutboard@sha256:" + "a" * 64
_OTHER_DIGEST_IMAGE = "ghcr.io/cbusillo/sellyouroutboard@sha256:" + "b" * 64
_DATABASE_IMAGE = "mariadb:13.0"
_PROVIDER_FACTORY = (
    "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider"
)


def _original_deploy(artifact_id: str = _ORIGINAL_IMAGE) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "deploy": {
            "schema_version": 1,
            "product": "sellyouroutboard",
            "instance": "testing",
            "artifact_id": artifact_id,
            "source_git_ref": "abc123",
        },
    }


def _runtime(
    *,
    target: str = _ORIGINAL_IMAGE,
    running: tuple[str, ...] = (_ORIGINAL_IMAGE, _DATABASE_IMAGE),
) -> GenericWebRuntimeArtifactObservation:
    return GenericWebRuntimeArtifactObservation(
        target_artifact_reference=target,
        running_container_images=running,
    )


class _RuntimeCloseOutProvider(_RecoveryObservationProvider):
    """Exact titled deployment is absent; runtime reads return queued results."""

    def __init__(
        self,
        runtime_observations: tuple[GenericWebRuntimeArtifactObservation | BaseException, ...],
    ) -> None:
        super().__init__(GenericWebProviderDeploymentObservation(outcome="absent"))
        self.runtime_observations = list(runtime_observations)
        self.runtime_calls = 0

    def observe_runtime_artifact(self, **_kwargs: object) -> GenericWebRuntimeArtifactObservation:
        self.runtime_calls += 1
        if not self.runtime_observations:
            raise AssertionError("unexpected runtime observation")
        observation = self.runtime_observations.pop(0)
        if isinstance(observation, BaseException):
            raise observation
        return observation


class _RecoveryHarness:
    def __init__(self, root: Path) -> None:
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(root / "launchplane.sqlite3")
        )
        self.store.ensure_schema()
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
        )
        self.app = _create_recovery_app(root=root, store=self.store)

    def reserve(
        self,
        *,
        idempotency_key: str,
        original_deploy: dict[str, object],
        state: str = "reconcile_required",
        provider_effect_phase: str = "deploy_trigger",
        legacy_snapshot_without_product: bool = False,
    ) -> Any:
        return _write_generic_web_recovery_reservation(
            self.store,
            _generic_web_recovery_reservation(
                original_deploy=original_deploy,
                idempotency_key=idempotency_key,
                state=state,  # type: ignore[arg-type]
                provider_effect_phase=provider_effect_phase,
                legacy_snapshot_without_product=legacy_snapshot_without_product,
            ),
        )

    def dry_run(
        self, provider: object, *, original_deploy: dict[str, object], idempotency_key: str
    ) -> tuple[int, dict[str, Any]]:
        with patch(_PROVIDER_FACTORY, return_value=provider):
            return _invoke_recovery(
                self.app,
                original_deploy=original_deploy,
                idempotency_key=idempotency_key,
                reason="Close out a deploy that already took effect.",
            )

    def apply(
        self,
        provider: object,
        *,
        original_deploy: dict[str, object],
        idempotency_key: str,
        digest: str,
    ) -> tuple[int, dict[str, Any]]:
        with ExitStack() as stack:
            stack.enter_context(patch(_PROVIDER_FACTORY, return_value=provider))
            stack.enter_context(
                patch(
                    "control_plane.generic_web_deploy_provider_adapter."
                    "GenericWebDeployProviderMutationAdapter.apply",
                    side_effect=AssertionError("close-out must never retry the deploy"),
                )
            )
            stack.enter_context(
                patch.object(
                    self.store,
                    "retry_reconciled_mutation",
                    side_effect=AssertionError("close-out must never retry"),
                )
            )
            return _invoke_recovery_apply(
                self.app,
                original_deploy=original_deploy,
                idempotency_key=idempotency_key,
                reason="Close out a deploy that already took effect.",
                expected_recovery_digest=digest,
            )

    def stored(self, reservation: Any) -> Any:
        return self.store.read_idempotency_record(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
        )


class GenericWebDeployRecoveryCloseOutTests(unittest.TestCase):
    def test_matching_runtime_closes_out_once_and_releases_the_target(self) -> None:
        original_deploy = _original_deploy()
        with TemporaryDirectory() as temporary_directory_name:
            harness = _RecoveryHarness(Path(temporary_directory_name))
            reservation = harness.reserve(
                idempotency_key="close-out-match", original_deploy=original_deploy
            )
            provider = _RuntimeCloseOutProvider((_runtime(), _runtime(), _runtime()))
            dry_status, dry_payload = harness.dry_run(
                provider,
                original_deploy=original_deploy,
                idempotency_key=reservation.idempotency_key,
            )
            apply_status, apply_payload = harness.apply(
                provider,
                original_deploy=original_deploy,
                idempotency_key=reservation.idempotency_key,
                digest=dry_payload["recovery_digest"],
            )
            replay_status, replay_payload = harness.apply(
                provider,
                original_deploy=original_deploy,
                idempotency_key=reservation.idempotency_key,
                digest=dry_payload["recovery_digest"],
            )
            stored = harness.stored(reservation)
            deployments = harness.store.list_deployment_records()
            next_deploy = harness.store.reserve_mutation(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key="next-deploy-after-close-out",
                request_fingerprint="next-deploy-fingerprint",
                lease_owner="next-deploy",
                reconciliation_key=reservation.reconciliation_key,
                provider_target_key=reservation.provider_target_key,
            )
            harness.store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(dry_payload["proposed_action"], "close_out_observed")
        self.assertFalse(dry_payload["retry_safe"])
        self.assertEqual(dry_payload["provider_outcome"], "unknown")
        self.assertEqual(apply_status, 202, apply_payload)
        self.assertEqual(apply_payload["recovery_action"], "close_out_observed")
        self.assertEqual(apply_payload["recovery_digest"], dry_payload["recovery_digest"])
        self.assertEqual(apply_payload["reservation_state"], "completed")
        self.assertFalse(apply_payload["retry_safe"])
        self.assertEqual(apply_payload["provider_outcome"], "unknown")
        self.assertEqual(apply_payload["provider_status"], "")
        self.assertEqual(replay_status, 202, replay_payload)
        self.assertEqual(replay_payload["recovery_action"], "close_out_observed")
        self.assertEqual(stored.state, "completed")
        self.assertEqual(stored.response_status_code, 202)
        self.assertEqual(
            stored.response_payload["recovery"],
            {
                "schema_version": 1,
                "recovery_digest": dry_payload["recovery_digest"],
                "recovery_action": "close_out_observed",
            },
        )
        result = stored.response_payload["result"]
        self.assertEqual(result["deploy_status"], "pass")
        self.assertEqual(result["deployment_record_id"], "")
        self.assertEqual(result["deploy_started_at"], stored.provider_effect_started_at)
        self.assertEqual(stored.response_payload["records"], {})
        self.assertEqual(deployments, ())
        self.assertEqual(next_deploy.status, "acquired")
        self.assertEqual(provider.runtime_calls, 2)
        serialized = json.dumps(apply_payload, sort_keys=True)
        self.assertNotIn(reservation.scope, serialized)
        self.assertNotIn(reservation.idempotency_key, serialized)

    def test_each_runtime_mismatch_holds_and_apply_refuses(self) -> None:
        cases = {
            "target configured for another digest": _runtime(target=_OTHER_DIGEST_IMAGE),
            "target configured by tag": _runtime(target="ghcr.io/cbusillo/sellyouroutboard:v1"),
            "running container on another digest": _runtime(
                running=(_OTHER_DIGEST_IMAGE, _DATABASE_IMAGE)
            ),
            "original and another digest both running": _runtime(
                running=(_ORIGINAL_IMAGE, _OTHER_DIGEST_IMAGE)
            ),
            "same repository running by tag": _runtime(
                running=(_ORIGINAL_IMAGE, "ghcr.io/cbusillo/sellyouroutboard:latest")
            ),
            "only unrelated containers running": _runtime(running=(_DATABASE_IMAGE,)),
            "nothing running": _runtime(running=()),
        }
        for name, observation in cases.items():
            with self.subTest(name), TemporaryDirectory() as temporary_directory_name:
                original_deploy = _original_deploy()
                harness = _RecoveryHarness(Path(temporary_directory_name))
                reservation = harness.reserve(
                    idempotency_key="close-out-mismatch", original_deploy=original_deploy
                )
                provider = _RuntimeCloseOutProvider((observation, observation))
                dry_status, dry_payload = harness.dry_run(
                    provider,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                )
                apply_status, apply_payload = harness.apply(
                    provider,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    digest=dry_payload["recovery_digest"],
                )
                stored = harness.stored(reservation)
                harness.store.close()

                self.assertEqual(dry_status, 200)
                self.assertEqual(dry_payload["proposed_action"], "hold_unknown")
                self.assertEqual(apply_status, 409)
                self.assertEqual(apply_payload["error"]["code"], "recovery_not_actionable")
                self.assertEqual(stored.state, "reconcile_required")

    def test_missing_or_ineligible_evidence_holds_without_close_out(self) -> None:
        class _NoRuntimeCapability(_RecoveryObservationProvider):
            def __init__(self) -> None:
                super().__init__(GenericWebProviderDeploymentObservation(outcome="absent"))

        cases: dict[str, dict[str, Any]] = {
            "runtime read fails": {
                "provider": _RuntimeCloseOutProvider((ClickException("read failed"),)),
                "runtime_calls": 1,
            },
            "provider has no runtime capability": {"provider": _NoRuntimeCapability()},
            "original artifact is not immutable": {
                "provider": _RuntimeCloseOutProvider(
                    (_runtime(target="ghcr.io/cbusillo/sellyouroutboard:v1"),)
                ),
                "original_deploy": _original_deploy("ghcr.io/cbusillo/sellyouroutboard:v1"),
                "runtime_calls": 1,
            },
            "effect never reached deploy trigger": {
                "provider": _RuntimeCloseOutProvider(()),
                "provider_effect_phase": "target_update",
                "expected_action": "retry_original_operation",
            },
            "legacy reservation snapshot": {
                "provider": _RuntimeCloseOutProvider(()),
                "legacy_snapshot_without_product": True,
            },
            "reservation still running with a live lease": {
                "provider": _RuntimeCloseOutProvider(()),
                "state": "running",
                "expected_action": "wait_for_active_lease",
            },
        }
        for name, case in cases.items():
            with self.subTest(name), TemporaryDirectory() as temporary_directory_name:
                original_deploy = case.get("original_deploy", _original_deploy())
                harness = _RecoveryHarness(Path(temporary_directory_name))
                reservation = harness.reserve(
                    idempotency_key="close-out-missing",
                    original_deploy=original_deploy,
                    state=case.get("state", "reconcile_required"),
                    provider_effect_phase=case.get("provider_effect_phase", "deploy_trigger"),
                    legacy_snapshot_without_product=case.get(
                        "legacy_snapshot_without_product", False
                    ),
                )
                provider = case["provider"]
                dry_status, dry_payload = harness.dry_run(
                    provider,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                )
                stored = harness.stored(reservation)
                harness.store.close()

                self.assertEqual(dry_status, 200, dry_payload)
                self.assertEqual(
                    dry_payload["proposed_action"], case.get("expected_action", "hold_unknown")
                )
                self.assertEqual(stored.state, reservation.state)
                self.assertEqual(
                    getattr(provider, "runtime_calls", 0), case.get("runtime_calls", 0)
                )

    def test_post_deploy_driver_is_never_closed_out(self) -> None:
        original_deploy = _original_deploy()
        with TemporaryDirectory() as temporary_directory_name:
            harness = _RecoveryHarness(Path(temporary_directory_name))
            reservation = harness.reserve(
                idempotency_key="close-out-post-deploy", original_deploy=original_deploy
            )
            provider = _RuntimeCloseOutProvider(())
            with patch(
                "control_plane.generic_web_deploy_provider_adapter."
                "generic_web_post_deploy_executor_for_driver_id",
                return_value=object(),
            ):
                dry_status, dry_payload = harness.dry_run(
                    provider,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                )
            harness.store.close()

        self.assertEqual(dry_status, 200)
        self.assertEqual(dry_payload["proposed_action"], "hold_unknown")
        self.assertEqual(provider.runtime_calls, 0)

    def test_apply_reinspects_runtime_and_rejects_changed_evidence(self) -> None:
        changes: dict[str, GenericWebRuntimeArtifactObservation | BaseException] = {
            "runtime no longer matches": _runtime(running=(_OTHER_DIGEST_IMAGE,)),
            "matching container count changed": _runtime(
                running=(_ORIGINAL_IMAGE, _ORIGINAL_IMAGE, _DATABASE_IMAGE)
            ),
            "runtime read fails at apply": ClickException("read failed"),
        }
        for name, apply_observation in changes.items():
            with self.subTest(name), TemporaryDirectory() as temporary_directory_name:
                original_deploy = _original_deploy()
                harness = _RecoveryHarness(Path(temporary_directory_name))
                reservation = harness.reserve(
                    idempotency_key="close-out-stale", original_deploy=original_deploy
                )
                provider = _RuntimeCloseOutProvider((_runtime(), apply_observation))
                dry_status, dry_payload = harness.dry_run(
                    provider,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                )
                apply_status, apply_payload = harness.apply(
                    provider,
                    original_deploy=original_deploy,
                    idempotency_key=reservation.idempotency_key,
                    digest=dry_payload["recovery_digest"],
                )
                stored = harness.stored(reservation)
                harness.store.close()

                self.assertEqual(dry_status, 200)
                self.assertEqual(dry_payload["proposed_action"], "close_out_observed")
                self.assertEqual(apply_status, 409)
                self.assertEqual(apply_payload["error"]["code"], "stale_recovery_digest")
                self.assertEqual(stored.state, "reconcile_required")


class RuntimeCloseOutEvaluationTests(unittest.TestCase):
    def test_counts_only_the_original_repository(self) -> None:
        evidence = evaluate_generic_web_runtime_close_out(
            observation=_runtime(running=(_ORIGINAL_IMAGE, _DATABASE_IMAGE, "redis:7")),
            expected_artifact_reference=_ORIGINAL_IMAGE,
        )

        self.assertEqual(evidence.running_container_count, 3)
        self.assertEqual(evidence.matching_container_count, 1)
        self.assertNotIn(_ORIGINAL_IMAGE, evidence.model_dump_json())

    def test_tag_and_digest_reference_of_the_same_repository_is_a_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_generic_web_runtime_close_out(
                observation=_runtime(
                    running=(
                        _ORIGINAL_IMAGE,
                        "ghcr.io/cbusillo/sellyouroutboard:old@sha256:" + "b" * 64,
                    ),
                ),
                expected_artifact_reference=_ORIGINAL_IMAGE,
            )

    def test_registry_port_is_not_mistaken_for_a_tag(self) -> None:
        original = "registry.example:5000/team/app@sha256:" + "c" * 64
        with self.assertRaises(ValueError):
            evaluate_generic_web_runtime_close_out(
                observation=_runtime(
                    target=original,
                    running=(original, "registry.example:5000/team/app:latest"),
                ),
                expected_artifact_reference=original,
            )


class DokployRuntimeArtifactReadTests(unittest.TestCase):
    def test_reads_running_images_of_the_exact_compose_app_only(self) -> None:
        project = "com.docker.compose.project"
        containers = [
            {"containerId": "c1", "name": "app-x1-sync-1", "state": "running"},
            {"containerId": "c2", "name": "app-x1-db-1", "state": "running"},
            {"containerId": "c3", "name": "app-x1-old-1", "state": "exited"},
            {"containerId": "c4", "name": "app-x10-sync-1", "state": "running"},
            {"containerId": "c5", "name": "frontend", "state": "running"},
            {"containerId": "c6", "name": "app-x1-worker-1", "state": "running"},
        ]
        inspected = {
            "c1": ({"Image": _ORIGINAL_IMAGE}, {"Running": True}),
            "c2": ({"Image": _DATABASE_IMAGE, "Labels": {project: "app-x1"}}, None),
            "c3": ({"Image": _OTHER_DIGEST_IMAGE}, {"Running": False}),
            "c4": ({"Image": _OTHER_DIGEST_IMAGE, "Labels": {project: "app-x10"}}, None),
            "c5": ({"Image": _OTHER_DIGEST_IMAGE, "Labels": {project: "app-x1"}}, None),
            "c6": ({"Image": _OTHER_DIGEST_IMAGE}, {"Running": False}),
        }

        def fake_request(*, path: str, query: dict[str, object], **_kwargs: object) -> object:
            if path == "/api/docker.getContainersByAppNameMatch":
                return containers
            container_id = query["containerId"]
            assert isinstance(container_id, str)
            config, state = inspected[container_id]
            return {"Config": config, **({"State": state} if state is not None else {})}

        with patch("control_plane.dokploy.api.dokploy_request", fake_request):
            running = dokploy_runtime_evidence.fetch_compose_running_container_images(
                host="https://dokploy.example",
                token="token",
                app_name="app-x1",
                server_id="server-1",
            )

        # c5 has a custom name but belongs to the app by its inspected project
        # label; c4 belongs to another app; c3 and c6 are not running when
        # inspected, even though c6 was listed as running.
        self.assertEqual(running, (_ORIGINAL_IMAGE, _DATABASE_IMAGE, _OTHER_DIGEST_IMAGE))

    def test_running_container_without_image_fails_closed(self) -> None:
        def fake_request(*, path: str, **_kwargs: object) -> object:
            if path == "/api/docker.getContainersByAppNameMatch":
                return [{"containerId": "c1", "name": "app-x1-sync-1", "state": "running"}]
            return {"Config": {}}

        with (
            patch("control_plane.dokploy.api.dokploy_request", fake_request),
            self.assertRaises(dokploy_runtime_evidence.DokployEvidenceProviderError),
        ):
            dokploy_runtime_evidence.fetch_compose_running_container_images(
                host="https://dokploy.example", token="token", app_name="app-x1"
            )

    def test_provider_reads_compose_targets_only(self) -> None:
        with self.assertRaises(ValueError):
            DokployGenericWebDeployProvider().observe_runtime_artifact(
                control_plane_root=Path("."),
                resolved_deploy_target=_generic_web_recovery_target(),
            )


if __name__ == "__main__":
    unittest.main()
