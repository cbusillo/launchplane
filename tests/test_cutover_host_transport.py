import base64
from collections.abc import Callable
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_transport import CutoverHostTransport
from control_plane.dokploy.cutover_execution import (
    bind_cutover_execution,
    require_cutover_execution,
)
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan
from pathlib import Path
from tempfile import TemporaryDirectory


class CutoverHostTransportTests(unittest.TestCase):
    def transport(self, guard: Mock) -> CutoverHostTransport:
        return CutoverHostTransport(
            host="https://provider.invalid", token="inert", server_id="registered", guard=guard
        )

    def test_partial_or_reordered_terminal_output_never_returns_a_result(self) -> None:
        for frames in (
            [{"type": "stdout", "offset": 0, "data": base64.b64encode(b"partial").decode()}],
            [{"type": "stdout", "offset": 1, "data": "YQ=="}],
            [{"type": "complete", "size": 8}],
        ):
            with self.subTest(frames=frames):
                transport = self.transport(Mock())

                def receive(**kwargs: object) -> None:
                    callback = kwargs["receive"]
                    assert callable(callback)
                    for frame in frames:
                        callback(frame)

                with patch(
                    "control_plane.dokploy.cutover_transport._read_remote", side_effect=receive
                ):
                    with self.assertRaises(RuntimeError):
                        transport(("docker", "inspect", "fixture"), None)

    def test_mutation_checks_guard_at_call_and_at_the_actual_send(self) -> None:
        guard = Mock()
        transport = self.transport(guard)
        sent = Mock()

        def remote(**kwargs: object) -> None:
            dispatch = kwargs["dispatch_guard"]
            callback = kwargs["receive"]
            assert callable(dispatch) and callable(callback)
            dispatch(sent)
            callback({"type": "stdout", "offset": 0, "data": "e30="})
            callback({"type": "complete", "size": 2})

        with patch("control_plane.dokploy.cutover_transport._read_remote", side_effect=remote):
            self.assertEqual(transport(("docker", "stop", "fixture"), None), "{}")
        sent.assert_called_once()
        sent.reset_mock()
        guard.side_effect = ValueError("lease lost")
        with patch("control_plane.dokploy.cutover_transport._read_remote", side_effect=remote):
            with self.assertRaises(ValueError):
                transport(("docker", "stop", "fixture"), None)
        sent.assert_not_called()

    def test_oversize_payload_refuses_before_any_remote_dispatch(self) -> None:
        with patch("control_plane.dokploy.cutover_transport._read_remote") as remote:
            with self.assertRaises(RuntimeError):
                self.transport(Mock())(("docker", "exec", "fixture"), "x" * 120_000)
        remote.assert_not_called()

    def test_execution_cannot_cross_original_plan_or_outlive_the_accepted_call(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            checkpoint = Mock()
            with self.assertRaises(ValueError):
                require_cutover_execution(plan)
            with bind_cutover_execution(plan, checkpoint):
                require_cutover_execution(plan)
                with self.assertRaises(ValueError):
                    require_cutover_execution(plan.model_copy(update={"release_id": "foreign"}))
            checkpoint.assert_called_once()
            with self.assertRaises(ValueError):
                require_cutover_execution(plan)

    def test_provider_observer_cannot_dispatch_after_the_original_guard_changes(self) -> None:
        from control_plane.dokploy import api

        guard = Mock()

        def observe(
            host: str, path: str, payload: api.JsonObject, send: Callable[[], api.JsonValue]
        ) -> api.JsonValue:
            guard.side_effect = ValueError("original lease lost during provider preparation")
            return send()

        with patch.object(api, "_dokploy_request", return_value={}) as dispatch:
            with api.observe_dokploy_mutations(observe), api.guard_dokploy_mutations(guard):
                with self.assertRaises(ValueError):
                    api.dokploy_request(
                        host="https://provider.invalid",
                        token="inert",
                        path="/api/compose.update",
                        method="POST",
                        payload={"composeId": "original"},
                    )
            dispatch.assert_not_called()
            api.dokploy_request(
                host="https://provider.invalid", token="inert", path="/api/compose.one"
            )
            dispatch.assert_called_once()

    def test_local_route_publication_rechecks_its_bound_lane_guard(self) -> None:
        from control_plane.dokploy.cutover_host import GuardedFileRoute
        from control_plane.dokploy.traefik_cutover import TraefikFileRoute, TraefikRouteTarget

        with TemporaryDirectory() as temporary:
            target = TraefikRouteTarget(
                route_id="fixture",
                host="fixture.invalid",
                route_file=Path(temporary) / "route.yaml",
                api_url="http://proxy.invalid",
                entrypoints=("web",),
                tls=False,
            )
            guard = Mock(side_effect=ValueError("bound lane authority changed"))
            route = GuardedFileRoute(target, guard)
            with (
                patch.object(TraefikFileRoute, "publish") as publish,
                self.assertRaises(ValueError),
            ):
                route.publish("http://candidate.invalid", expected=("http://serving.invalid",))
            publish.assert_not_called()
