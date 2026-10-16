"""Applied provider authority, refusal and exact receipt consumption."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.dokploy.traefik_cutover import (
    TraefikFileRoute,
    TraefikRouteTarget,
    route_payload,
)
import json
from control_plane.dokploy.slot_prepare import prepare_dokploy_odoo_slot
from tests.test_odoo_cutover import IsolatedCutoverProvider


class OdooCutoverProviderTests(unittest.TestCase):
    def test_timed_out_publication_waits_for_the_already_published_owned_file(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.yaml"
            target = TraefikRouteTarget(
                route_id="lane",
                host="fixture.invalid",
                entrypoints=("web",),
                route_file=path,
                api_url="http://fixture.invalid",
                tls=False,
            )
            path.write_text(json.dumps(route_payload(target, "http://old:8069")))
            route = TraefikFileRoute(target, apply_timeout_seconds=0.005)
            with (
                patch.object(route, "observed_target", return_value="http://old:8069"),
                self.assertRaises(TimeoutError),
            ):
                route.publish("http://new:8069", expected=("http://old:8069",))
            with (
                patch.object(
                    route, "observed_target", side_effect=("http://old:8069", "http://new:8069")
                ),
                patch(
                    "control_plane.dokploy.traefik_cutover.os.replace",
                    side_effect=AssertionError("must not publish again"),
                ),
            ):
                self.assertEqual(
                    route.publish("http://new:8069", expected=("http://old:8069",)),
                    "http://new:8069",
                )

    def test_recovery_renotifies_exact_owned_intent_after_lost_file_event(self) -> None:
        import os

        with TemporaryDirectory() as temporary:
            target = TraefikRouteTarget(
                route_id="lane",
                host="fixture.invalid",
                entrypoints=("web",),
                route_file=Path(temporary) / "route.yaml",
                api_url="http://fixture.invalid",
                tls=False,
            )
            target.route_file.write_text(json.dumps(route_payload(target, "http://new:8069")))
            route = TraefikFileRoute(target, apply_timeout_seconds=0.005)
            applied = "http://old:8069"
            replace = os.replace

            def publish(source: Any, destination: Any) -> None:
                nonlocal applied
                replace(source, destination)
                applied = "http://new:8069"

            with (
                patch.object(route, "observed_target", side_effect=lambda: applied),
                patch("control_plane.dokploy.traefik_cutover.os.replace", side_effect=publish),
            ):
                self.assertEqual(
                    route.publish("http://new:8069", expected=("http://old:8069",)), applied
                )
            self.assertEqual(applied, "http://new:8069")

    def test_single_route_switch_never_deletes_another_service_in_its_file(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "route.yaml"
            target = TraefikRouteTarget(
                route_id="lane",
                host="fixture.invalid",
                entrypoints=("web",),
                route_file=path,
                api_url="http://fixture.invalid",
                tls=False,
            )
            payload = route_payload(target, "http://old:8069")
            payload["http"]["services"]["other"] = {
                "loadBalancer": {"servers": [{"url": "http://other:8069"}]}
            }
            path.write_text(json.dumps(payload))
            original = path.read_bytes()
            route = TraefikFileRoute(target)
            with (
                patch.object(route, "observed_target", return_value="http://old:8069"),
                self.assertRaisesRegex(ValueError, "single-route"),
            ):
                route.publish("http://new:8069", expected=("http://old:8069",))
            self.assertEqual(path.read_bytes(), original)

    def test_compatible_candidate_cannot_change_shared_managed_credentials(self) -> None:
        with TemporaryDirectory() as temporary:
            provider = IsolatedCutoverProvider(Path(temporary))
            self.addCleanup(provider.engine.dispose)
            with (
                patch(
                    "control_plane.dokploy.slot_prepare.api.fetch_dokploy_target_payload",
                    side_effect=[
                        {"appName": "old", "env": "INERT_FIXTURE_KEY=old"},
                        {"appName": "new", "env": "INERT_FIXTURE_KEY=new"},
                    ],
                ),
                patch("control_plane.dokploy.slot_prepare.api.dokploy_request") as write,
                self.assertRaisesRegex(ValueError, "identical managed"),
            ):
                prepare_dokploy_odoo_slot(
                    provider.cutover,
                    host="fixture.invalid",
                    token="inert",
                    data_volume="fixture",
                    private_network="fixture",
                )
            write.assert_not_called()

    def test_foreign_duplicate_or_wrong_tls_route_is_never_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "route.yaml"
            path.write_bytes(b"previous-authority")
            target = TraefikRouteTarget(
                route_id="lane",
                host="fixture.invalid",
                entrypoints=("web",),
                route_file=path,
                api_url="http://fixture.invalid",
                tls=False,
            )
            route = TraefikFileRoute(target)
            base = {
                "name": "lane@file",
                "rule": "Host(`fixture.invalid`)",
                "service": "lane",
                "status": "enabled",
                "entryPoints": ["web"],
                "middlewares": [],
            }
            foreign = {**base, "name": "other@docker"}
            for routers in (
                [base, foreign],
                [foreign],
                [{**base, "tls": {}}],
                [{**base, "rule": "Host(`fixture.invalid`) || PathPrefix(`/`)"}],
            ):
                with (
                    self.subTest(routers=routers),
                    patch.object(
                        route,
                        "_api",
                        return_value={"routers": {router["name"]: router for router in routers}},
                    ),
                    self.assertRaises(ValueError),
                ):
                    route.publish("http://new:8069", expected=("http://old:8069",))
                self.assertEqual(path.read_bytes(), b"previous-authority")
            with (
                patch.object(
                    route,
                    "_api",
                    side_effect=[
                        {"routers": {"lane@file": base}},
                        {
                            "status": "enabled",
                            "loadBalancer": {"servers": [{"url": "http://foreign:8069"}]},
                        },
                    ],
                ),
                self.assertRaisesRegex(ValueError, "authority changed"),
            ):
                route.publish("http://new:8069", expected=("http://old:8069",))
            self.assertEqual(path.read_bytes(), b"previous-authority")

    def test_candidate_preparation_deploys_only_distinct_private_project(self) -> None:
        with TemporaryDirectory() as temporary:
            provider = IsolatedCutoverProvider(Path(temporary))
            self.addCleanup(provider.engine.dispose)
            plan = provider.cutover
            old: dict[str, Any] = {
                "appName": "old-project",
                "composeFile": "old-service",
                "domains": [{"host": "fixture.invalid"}],
            }
            candidate: dict[str, Any] = {"appName": "candidate-project", "domains": []}
            applied = dict(candidate)
            calls: list[dict[str, Any]] = []

            def read(**kwargs: object) -> dict[str, object]:
                return old if kwargs["target_id"] == plan.old.compose_id else applied

            def update(**kwargs: object) -> None:
                calls.append(kwargs)
                payload = kwargs["payload"]
                assert isinstance(payload, dict)
                applied.update(payload)

            with (
                patch(
                    "control_plane.dokploy.slot_prepare.api.fetch_dokploy_target_payload",
                    side_effect=read,
                ),
                patch(
                    "control_plane.dokploy.slot_prepare.api.deployment_for_target_by_title",
                    return_value=None,
                ),
                patch(
                    "control_plane.dokploy.slot_prepare.api.list_deployments_for_target",
                    return_value=(),
                ),
                patch("control_plane.dokploy.slot_prepare.api.dokploy_request", side_effect=update),
                patch("control_plane.dokploy.slot_prepare.api.trigger_deployment") as deploy,
                patch(
                    "control_plane.dokploy.slot_prepare.api.wait_for_titled_or_sole_new_target_deployment"
                ),
            ):
                prepare_dokploy_odoo_slot(
                    plan,
                    host="fixture.invalid",
                    token="inert-fixture-token",
                    data_volume="fixture-data",
                    private_network="fixture-private",
                )
            self.assertEqual(deploy.call_args.kwargs["target_id"], plan.new.compose_id)
            self.assertEqual(old["composeFile"], "old-service")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["payload"]["composeId"], plan.new.compose_id)


class AppliedOldIntentRecoveryTests(unittest.TestCase):
    def test_inverse_publication_cancels_a_delayed_candidate_file(self) -> None:
        with TemporaryDirectory() as temporary:
            target = TraefikRouteTarget(
                route_id="lane",
                host="fixture.invalid",
                entrypoints=("web",),
                route_file=Path(temporary) / "route.yaml",
                api_url="http://fixture.invalid",
                tls=False,
            )
            target.route_file.write_text(json.dumps(route_payload(target, "http://new:8069")))
            route = TraefikFileRoute(target)
            applied = "http://old:8069"
            with patch.object(route, "observed_target", side_effect=lambda: applied):
                route.publish("http://old:8069", expected=("http://old:8069", "http://new:8069"))
            # A later provider start consumes durable intent, rather than the
            # route which happened to be applied during the inverse operation.
            applied = json.loads(target.route_file.read_text())["http"]["services"]["lane"][
                "loadBalancer"
            ]["servers"][0]["url"]
            self.assertEqual(applied, "http://old:8069")
