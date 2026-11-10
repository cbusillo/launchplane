from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from control_plane import testing_copy_route as maintenance
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
from control_plane.dokploy.testing_route import (
    observe_testing_route,
    route_document,
    route_document_content,
)
from tests import test_testing_route_effects as effects_tests


class TestingCopyRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = effects_tests.TestingRouteFenceControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.native = self.fixture.fixture
        self.before = self.native.plan
        self.native.raw = self.native.fixture.apply(
            self.before, self.before.new_backend, retire_old=True
        )
        original_content = route_document_content(self.before, self.before.new_backend)
        assert original_content is not None
        self.native.route_file.write_text(original_content)
        self.native.client.payload["old_project"] = self.native.candidate_project
        self.native.client.old_runtime_identity = self.native.new_identity
        self.native.containers["new"]["Config"]["Env"].append("ODOO_WORKERS=6")
        candidate = SimpleNamespace(
            compose_id="target", namespace="namespace", server_id="testing-server"
        )
        publication = SimpleNamespace(plan=SimpleNamespace(candidate=candidate))
        for target, name, value in (
            (maintenance, "published_testing_copy", publication),
            (maintenance, "effective_testing_route", self.before),
            (source, "read_dokploy_config", ("https://provider.invalid", "fixture")),
            (
                api,
                "fetch_dokploy_target_payload",
                {
                    "composeId": candidate.compose_id,
                    "name": candidate.namespace,
                    "serverId": candidate.server_id,
                    "appName": self.native.candidate_project,
                },
            ),
            (maintenance, "DokployTestingRouteTransport", self.native.client),
        ):
            self.enterContext(patch.object(target, name, return_value=value))
        self.checkpoint = Mock()

    def run_route(self) -> None:
        maintenance.publish_testing_copy_route(
            root=self.native.root,
            store=self.fixture.store,
            identity=self.native.new_identity,
            checkpoint=self.checkpoint,
            operation_id="stable-operation",
            lease_owner="stable-worker",
        )

    def test_same_copy_publication_and_replay_preserve_the_http_authority(self) -> None:
        self.run_route()
        self.assertEqual(
            observe_testing_route(
                self.before, self.native.raw, json.loads(self.native.route_file.read_text())
            ),
            self.before.new_backend,
        )
        document = json.loads(self.native.route_file.read_text())
        self.assertEqual(
            document["http"]["services"][self.before.route_id],
            route_document(self.before, self.before.new_backend)["http"]["services"][
                self.before.route_id
            ],
        )
        effects = self.native.remote_modes.count("publish_v2")
        self.run_route()
        self.assertEqual(self.native.remote_modes.count("publish_v2"), effects)
        self.assertTrue(document["http"]["services"].get(self.before.route_id + "-websocket"))

    def test_worker_mode_drift_refuses_before_the_writer(self) -> None:
        self.native.before_publish = lambda: self.native.containers["new"]["Config"][
            "Env"
        ].__setitem__(-1, "ODOO_WORKERS=0")
        with self.assertRaisesRegex(ValueError, "worker mode changed"):
            self.run_route()
        self.assertNotIn("-websocket", self.native.route_file.read_text())

    def test_lost_reply_is_observed_by_a_fresh_invocation_without_another_writer(self) -> None:
        self.native.before_publish = lambda: setattr(self.native, "lose_writer_reply", True)
        with self.assertRaises(NativeSourceUnavailable):
            self.run_route()
        effects = self.native.remote_modes.count("publish_v2")
        self.native.before_publish = None
        self.native.lose_writer_reply = False
        self.run_route()
        self.assertEqual(self.native.remote_modes.count("publish_v2"), effects)

    def test_unrelated_invalid_worker_mode_does_not_block_this_copy(self) -> None:
        self.native.containers["old"]["Config"]["Env"].append("ODOO_WORKERS=invalid")
        self.run_route()
        self.assertIn("-websocket", self.native.route_file.read_text())

    def test_threaded_copy_keeps_its_http_route_without_a_provider_write(self) -> None:
        self.native.containers["new"]["Config"]["Env"][-1] = "ODOO_WORKERS=0"
        original = self.native.route_file.read_text()
        self.run_route()
        self.assertEqual(self.native.route_file.read_text(), original)
        self.assertEqual(self.native.effect_count, 0)
