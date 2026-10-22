"""Exact-artifact, failure, restart and both-target generic-web cutover behavior."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
import unittest

import click
from unittest.mock import Mock, patch

from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
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
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
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
        self.writer_slot = intent.old
        self.published_slot = intent.old
        self.delay_apply = False
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
            background_workers_fenced=not intent.lane.writers
            or slot.slot_id != self.writer_slot.slot_id,
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
        if (
            self.published_slot.private_url != slot.private_url
            or self.slot.private_url != slot.private_url
        ):
            self.actions.append("switch-" + slot.slot_id)
        self.published_slot = slot
        if self.delay_apply:
            self.delay_apply = False
            raise TimeoutError("published file not yet applied")
        self.slot = slot
        if self.switch_uncertain:
            self.switch_uncertain = False
            raise TimeoutError("applied but public response unavailable")
        return self.current_route(intent)

    def handoff_writers(self, _intent: Any, slot: WebSlot) -> None:
        self.actions.append("writers-" + slot.slot_id)
        self.writer_slot = slot

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
    @staticmethod
    def seed(
        root: Path, target: str
    ) -> tuple[FilesystemRecordStore, GenericWebCutoverPlan, Provider]:
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

    def test_promotion_checkpoint_keeps_logical_artifact_when_cutover_resolves_image_digest(
        self,
    ) -> None:
        from control_plane.contracts.deploy_target import ProviderTargetRecord
        from control_plane.generic_web_promotion_provider_adapter import (
            GenericWebProdPromotionProviderMutationAdapter,
        )
        from control_plane.generic_web_promotion_http import GenericWebProdPromotionEnvelope
        from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionRequest
        from control_plane.provider_operations import (
            run_durable_provider_operation,
            ProviderMutationUnknownError,
        )

        with TemporaryDirectory() as folder:
            root = Path(folder)
            recorded, intent, _ = self.seed(root, "compose")
            profile = recorded.read_product_profile_record("fixture")
            lane = profile.lanes[0]
            profile = profile.model_copy(
                update={
                    "lanes": (ProductLaneProfile(context=lane.context, instance="testing"), lane)
                }
            )
            manifest = intent.candidate.model_copy(deep=True)
            logical = manifest.image.repository + ":sha-" + manifest.source_commit
            manifest.artifact_id = logical
            assert manifest.source_build is not None
            manifest.source_build.manifest_artifact_id = logical
            identity = intent.new.runtime_identity.model_copy(
                update={
                    "instance": "testing",
                    "artifact_id": logical,
                    "deployment_record_id": "testing-logical",
                }
            )
            testing = DeploymentRecord(
                record_id=identity.deployment_record_id,
                context=lane.context,
                instance="testing",
                artifact_identity=ArtifactIdentityReference(artifact_id=logical),
                source_git_ref=manifest.source_commit,
                runtime_identity=identity,
                resolved_target=ResolvedTargetEvidence(
                    target_type="application", target_id="testing-target", target_name="testing"
                ),
                deploy=DeploymentEvidence(
                    target_name="testing",
                    target_type="application",
                    deploy_mode="fixture",
                    status="pass",
                ),
            )
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'native.db'}")
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(profile)
            store.write_artifact_manifest(manifest)
            store.write_deployment_record(testing)
            store.write_environment_inventory(
                build_environment_inventory(
                    deployment_record=testing, updated_at=profile.updated_at
                )
            )
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context=lane.context,
                    instance="testing",
                    provider_id="dokploy",
                    target_category="application",
                    provider_target_type="application",
                    target_id="testing-target",
                    display_name="testing",
                    updated_at=profile.updated_at,
                )
            )
            adapter = GenericWebProdPromotionProviderMutationAdapter(
                control_plane_root=root,
                record_store=store,
                profile=profile,
                lane=lane,
                promotion_request=GenericWebProdPromotionEnvelope(
                    product=profile.product,
                    promotion=GenericWebProdPromotionRequest(
                        product=profile.product,
                    ),
                ),
                trace_id="cutover-source",
                validate_before_effect=lambda _: None,
                deploy_provider=Mock(provider_id="dokploy"),
            )
            admitted: list[str] = []

            def stop_after_checkpoint(**kwargs: Any) -> Any:
                kwargs["provider_effect_checkpoint"]("cutover_first_effect")
                admitted.append(kwargs["request"].promotion.artifact_id)
                raise ProviderMutationUnknownError(
                    "Controlled stop after admission, before provider I/O."
                )

            with patch(
                "control_plane.generic_web_promotion_provider_adapter.execute_generic_web_prod_promotion_result",
                side_effect=stop_after_checkpoint,
            ):
                result = run_durable_provider_operation(
                    store=store,
                    scope="cutover-source",
                    route_path="/fixture/promotion",
                    idempotency_key="logical-cutover",
                    request_fingerprint="f" * 64,
                    lease_owner="fixture",
                    response_trace_id="cutover-source",
                    adapter=adapter,
                )
            self.assertEqual(admitted, [logical])
            assert result.record is not None
            self.assertTrue(result.record.provider_effect_started_at)

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
                assert current.runtime_identity is not None
                self.assertEqual(current.runtime_identity.slot, "new")
                completed = store.read_deployment_record("release").generic_web_cutover
                assert completed is not None
                self.assertEqual(completed.step, "complete")
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
                current = store.read_environment_inventory(
                    context_name="fixture", instance_name="prod"
                )
                assert current.runtime_identity is not None
                self.assertEqual(
                    current.runtime_identity.slot,
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

    def test_non_production_cutover_refuses_failing_integration_key_before_provider(
        self,
    ) -> None:
        with TemporaryDirectory() as folder:
            store, intent, provider = self.seed(Path(folder), "compose")
            profile = store.read_product_profile_record("fixture")
            store.write_product_profile_record(
                profile.model_copy(
                    update={"lanes": (profile.lanes[0].model_copy(update={"instance": "testing"}),)}
                )
            )
            store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="policy",
                    source="test",
                    updated_at=datetime.now(UTC).isoformat(),
                    rules=(
                        RuntimeSecretSafetyRule(binding_key="UNRELATED", secret_class="testing"),
                    ),
                )
            )
            # A context-wide production key with no classification reaches the testing lane.
            store.write_secret_binding(
                SecretBinding(
                    binding_id="binding-stripe",
                    secret_id="secret-stripe",
                    integration="runtime_environment",
                    binding_key="STRIPE_SECRET_KEY",
                    context="fixture",
                    created_at=datetime.now(UTC).isoformat(),
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            factory_calls: list[object] = []

            def factory(admitted: Any) -> Any:
                factory_calls.append(admitted)
                return provider

            with (
                patch.object(DokployGenericWebDeployProvider, "execute_artifact_deploy") as inplace,
                self.assertRaises(ValueError) as refused,
            ):
                execute_generic_web_deploy(
                    control_plane_root=Path(folder),
                    record_store=store,
                    request=GenericWebDeployRequest(
                        product="fixture",
                        instance="testing",
                        artifact_id=intent.candidate.artifact_id,
                        source_git_ref=intent.candidate.source_commit,
                        deploy_reference=intent.new.deploy_reference,
                    ),
                    deploy_provider=DokployGenericWebDeployProvider(
                        cutover_provider_factory=factory
                    ),
                    deployment_record_id="release",
                )
            self.assertIn("STRIPE_SECRET_KEY", str(refused.exception))
            self.assertEqual(factory_calls, [])
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
            failed = store.read_deployment_record("failed").generic_web_cutover
            assert failed is not None
            self.assertEqual(failed.step, "rolled_back")

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
            assert blocked.generic_web_cutover is not None
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
                self.assertEqual(inspection.observation.outcome, "absent")
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


class WebCutoverReviewRecoveryTests(unittest.TestCase):
    def test_public_failure_before_writer_handoff_keeps_old_owner_and_can_switch_back(self) -> None:
        for bad_identity in (False, True):
            with self.subTest(bad_identity=bad_identity), TemporaryDirectory() as folder:
                intent = plan()
                provider = Provider(intent)
                store = WebDeploymentCutoverStore(
                    FilesystemRecordStore(Path(folder)), deployment(intent)
                )
                original = provider.current_route

                def failed(intent: GenericWebCutoverPlan) -> WebRouteObservation:
                    route = original(intent)
                    if provider.slot.slot_id == intent.new.slot_id:
                        health = HealthcheckEvidence(status="fail")
                        if bad_identity:
                            health = route.public_health.model_copy(
                                update={"observed_runtime_identity": intent.old.runtime_identity}
                            )
                        return route.model_copy(update={"public_health": health})
                    return route

                with patch.object(provider, "current_route", side_effect=failed):
                    with self.assertRaises(ValueError):
                        execute_web_cutover(intent, store, provider, record_id="release")
                    self.assertEqual(store.load("release").step, "switching")
                    self.assertEqual(provider.writer_slot.slot_id, intent.old.slot_id)
                    self.assertFalse(
                        provider.readiness(intent, intent.old).background_workers_fenced
                    )
                    rolled = rollback_web_cutover(store, provider, "release")
                self.assertEqual(rolled.step, "rolled_back")
                self.assertEqual(provider.writer_slot.slot_id, intent.old.slot_id)
                self.assertNotIn("writers-new", provider.actions)

    def test_abort_cancels_unapplied_forward_intent_before_a_delayed_event(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan()
            provider = Provider(intent)
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            provider.delay_apply = True
            with self.assertRaises(TimeoutError):
                execute_web_cutover(intent, store, provider, record_id="release")
            self.assertEqual(provider.slot.slot_id, intent.old.slot_id)
            self.assertEqual(provider.published_slot.slot_id, intent.new.slot_id)
            rollback_web_cutover(store, provider, "release")
            # Simulate a delayed watch event or proxy restart consuming disk.
            provider.slot = provider.published_slot
            self.assertEqual(provider.slot.slot_id, intent.old.slot_id)
            self.assertEqual(store.load("release").step, "rolled_back")

    def test_candidate_from_another_image_repository_cannot_reserve_a_route(self) -> None:
        from control_plane.generic_web_cutover import resolve_web_cutover_target

        with TemporaryDirectory() as folder:
            store, intent, _ = WebCutoverDeploymentTests().seed(Path(folder), "compose")
            candidate = intent.candidate.model_copy(deep=True)
            candidate.image.repository = "ghcr.io/example/foreign"
            store.write_artifact_manifest(candidate)
            profile = store.read_product_profile_record("fixture")
            with self.assertRaises(ValueError):
                resolve_web_cutover_target(
                    store,
                    profile=profile,
                    lane=profile.lanes[0],
                    artifact_id=candidate.artifact_id,
                    source_git_ref=candidate.source_commit,
                    deploy_reference="",
                    provider_id="dokploy",
                    timeout_seconds=None,
                )

    @staticmethod
    def promotion_fixture(
        root: Path, target: str
    ) -> tuple[FilesystemRecordStore, GenericWebCutoverPlan, Provider]:
        store, intent, provider = WebCutoverDeploymentTests.seed(root, target)
        profile = store.read_product_profile_record("fixture")
        profile.lanes = (
            ProductLaneProfile(context="fixture", instance="testing"),
            *profile.lanes,
        )
        store.write_product_profile_record(profile)
        old_inventory = store.read_environment_inventory(
            context_name="fixture", instance_name="prod"
        )
        testing_record = DeploymentRecord(
            record_id="qualified-testing",
            context="fixture",
            instance="testing",
            artifact_identity=ArtifactIdentityReference(artifact_id=intent.candidate.artifact_id),
            source_git_ref=intent.candidate.source_commit,
            runtime_identity=intent.new.runtime_identity.model_copy(
                update={"instance": "testing", "deployment_record_id": "qualified-testing"}
            ),
            deploy=DeploymentEvidence(
                deploy_mode="seed",
                status="pass",
                target_name="testing",
                target_type="application",
            ),
            resolved_target=ResolvedTargetEvidence(
                target_type="application", target_id="testing-target", target_name="testing"
            ),
        )
        from control_plane.contracts.deploy_target import ProviderTargetRecord

        assert testing_record.deployed_target is not None
        store.write_provider_target_record(
            ProviderTargetRecord(
                context=testing_record.context,
                instance=testing_record.instance,
                provider_id=testing_record.deployed_target.provider_id,
                target_category=testing_record.deployed_target.target_category,
                provider_target_type=testing_record.deployed_target.provider_target_type,
                target_id=testing_record.deployed_target.target_id,
                display_name=testing_record.deployed_target.display_name,
                updated_at=old_inventory.updated_at,
            )
        )
        store.write_deployment_record(testing_record)
        store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=testing_record, updated_at=old_inventory.updated_at
            )
        )
        return store, intent, provider

    def test_promotion_failure_after_switch_runs_automatic_switch_back_and_finalizes(self) -> None:
        from control_plane.workflows.generic_web_promotion import (
            GenericWebProdPromotionRequest,
            execute_generic_web_prod_promotion,
        )
        from tests.support.promotion_backup import stub_verified_promotion_backup

        stub_verified_promotion_backup(self, "control_plane.workflows.generic_web_promotion")
        for failure in ("handoff", "drain"):
            with self.subTest(failure=failure), TemporaryDirectory() as folder:
                root = Path(folder)
                store, intent, provider = self.promotion_fixture(root, "application")
                original_handoff = provider.handoff_writers

                def handoff(p: GenericWebCutoverPlan, slot: WebSlot) -> None:
                    if failure == "handoff" and slot.slot_id == p.new.slot_id:
                        raise ValueError("injected candidate worker handoff failure")
                    original_handoff(p, slot)

                def drain(p: GenericWebCutoverPlan, slot: WebSlot) -> bool:
                    return not (failure == "drain" and slot.slot_id == p.old.slot_id)

                selected = DokployGenericWebDeployProvider(
                    cutover_provider_factory=lambda _p: provider
                )
                with (
                    patch.object(provider, "handoff_writers", side_effect=handoff),
                    patch.object(provider, "drain", side_effect=drain),
                    patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"),
                    patch(
                        "control_plane.workflows.generic_web_promotion.require_release_approval"
                    ) as approval,
                ):
                    result = execute_generic_web_prod_promotion(
                        control_plane_root=root,
                        record_store=store,
                        request=GenericWebProdPromotionRequest(
                            product="fixture",
                            artifact_id=intent.candidate.artifact_id,
                            deploy_reference=intent.new.deploy_reference,
                            source_git_ref=intent.candidate.source_commit,
                            backup_record_id="infrastructure-example",
                            verify_health=False,
                        ),
                        deploy_provider=selected,
                        deployment_record_id="promotion-attempt",
                    )
                approval.assert_called_once()
                self.assertEqual(result.promotion_status, "fail")
                self.assertEqual(result.rollback_status, "pass")
                self.assertEqual(provider.slot.slot_id, intent.old.slot_id)
                self.assertEqual(provider.writer_slot.slot_id, intent.old.slot_id)
                attempt = store.read_deployment_record("promotion-attempt")
                assert attempt.generic_web_cutover is not None
                self.assertEqual(attempt.generic_web_cutover.step, "rolled_back")
                self.assertEqual(attempt.deploy.status, "fail")
                self.assertEqual(
                    store.read_promotion_record(result.promotion_record_id).deploy.status, "fail"
                )
                restored = store.read_environment_inventory(
                    context_name="fixture", instance_name="prod"
                )
                assert restored.runtime_identity is not None
                self.assertEqual(
                    restored.runtime_identity.slot,
                    intent.old.slot_id,
                )

    def test_cutover_receipt_storage_failure_never_marks_effect_or_calls_provider(self) -> None:
        with TemporaryDirectory() as folder:
            intent = plan()
            provider = Provider(intent)
            store = WebDeploymentCutoverStore(
                FilesystemRecordStore(Path(folder)), deployment(intent)
            )
            checkpoint = Mock()
            with patch.object(store, "save", side_effect=OSError("receipt storage unavailable")):
                with self.assertRaises(OSError):
                    execute_web_cutover(
                        intent, store, provider, record_id="release", checkpoint=checkpoint
                    )
            checkpoint.assert_not_called()
            self.assertEqual(provider.actions, [])

    def test_first_checkpoint_refusal_never_rolls_back_a_new_cutover(self) -> None:
        self.cutover_checkpoint_failure(prior_dispatch=False)

    def test_non_source_first_checkpoint_refusal_does_not_hold_next_release(self) -> None:
        from control_plane.workflows.generic_web_promotion import (
            GenericWebProdPromotionRequest,
            execute_generic_web_prod_promotion,
        )
        from tests.support.promotion_backup import stub_verified_promotion_backup

        stub_verified_promotion_backup(self, "control_plane.workflows.generic_web_promotion")
        for target in ("application", "compose"):
            with self.subTest(target=target), TemporaryDirectory() as folder:
                root = Path(folder)
                store, intent, provider = self.promotion_fixture(root, target)
                selected = DokployGenericWebDeployProvider(
                    cutover_provider_factory=lambda _p: provider
                )
                request = GenericWebProdPromotionRequest(
                    product="fixture",
                    artifact_id=intent.candidate.artifact_id,
                    deploy_reference=intent.new.deploy_reference,
                    source_git_ref=intent.candidate.source_commit,
                    backup_record_id="infrastructure-example",
                    verify_health=False,
                )

                def refuse(_phase: str) -> None:
                    raise ValueError("Synthetic current authority changed")

                with (
                    patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"),
                    patch("control_plane.workflows.generic_web_promotion.require_release_approval"),
                ):
                    with self.assertRaises(ValueError):
                        execute_generic_web_prod_promotion(
                            control_plane_root=root,
                            record_store=store,
                            request=request,
                            deploy_provider=selected,
                            deployment_record_id="refused-attempt",
                            provider_effect_checkpoint=refuse,
                        )
                    saved = store.read_deployment_record("refused-attempt")
                    assert saved.generic_web_cutover is not None
                    self.assertEqual(saved.generic_web_cutover.step, "blocked")
                    self.assertEqual(saved.deploy.status, "fail")
                    self.assertEqual(provider.actions, [])
                    result = execute_generic_web_deploy(
                        control_plane_root=root,
                        record_store=store,
                        request=GenericWebDeployRequest(
                            product="fixture",
                            instance="prod",
                            artifact_id=intent.candidate.artifact_id,
                            deploy_reference=intent.new.deploy_reference,
                            source_git_ref=intent.candidate.source_commit,
                        ),
                        deploy_provider=selected,
                        deployment_record_id="next-attempt",
                    )
                    self.assertEqual(result.deploy_status, "pass")

    def test_preexisting_preparing_cutover_keeps_recovery_after_checkpoint_refusal(self) -> None:
        self.cutover_checkpoint_failure(prior_dispatch=True)

    def cutover_checkpoint_failure(self, *, prior_dispatch: bool) -> None:
        from control_plane.workflows.generic_web_promotion import (
            GenericWebProdPromotionRequest,
            execute_generic_web_prod_promotion,
        )
        from tests.support.promotion_backup import stub_verified_promotion_backup

        stub_verified_promotion_backup(self, "control_plane.workflows.generic_web_promotion")
        for target in ("application", "compose"):
            with self.subTest(target=target), TemporaryDirectory() as folder:
                root = Path(folder)
                store, intent, provider = self.promotion_fixture(root, target)
                baseline = store.read_environment_inventory(
                    context_name="fixture", instance_name="prod"
                )
                selected = DokployGenericWebDeployProvider(
                    cutover_provider_factory=lambda _p: provider
                )
                with (
                    patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"),
                    patch("control_plane.workflows.generic_web_promotion.require_release_approval"),
                ):
                    if prior_dispatch:
                        prepare = provider.prepare_candidate

                        def dispatched(p: GenericWebCutoverPlan) -> None:
                            prepare(p)
                            raise TimeoutError("original prepare reply lost")

                        with patch.object(provider, "prepare_candidate", side_effect=dispatched):
                            with self.assertRaises(
                                (ValueError, TimeoutError, click.ClickException)
                            ):
                                execute_generic_web_deploy(
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
                                    deployment_record_id="promotion-attempt",
                                )
                        saved = store.read_deployment_record("promotion-attempt")
                        assert saved.generic_web_cutover is not None
                        self.assertEqual(saved.generic_web_cutover.step, "preparing")
                        self.assertEqual(provider.actions, ["prepare"])

                    qualified = store.read_deployment_record("qualified-testing")
                    refuse_source = True

                    def fail_testing(_evidence: HealthcheckEvidence) -> HealthcheckEvidence:
                        if not refuse_source:
                            return _evidence
                        record = store.read_deployment_record("qualified-testing")
                        store.write_deployment_record(
                            record.model_copy(
                                update={
                                    "deploy": record.deploy.model_copy(update={"status": "fail"})
                                }
                            )
                        )
                        return _evidence

                    request = GenericWebProdPromotionRequest(
                        product="fixture",
                        artifact_id=intent.candidate.artifact_id,
                        deploy_reference=intent.new.deploy_reference,
                        source_git_ref=intent.candidate.source_commit,
                        backup_record_id="infrastructure-example",
                        verify_health=False,
                    )
                    with patch(
                        "control_plane.workflows.generic_web_promotion._verify_health_evidence",
                        side_effect=fail_testing,
                    ):
                        if prior_dispatch:
                            result = execute_generic_web_prod_promotion(
                                control_plane_root=root,
                                record_store=store,
                                request=request,
                                deploy_provider=selected,
                                deployment_record_id="promotion-attempt",
                            )
                            self.assertEqual(result.rollback_status, "pass")
                            self.assertIn("writers-" + intent.old.slot_id, provider.actions)
                            self.assertIn("drain-" + intent.new.slot_id, provider.actions)
                        else:
                            for _ in range(2):
                                store.write_deployment_record(qualified)
                                with self.assertRaises((ValueError, click.ClickException)):
                                    execute_generic_web_prod_promotion(
                                        control_plane_root=root,
                                        record_store=store,
                                        request=request,
                                        deploy_provider=selected,
                                        deployment_record_id="promotion-attempt",
                                    )
                                self.assertEqual(provider.actions, [])
                                with self.assertRaises(FileNotFoundError):
                                    store.read_deployment_record("promotion-attempt")
                                self.assertEqual(
                                    store.read_environment_inventory(
                                        context_name="fixture", instance_name="prod"
                                    ),
                                    baseline,
                                )
                            store.write_deployment_record(qualified)
                            refuse_source = False
                            result = execute_generic_web_prod_promotion(
                                control_plane_root=root,
                                record_store=store,
                                request=request,
                                deploy_provider=selected,
                                deployment_record_id="promotion-attempt",
                            )
                            self.assertEqual(result.promotion_status, "pass")
                            self.assertEqual(provider.slot.slot_id, intent.new.slot_id)


class WebCutoverRefusalObservationTests(unittest.TestCase):
    def test_refusal_checkpoint_and_empty_effect_journal_allow_pre_effect_recovery(self) -> None:
        from control_plane.generic_web_deploy_provider_adapter import (
            GenericWebDeployProviderMutationAdapter,
        )
        from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
        from control_plane.provider_operations import ProviderMutationRejectedError

        with TemporaryDirectory() as folder:
            root = Path(folder)
            store, intent, _ = WebCutoverDeploymentTests().seed(root, "compose")
            candidate = intent.candidate.model_copy(update={"release_compatibility": None})
            store.write_artifact_manifest(candidate)
            profile = store.read_product_profile_record("fixture")
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=root,
                record_store=store,
                deploy_request=GenericWebDeployEnvelope(
                    product="fixture",
                    deploy=GenericWebDeployRequest(
                        product="fixture",
                        instance="prod",
                        artifact_id=candidate.artifact_id,
                        source_git_ref=candidate.source_commit,
                    ),
                ),
                profile=profile,
                lane=profile.lanes[0],
                trace_id="fixture",
            )
            lease: Any = type(
                "Lease",
                (),
                {"checkpoint_effect": lambda _s, _phase: self.fail("refusal performed an effect")},
            )()
            with patch("control_plane.workflows.generic_web_deploy.require_forward_lane_build"):
                key = adapter.reconciliation_key()
                with self.assertRaises(ProviderMutationRejectedError):
                    adapter.apply("refused", lease)
            inspection = adapter.inspect(
                provider_operation_key="refused", provider_effect_phase="", reconciliation_key=key
            )
            self.assertEqual(inspection.observation.outcome, "absent")
            self.assertTrue(inspection.retry_safe)
            contradictory = adapter.inspect(
                provider_operation_key="refused",
                provider_effect_phase="atomic_switch",
                reconciliation_key=key,
            )
            self.assertEqual(contradictory.observation.outcome, "unknown")
            self.assertFalse(contradictory.retry_safe)


class WebCutoverRecordsCommitTests(unittest.TestCase):
    def test_terminal_traffic_checkpoint_holds_lane_until_outer_commit_and_resumes(self) -> None:
        from control_plane.generic_web_cutover import execute_web_cutover_deployment

        for operation in ("forward", "abort", "switch-back"):
            for boundary in ("before_commit", "inventory", "acknowledge"):
                with (
                    self.subTest(operation=operation, boundary=boundary),
                    TemporaryDirectory() as folder,
                ):
                    store, intent, provider = WebCutoverDeploymentTests().seed(
                        Path(folder), "compose"
                    )
                    lane = store.read_product_profile_record("fixture").lanes[0]

                    def run(*, reverse: bool = False, record_id: str = "release", **kw: Any) -> Any:
                        return execute_web_cutover_deployment(
                            store,
                            product="fixture",
                            lane=lane,
                            artifact_id=(
                                intent.production if reverse else intent.candidate
                            ).artifact_id,
                            source_git_ref=(
                                intent.production if reverse else intent.candidate
                            ).source_commit,
                            deploy_reference="" if reverse else intent.new.deploy_reference,
                            record_id=record_id,
                            factory=lambda _p: provider,
                            rollback=reverse,
                            **kw,
                        )

                    if operation == "abort":
                        provider.fail_ready = True
                        with self.assertRaises(ValueError):
                            run()
                        provider.fail_ready = False
                    elif operation == "switch-back":
                        run()
                    reverse = operation != "forward"
                    result_id = "reverse" if reverse else "release"
                    original_save = WebDeploymentCutoverStore.save

                    def interrupted_ack(holder: Any, record: Any) -> None:
                        if record.records_committed:
                            raise TimeoutError("injected records acknowledgement loss")
                        original_save(holder, record)

                    if boundary == "before_commit":

                        def before_commit() -> None:
                            raise TimeoutError("injected lease loss")

                        with self.assertRaises(TimeoutError):
                            run(
                                reverse=reverse,
                                record_id=result_id,
                                before_records_commit=before_commit,
                            )
                    elif boundary == "inventory":
                        with patch.object(
                            store, "write_environment_inventory", side_effect=TimeoutError
                        ):
                            with self.assertRaises(TimeoutError):
                                run(reverse=reverse, record_id=result_id)
                    else:
                        with patch.object(WebDeploymentCutoverStore, "save", interrupted_ack):
                            with self.assertRaises(TimeoutError):
                                run(reverse=reverse, record_id=result_id)
                    held = store.read_deployment_record("release").generic_web_cutover
                    assert held is not None
                    self.assertEqual(held.step, "rolled_back" if reverse else "complete")
                    self.assertFalse(held.records_committed)
                    actions = tuple(provider.actions)
                    with self.assertRaises(ValueError):
                        run(record_id="unrelated")
                    self.assertEqual(tuple(provider.actions), actions)
                    # Reopen storage and recover through the public bridge, not
                    # the inner state machine. Both stale/correct inventory work.
                    store = FilesystemRecordStore(Path(folder))
                    result = run(reverse=reverse, record_id=result_id)
                    self.assertEqual(result.deploy.status, "pass")
                    committed = store.read_deployment_record("release").generic_web_cutover
                    assert committed is not None
                    self.assertTrue(committed.records_committed)
                    self.assertEqual(
                        store.read_environment_inventory(
                            context_name=lane.context, instance_name=lane.instance
                        ).deployment_record_id,
                        result.record_id,
                    )
                    if reverse:
                        self.assertNotEqual(
                            store.read_deployment_record("release").deploy.status, "pending"
                        )
                        # A lost outer response after all records committed is
                        # also retryable with the same rollback result id.
                        self.assertEqual(run(reverse=True, record_id=result_id), result)

    def test_application_without_provider_reference_rejects_before_intent_or_reservation(
        self,
    ) -> None:
        from control_plane.generic_web_cutover import (
            execute_web_cutover_deployment,
            resolve_web_cutover_target,
        )

        with TemporaryDirectory() as folder:
            store, intent, provider = WebCutoverDeploymentTests().seed(Path(folder), "application")
            profile = store.read_product_profile_record("fixture")
            with self.assertRaises(ValueError):
                resolve_web_cutover_target(
                    store,
                    profile=profile,
                    provider_id="dokploy",
                    timeout_seconds=10,
                    lane=profile.lanes[0],
                    artifact_id=intent.candidate.artifact_id,
                    source_git_ref=intent.candidate.source_commit,
                    deploy_reference="",
                )
            with self.assertRaises(ValueError):
                execute_web_cutover_deployment(
                    store,
                    product=profile.product,
                    record_id="release",
                    factory=lambda _p: provider,
                    lane=profile.lanes[0],
                    artifact_id=intent.candidate.artifact_id,
                    source_git_ref=intent.candidate.source_commit,
                    deploy_reference="",
                )
            with self.assertRaises(FileNotFoundError):
                store.read_deployment_record("release")
            self.assertEqual(provider.actions, [])
