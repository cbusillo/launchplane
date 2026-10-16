"""Both Dokploy target shapes prepare only the inactive immutable slot."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.generic_web_cutover import DokployWebCutoverProvider, WebSlotDriver
from control_plane.dokploy.traefik_cutover import TraefikFileRoute, TraefikRouteTarget
from tests.test_generic_web_cutover import plan, Provider


class WebCutoverProviderTests(unittest.TestCase):
    def provider(self, root: Path, target: str) -> tuple[Any, Any, Any]:
        intent = plan(target)
        fixture = Provider(intent)
        route = Mock(spec=TraefikFileRoute)
        route.target = TraefikRouteTarget(
            route_id=intent.lane.route_id,
            host=intent.lane.host,
            route_file=root / "route.yaml",
            entrypoints=("web",),
            api_url="http://fixture.invalid",
            tls=False,
        )
        route.observed_target.side_effect = lambda: fixture.slot.private_url
        route.evidence_id.return_value = "applied"

        def publish(backend: str, **_kwargs: Any) -> str:
            fixture.slot = intent.new if backend == intent.new.private_url else intent.old
            return backend

        route.publish.side_effect = publish
        driver = WebSlotDriver(
            exclusive=fixture.exclusive,
            preflight=Mock(),
            stage_runtime=Mock(return_value=intent.lane.runtime_settings_sha256),
            readiness=fixture.readiness,
            private_health=lambda _plan, slot: fixture.current_route(
                intent
            ).public_health.model_copy(
                update={
                    "observed_runtime_identity": slot.runtime_identity,
                    "urls": (slot.private_url + "/health",),
                }
            ),
            public_health=lambda _plan: fixture.current_route(intent).public_health,
            handoff=lambda _plan, slot: {w: slot.target_id for w in intent.lane.writers},
            drain=lambda _plan, _slot: True,
        )
        return (
            DokployWebCutoverProvider(
                intent, host="https://provider.invalid", token="inert", route=route, driver=driver
            ),
            fixture,
            route,
        )

    def test_both_target_shapes_deploy_only_the_candidate_then_verify_private_and_public(
        self,
    ) -> None:
        for target in ("application", "compose"):
            with self.subTest(target=target), TemporaryDirectory() as folder:
                provider, fixture, route = self.provider(Path(folder), target)
                with patch(
                    "control_plane.dokploy.generic_web_cutover.execute_dokploy_artifact_deploy"
                ) as deploy:
                    provider.prepare_candidate(provider.plan)
                args = deploy.call_args.kwargs
                self.assertEqual(args["resolved_target"].target_id, provider.plan.new.target_id)
                self.assertEqual(args["resolved_target"].target_type, target)
                self.assertEqual(args["ship_request"].artifact_id, provider.plan.new.image)
                self.assertEqual(
                    args["ship_request"].deploy_reference, provider.plan.new.deploy_reference
                )
                self.assertEqual(args["runtime_identity"], provider.plan.new.runtime_identity)
                self.assertEqual(fixture.slot, provider.plan.old)
                provider.readiness(provider.plan, provider.plan.new)
                observed = provider.switch(provider.plan, provider.plan.new)
                self.assertEqual(
                    observed.public_health.observed_runtime_identity,
                    provider.plan.new.runtime_identity,
                )
                route.publish.assert_called_once_with(
                    provider.plan.new.private_url,
                    expected=(provider.plan.old.private_url, provider.plan.new.private_url),
                )

    def test_unread_back_managed_settings_never_start_candidate(self) -> None:
        with TemporaryDirectory() as folder:
            provider, _, _ = self.provider(Path(folder), "compose")
            provider.driver.stage_runtime.return_value = "d" * 64
            with patch(
                "control_plane.dokploy.generic_web_cutover.execute_dokploy_artifact_deploy"
            ) as deploy:
                with self.assertRaises(ValueError):
                    provider.prepare_candidate(provider.plan)
                deploy.assert_not_called()

    def test_wrong_private_digest_is_refused_before_switch(self) -> None:
        with TemporaryDirectory() as folder:
            provider, fixture, route = self.provider(Path(folder), "application")
            original = provider.driver.readiness
            object.__setattr__(
                provider.driver,
                "readiness",
                lambda p, s: original(p, s).model_copy(update={"image": p.old.image}),
            )
            with self.assertRaises(ValueError):
                provider.readiness(provider.plan, provider.plan.new)
            route.publish.assert_not_called()
