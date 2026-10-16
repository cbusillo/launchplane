"""Exact-artifact, failure, restart and both-target generic-web cutover behavior."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.generic_web_cutover import (
    GenericWebCutoverLane,
    GenericWebCutoverPlan,
    WebSlotCoordinates,
    WebSlot,
    WebSlotReadiness,
    WebRouteObservation,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    ProductImageProfile,
)
from control_plane.contracts.promotion_record import (
    DeploymentEvidence,
    HealthcheckEvidence,
    ArtifactIdentityReference,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.generic_web_cutover import WebDeploymentCutoverStore
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_cutover import execute_web_cutover, rollback_web_cutover
from control_plane.workflows.generic_web_deploy import (
    execute_generic_web_deploy,
    GenericWebDeployRequest,
)
from control_plane.workflows.generic_web_deploy_provider import DokployGenericWebDeployProvider
from control_plane.workflows.inventory import build_environment_inventory
from tests.test_release_compatibility import artifact, file


def plan(target: str = "compose", incompatible: bool = False) -> GenericWebCutoverPlan:
    before = artifact(
        "old", files=(file("static/app.css", "static", module=""),), modules=(), install_modules=()
    )
    after = artifact(
        "new",
        files=(file("static/app.css", "static", module="", digest="b"),),
        modules=(),
        install_modules=(),
    )
    after.image.digest = "sha256:" + "b" * 64
    if incompatible:
        after.release_compatibility = None
    config = GenericWebCutoverLane(
        route_id="fixture",
        host="fixture.invalid",
        writers=("queue",),
        runtime_settings_sha256="c" * 64,
        slots=cast(
            tuple[WebSlotCoordinates, WebSlotCoordinates],
            tuple(
                WebSlotCoordinates(
                    slot_id=name,
                    target_id=name + "-target",
                    target_type=cast(Literal["application", "compose"], target),
                    private_url="http://" + name + ":8080",
                )
                for name in ("old", "new")
            ),
        ),
    )
    slots = []
    for coordinate, manifest in zip(config.slots, (before, after), strict=True):
        image = f"{manifest.image.repository}@{manifest.image.digest}"
        slots.append(
            WebSlot(
                **coordinate.model_dump(),
                image=image,
                deploy_reference=manifest.image.repository + ":sha-" + manifest.source_commit,
                runtime_identity=RuntimeIdentity(
                    product="fixture",
                    context="fixture",
                    instance="prod",
                    artifact_id=manifest.artifact_id,
                    source_git_ref=manifest.source_commit,
                    image_reference=image,
                    image_digest=manifest.image.digest,
                    slot=coordinate.slot_id,
                    deployment_record_id=coordinate.slot_id,
                    release_id="release",
                ),
            )
        )
    return GenericWebCutoverPlan(
        release_id="release",
        lane=config,
        production=before,
        candidate=after,
        compatibility=classify_release_database_compatibility(production=before, candidate=after),
        old=slots[0],
        new=slots[1],
    )


class Provider:
    def __init__(self, intent: GenericWebCutoverPlan):
        self.plan, self.slot = intent, intent.old
        self.actions: list[str] = []
        self.fail_ready = False
        self.switch_uncertain = False
        self.wrong_public = False
        self.drained = True
        self.stale = False

    def exclusive(self, _plan: Any) -> Any:
        return nullcontext()

    def preflight(self, _plan: Any) -> None:
        pass

    def prepare_candidate(self, _plan: Any) -> None:
        self.actions.append("prepare")

    def readiness(self, intent: GenericWebCutoverPlan, slot: WebSlot) -> WebSlotReadiness:
        if self.fail_ready:
            raise ValueError("failed private readiness")
        return WebSlotReadiness(
            slot_id=slot.slot_id,
            image=slot.image,
            host=intent.lane.host,
            runtime_identity=slot.runtime_identity,
            runtime_settings_sha256=intent.lane.runtime_settings_sha256,
            observed_at=datetime.now(UTC) - timedelta(minutes=2 if self.stale else 0),
            evidence_id="private",
            render=True,
            assets=True,
            warmed=True,
            first_request_ms=0,
            warmed_request_ms=0,
            background_workers_fenced=not intent.lane.writers or slot.slot_id != self.slot.slot_id,
            retained_previous_assets=True,
        )

    def current_route(self, intent: GenericWebCutoverPlan) -> WebRouteObservation:
        return WebRouteObservation(
            route_id=intent.lane.route_id,
            host=intent.lane.host,
            target_url=self.slot.private_url,
            slot_id=self.slot.slot_id,
            evidence_id="applied",
            applied=True,
            authoritative_router_count=1,
            public_health=HealthcheckEvidence(
                verified=True,
                status="pass",
                timeout_seconds=10,
                urls=("http://fixture.invalid/health",),
                observed_runtime_identity=(
                    intent.old if self.wrong_public else self.slot
                ).runtime_identity,
            ),
        )

    def switch(self, intent: GenericWebCutoverPlan, slot: WebSlot) -> WebRouteObservation:
        self.actions.append("switch-" + slot.slot_id)
        self.slot = slot
        if self.switch_uncertain:
            self.switch_uncertain = False
            raise TimeoutError("applied but public response unavailable")
        return self.current_route(intent)

    def handoff_writers(self, _intent: Any, slot: WebSlot) -> None:
        self.actions.append("writers-" + slot.slot_id)

    def drain(self, _intent: Any, slot: WebSlot) -> bool:
        self.actions.append("drain-" + slot.slot_id)
        return self.drained


def deployment(intent: GenericWebCutoverPlan) -> DeploymentRecord:
    return DeploymentRecord(
        record_id="release",
        context="fixture",
        instance="prod",
        source_git_ref=intent.candidate.source_commit,
        deploy=DeploymentEvidence(
            deploy_mode="atomic-two-slot", target_name="fixture", target_type="compose"
        ),
    )


class WebCutoverTests(unittest.TestCase):
    def test_release_and_switch_back_survive_store_reopen_for_both_targets(self) -> None:
        for target in ("application", "compose"):
            for backend in ("files", "sqlite"):
                with self.subTest(target=target, backend=backend), TemporaryDirectory() as folder:
                    root = Path(folder)

                    def open_store() -> Any:
                        return (
                            FilesystemRecordStore(root)
                            if backend == "files"
                            else PostgresRecordStore(
                                database_url="sqlite+pysqlite:///" + str(root / "records.db")
                            )
                        )

                    store = open_store()
                    if backend == "sqlite":
                        store.ensure_schema()
                    intent = plan(target)
                    provider = Provider(intent)
                    persistence = WebDeploymentCutoverStore(store, deployment(intent))
                    result = execute_web_cutover(intent, persistence, provider, record_id="release")
                    self.assertEqual(result.step, "complete")
                    self.assertTrue(result.old_connections_drained)
                    reopened = WebDeploymentCutoverStore(open_store(), deployment(intent))
                    self.assertEqual(reopened.load("release").plan, intent)
                    rolled = rollback_web_cutover(reopened, provider, "release")
                    self.assertEqual(rolled.step, "rolled_back")
                    self.assertEqual(provider.slot, intent.old)
                    self.assertEqual(
                        provider.actions,
                        [
                            "prepare",
                            "switch-new",
                            "writers-new",
                            "drain-old",
                            "switch-old",
                            "writers-old",
                            "drain-new",
                        ],
                    )

    def test_failed_or_stale_private_readiness_keeps_old_serving(self) -> None:
        for failure in ("fail_ready", "stale"):
            with self.subTest(failure=failure), TemporaryDirectory() as folder:
                intent = plan()
                provider = Provider(intent)
                setattr(provider, failure, True)
                store = WebDeploymentCutoverStore(
                    FilesystemRecordStore(Path(folder)), deployment(intent)
                )
                with self.assertRaises(ValueError):
                    execute_web_cutover(intent, store, provider, record_id="release")
                self.assertEqual(provider.slot, intent.old)
                self.assertEqual(provider.actions, ["prepare"])

    def test_unknown_classification_is_recorded_without_any_provider_effect(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan(incompatible=True)
            provider = Provider(intent)
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            with self.assertRaises(ValueError):
                execute_web_cutover(intent, store, provider, record_id="release")
            self.assertEqual(store.load("release").step, "blocked")
            self.assertEqual(store.load("release").plan.compatibility, intent.compatibility)
            self.assertFalse(provider.actions)
            with self.assertRaises(ValueError):
                rollback_web_cutover(store, provider, "release")

    def test_restart_after_uncertain_forward_and_reverse_switch_reads_authority(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan()
            provider = Provider(intent)
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            provider.switch_uncertain = True
            with self.assertRaises(TimeoutError):
                execute_web_cutover(intent, store, provider, record_id="release")
            self.assertEqual(store.load("release").step, "switching")
            resumed = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            execute_web_cutover(intent, resumed, provider, record_id="release")
            self.assertEqual(provider.actions.count("prepare"), 1)
            self.assertEqual(provider.actions.count("switch-new"), 1)
            provider.switch_uncertain = True
            with self.assertRaises(TimeoutError):
                rollback_web_cutover(resumed, provider, "release")
            started = resumed.load("release").rollback_started_at
            rollback_web_cutover(resumed, provider, "release")
            self.assertEqual(provider.actions.count("switch-old"), 1)
            self.assertEqual(resumed.load("release").rollback_started_at, started)

    def test_wrong_public_runtime_after_switch_never_resumes_writers(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan()
            provider = Provider(intent)
            original = provider.switch

            def wrong(intent: Any, slot: Any) -> Any:
                provider.wrong_public = True
                return original(intent, slot)

            provider.switch = wrong  # type: ignore[method-assign]
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            with self.assertRaises(ValueError):
                execute_web_cutover(intent, store, provider, record_id="release")
            self.assertNotIn("writers-new", provider.actions)
            self.assertIsNone(store.load("release").ended_at)

    def test_missing_drain_stays_recoverable_and_rechecks_authority(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan()
            provider = Provider(intent)
            provider.drained = False
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            with self.assertRaises(TimeoutError):
                execute_web_cutover(intent, store, provider, record_id="release")
            self.assertEqual(store.load("release").step, "draining")
            provider.drained = True
            result = execute_web_cutover(intent, store, provider, record_id="release")
            self.assertEqual(result.step, "complete")
            self.assertEqual(provider.actions.count("writers-new"), 1)


class WebCutoverDeploymentTests(unittest.TestCase):
    def seed(self, root: Path, target: str) -> tuple[Any, Any, Any]:
        intent = plan(target)
        production_manifest, candidate_manifest = (
            intent.production.model_copy(deep=True),
            intent.candidate.model_copy(deep=True),
        )
        for manifest in (production_manifest, candidate_manifest):
            manifest.artifact_id = f"{manifest.image.repository}@{manifest.image.digest}"
            assert manifest.source_build is not None
            manifest.source_build.manifest_artifact_id = manifest.artifact_id
        slots = [
            slot.model_copy(
                update={
                    "runtime_identity": slot.runtime_identity.model_copy(
                        update={"artifact_id": manifest.artifact_id}
                    )
                }
            )
            for slot, manifest in zip(
                (intent.old, intent.new), (production_manifest, candidate_manifest), strict=True
            )
        ]
        intent = GenericWebCutoverPlan.model_validate(
            {
                **intent.model_dump(),
                "old": slots[0],
                "new": slots[1],
                "production": production_manifest,
                "candidate": candidate_manifest,
                "compatibility": classify_release_database_compatibility(
                    production=production_manifest, candidate=candidate_manifest
                ),
            }
        )
        store = FilesystemRecordStore(root)
        profile = LaunchplaneProductProfileRecord(
            product="fixture",
            display_name="Fixture",
            image=ProductImageProfile(repository=intent.production.image.repository),
            repository="example/site",
            driver_id="generic-web",
            updated_at=datetime.now(UTC).isoformat(),
            source="fixture",
            lanes=(
                ProductLaneProfile(
                    context="fixture", instance="prod", generic_web_cutover=intent.lane
                ),
            ),
        )
        store.write_product_profile_record(profile)
        for manifest in (intent.production, intent.candidate):
            store.write_artifact_manifest(manifest)
        old = DeploymentRecord(
            record_id="old",
            context="fixture",
            instance="prod",
            source_git_ref=intent.production.source_commit,
            runtime_identity=intent.old.runtime_identity,
            artifact_identity=ArtifactIdentityReference(artifact_id=intent.production.artifact_id),
            deploy=DeploymentEvidence(
                deploy_mode="seed", status="pass", target_name="old", target_type="compose"
            ),
        )
        store.write_deployment_record(old)
        store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=old, updated_at=datetime.now(UTC).isoformat()
            )
        )
        return store, intent, Provider(intent)

    def test_existing_deploy_entrypoint_uses_inactive_slot_and_publishes_inventory(self) -> None:
        for target in ("application", "compose"):
            with self.subTest(target=target), TemporaryDirectory() as folder:
                store, intent, provider = self.seed(Path(folder), target)

                def factory(admitted: Any) -> Any:
                    provider.plan = admitted
                    return provider

                deploy_provider = DokployGenericWebDeployProvider(cutover_provider_factory=factory)
                with patch(
                    "control_plane.workflows.generic_web_deploy.require_forward_lane_build"
                ) as movement:
                    result = execute_generic_web_deploy(
                        control_plane_root=Path(folder),
                        record_store=store,
                        request=GenericWebDeployRequest(
                            product="fixture",
                            instance="prod",
                            artifact_id=intent.candidate.artifact_id,
                            source_git_ref=intent.candidate.source_commit,
                            deploy_reference=intent.new.deploy_reference,
                        ),
                        deploy_provider=deploy_provider,
                        deployment_record_id="release",
                    )
                movement.assert_called_once()
                self.assertEqual(result.deploy_status, "pass")
                current = store.read_environment_inventory(
                    context_name="fixture", instance_name="prod"
                )
                self.assertEqual(current.runtime_identity.slot, "new")
                self.assertEqual(
                    store.read_deployment_record("release").generic_web_cutover.step, "complete"
                )
                rolled = execute_generic_web_deploy(
                    control_plane_root=Path(folder),
                    record_store=store,
                    request=GenericWebDeployRequest(
                        product="fixture",
                        instance="prod",
                        artifact_id=intent.production.artifact_id,
                        source_git_ref=intent.production.source_commit,
                    ),
                    deploy_provider=deploy_provider,
                    rollback=True,
                    deployment_record_id="rollback",
                )
                self.assertEqual(rolled.deploy_status, "pass")
                self.assertEqual(
                    store.read_environment_inventory(
                        context_name="fixture", instance_name="prod"
                    ).runtime_identity.slot,
                    "old",
                )

    def test_missing_provider_cannot_invoke_in_place_deploy(self) -> None:
        with TemporaryDirectory() as folder:
            store, intent, _ = self.seed(Path(folder), "compose")
            with (
                patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"),
                patch.object(DokployGenericWebDeployProvider, "execute_artifact_deploy") as inplace,
            ):
                with self.assertRaises(ValueError):
                    execute_generic_web_deploy(
                        control_plane_root=Path(folder),
                        record_store=store,
                        request=GenericWebDeployRequest(
                            product="fixture",
                            instance="prod",
                            artifact_id=intent.candidate.artifact_id,
                            source_git_ref=intent.candidate.source_commit,
                        ),
                        deployment_record_id="release",
                    )
                inplace.assert_not_called()


class WebCutoverAuthorityTests(unittest.TestCase):
    def test_reonboarding_preserves_exact_lane_traffic_authority(self) -> None:
        from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
        from control_plane.workflows.product_onboarding import build_product_profile_record
        from tests.test_product_onboarding import _manifest_payload

        manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
        profile = build_product_profile_record(
            manifest=manifest, updated_at=datetime.now(UTC).isoformat()
        )
        selected = profile.lanes[0]
        selected.generic_web_cutover = plan().lane
        refreshed = build_product_profile_record(
            manifest=manifest, existing_profile=profile, updated_at=datetime.now(UTC).isoformat()
        )
        self.assertEqual(refreshed.lanes[0].generic_web_cutover, selected.generic_web_cutover)

    def test_retained_cutover_prevents_stale_profile_or_removed_config_bypass(self) -> None:
        with TemporaryDirectory() as folder:
            store, intent, provider = WebCutoverDeploymentTests().seed(Path(folder), "compose")
            stale = store.read_product_profile_record("fixture")
            provider.fail_ready = True
            selected = DokployGenericWebDeployProvider(cutover_provider_factory=lambda _p: provider)
            request = GenericWebDeployRequest(
                product="fixture",
                instance="prod",
                artifact_id=intent.candidate.artifact_id,
                source_git_ref=intent.candidate.source_commit,
            )
            with patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"):
                with self.assertRaises(ValueError):
                    execute_generic_web_deploy(
                        control_plane_root=Path(folder),
                        record_store=store,
                        request=request,
                        deploy_provider=selected,
                        deployment_record_id="held",
                    )
            current = store.read_product_profile_record("fixture")
            current.lanes[0].generic_web_cutover = None
            store.write_product_profile_record(current)
            with patch.object(DokployGenericWebDeployProvider, "execute_artifact_deploy") as legacy:
                with self.assertRaises(ValueError):
                    execute_generic_web_deploy(
                        control_plane_root=Path(folder),
                        record_store=store,
                        request=request,
                        profile=stale,
                        deploy_provider=selected,
                        deployment_record_id="another",
                    )
                legacy.assert_not_called()


class WebCutoverCallerRegressionTests(unittest.TestCase):
    def test_existing_application_rollback_planner_and_apply_use_retained_slot(self) -> None:
        from control_plane.contracts.generic_web_rollback import (
            GenericWebRollbackPlanRequest,
            build_generic_web_rollback_plan,
        )
        from control_plane.workflows.generic_web_rollback import execute_generic_web_rollback

        with TemporaryDirectory() as folder:
            root = Path(folder)
            store, intent, provider = WebCutoverDeploymentTests().seed(root, "application")
            selected = DokployGenericWebDeployProvider(cutover_provider_factory=lambda _p: provider)
            with patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"):
                result = execute_generic_web_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=GenericWebDeployRequest(
                        product="fixture",
                        instance="prod",
                        artifact_id=intent.candidate.artifact_id,
                        source_git_ref=intent.candidate.source_commit,
                        deploy_reference=intent.new.deploy_reference,
                    ),
                    deploy_provider=selected,
                    deployment_record_id="forward",
                )
            # The next promotion's preflight plans against the current forward
            # record; it must not require a tag for retained traffic authority.
            next_target = build_generic_web_rollback_plan(
                record_store=store,
                request=GenericWebRollbackPlanRequest(
                    product="fixture", rollback_deployment_record_id=result.deployment_record_id
                ),
            )
            self.assertEqual(next_target.status, "ready")
            rolled = execute_generic_web_rollback(
                control_plane_root=root,
                record_store=store,
                request=GenericWebRollbackPlanRequest(
                    product="fixture", rollback_deployment_record_id="old"
                ),
                deploy_provider=selected,
            )
            self.assertEqual(rolled.rollback_status, "pass")
            self.assertEqual(provider.slot.slot_id, intent.old.slot_id)
            after = build_generic_web_rollback_plan(
                record_store=store,
                request=GenericWebRollbackPlanRequest(
                    product="fixture", rollback_deployment_record_id=rolled.deployment_record_id
                ),
            )
            self.assertEqual(after.status, "ready")

    def test_pre_switch_abort_retains_active_writers_and_finalizes_failed_attempt(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store, intent, provider = WebCutoverDeploymentTests().seed(root, "compose")
            selected = DokployGenericWebDeployProvider(cutover_provider_factory=lambda _p: provider)
            request = GenericWebDeployRequest(
                product="fixture",
                instance="prod",
                artifact_id=intent.candidate.artifact_id,
                source_git_ref=intent.candidate.source_commit,
            )
            provider.fail_ready = True
            with patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"):
                with self.assertRaises(ValueError):
                    execute_generic_web_deploy(
                        control_plane_root=root,
                        record_store=store,
                        request=request,
                        deploy_provider=selected,
                        deployment_record_id="failed",
                    )
            provider.fail_ready = False
            self.assertFalse(
                provider.readiness(provider.plan, provider.plan.old).background_workers_fenced
            )
            rolled = execute_generic_web_deploy(
                control_plane_root=root,
                record_store=store,
                request=GenericWebDeployRequest(
                    product="fixture",
                    instance="prod",
                    artifact_id=intent.production.artifact_id,
                    source_git_ref=intent.production.source_commit,
                ),
                rollback=True,
                deploy_provider=selected,
                deployment_record_id="abort",
            )
            self.assertEqual(rolled.deploy_status, "pass")
            self.assertEqual(store.read_deployment_record("failed").deploy.status, "fail")
            self.assertNotIn("switch-old", provider.actions)
            self.assertEqual(
                store.read_deployment_record("failed").generic_web_cutover.step, "rolled_back"
            )

    def test_unknown_release_finalizes_failure_without_provider_or_legacy_effects(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            store, intent, _ = WebCutoverDeploymentTests().seed(root, "compose")
            candidate = intent.candidate.model_copy(update={"release_compatibility": None})
            store.write_artifact_manifest(candidate)
            with patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"):
                with self.assertRaises(ValueError):
                    execute_generic_web_deploy(
                        control_plane_root=root,
                        record_store=store,
                        request=GenericWebDeployRequest(
                            product="fixture",
                            instance="prod",
                            artifact_id=candidate.artifact_id,
                            source_git_ref=candidate.source_commit,
                        ),
                        deployment_record_id="unknown",
                    )
            blocked = store.read_deployment_record("unknown")
            self.assertEqual(blocked.deploy.status, "fail")
            self.assertEqual(blocked.generic_web_cutover.step, "blocked")
            self.assertTrue(blocked.deploy.finished_at)

    def test_http_mutation_adapter_reserves_route_and_resumes_exact_uncertain_cutover(self) -> None:
        from control_plane.generic_web_deploy_provider_adapter import (
            GenericWebDeployProviderMutationAdapter,
        )
        from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
        from control_plane.provider_operations import ProviderMutationUnknownError

        with TemporaryDirectory() as folder:
            root = Path(folder)
            store, intent, provider = WebCutoverDeploymentTests().seed(root, "compose")
            selected = DokployGenericWebDeployProvider(cutover_provider_factory=lambda _p: provider)
            profile = store.read_product_profile_record("fixture")
            envelope = GenericWebDeployEnvelope(
                product="fixture",
                deploy=GenericWebDeployRequest(
                    product="fixture",
                    instance="prod",
                    artifact_id=intent.candidate.artifact_id,
                    source_git_ref=intent.candidate.source_commit,
                ),
            )
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=root,
                record_store=store,
                deploy_request=envelope,
                profile=profile,
                lane=profile.lanes[0],
                trace_id="fixture-trace",
                deploy_provider=selected,
            )
            lease: Any = type(
                "Lease",
                (),
                {"checkpoint_effect": lambda _s, _phase: None, "assert_current": lambda _s: None},
            )()
            with (
                patch.object(
                    selected, "resolve_deploy_target", side_effect=AssertionError("legacy target")
                ),
                patch.object(
                    selected,
                    "observe_artifact_deploy",
                    side_effect=AssertionError("legacy observation"),
                ),
                patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"),
            ):
                key = adapter.reconciliation_key()
                provider.switch_uncertain = True
                with self.assertRaises(ProviderMutationUnknownError):
                    adapter.apply("fixture-operation", lease)
                inspection = adapter.inspect(
                    provider_operation_key="fixture-operation",
                    provider_effect_phase="atomic_switch",
                    reconciliation_key=key,
                )
                self.assertTrue(inspection.retry_safe)
                self.assertEqual(inspection.provider_evidence, "cutover_checkpoint_recoverable")
                outcome = adapter.apply("fixture-operation", lease)
                self.assertTrue(outcome.durable)
                observed = adapter.observe("fixture-operation", "records_commit", key)
                self.assertEqual(observed.outcome, "present")
                self.assertEqual(provider.actions.count("prepare"), 1)
                self.assertEqual(provider.actions.count("switch-new"), 1)


class WebCutoverUnhealthyRecoveryTests(unittest.TestCase):
    def test_unhealthy_candidate_can_switch_back_using_independent_applied_authority(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan()
            provider = Provider(intent)
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            execute_web_cutover(intent, store, provider, record_id="release")
            original = provider.current_route

            def unhealthy(intent: GenericWebCutoverPlan) -> WebRouteObservation:
                route = original(intent)
                if provider.slot.slot_id == intent.new.slot_id:
                    return route.model_copy(
                        update={"public_health": HealthcheckEvidence(status="fail")}
                    )
                return route

            provider.current_route = unhealthy  # type: ignore[method-assign]
            rolled = rollback_web_cutover(store, provider, "release")
            self.assertEqual(rolled.step, "rolled_back")
            self.assertEqual(provider.slot, intent.old)
