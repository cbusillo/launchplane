import base64
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
        self.assertEqual(guard.call_count, 2)
        sent.assert_called_once()
        guard.side_effect = ValueError("lease lost")
        with patch("control_plane.dokploy.cutover_transport._read_remote") as remote_call:
            with self.assertRaises(ValueError):
                transport(("docker", "stop", "fixture"), None)
        remote_call.assert_not_called()

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
