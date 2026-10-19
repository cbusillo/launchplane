"""Isolated creation records custody without adopting or rerouting a lane."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from control_plane.dokploy import api, sandbox_placement, source


class IsolatedPlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.namespace = "lp-testing-" + uuid4().hex
        self.identities: dict[str, str] = {}
        self.effects: list[str] = []
        self.created: list[dict[str, object]] = []
        self.inventory: tuple[str, str, str] = ("", "", "")
        self.guard = Mock()
        self.enterContext(
            patch.object(source, "read_dokploy_config", return_value=("fixture.invalid", "fixture"))
        )
        self.enterContext(
            patch.object(
                sandbox_placement, "created_placement", side_effect=lambda *_: self.inventory
            )
        )
        self.request = self.enterContext(
            patch.object(api, "dokploy_request", side_effect=self.provider)
        )

    def provider(self, **kwargs: object) -> dict[str, str]:
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        self.created.append(payload)
        if "composeType" in payload:
            self.assertEqual(payload["environmentId"], self.identities["environment_id"])
            self.inventory = (*self.inventory[:2], "new-compose")
            return {"composeId": self.inventory[2]}
        if "projectId" in payload:
            self.assertEqual(payload["projectId"], self.identities["project_id"])
            self.inventory = (self.inventory[0], "new-environment", "")
            return {"environmentId": self.inventory[1]}
        self.inventory = ("new-project", "", "")
        return {"projectId": self.inventory[0]}

    def create(self) -> None:
        sandbox_placement.create_isolated_placement(
            Path("."),
            namespace=self.namespace,
            server_id="selected-testing-server",
            description="Testing copy",
            checkpoint=self.guard,
            before_create=self.effects.append,
            record_identity=self.identities.__setitem__,
        )

    def test_children_use_recorded_parent_custody_and_no_lane_is_adopted(self) -> None:
        self.create()
        self.assertEqual(tuple(self.identities.values()), self.inventory)
        self.assertEqual(self.created[-1]["appName"], self.namespace)
        self.assertEqual(self.created[-1]["serverId"], "selected-testing-server")
        self.assertTrue(all(p["name"] == self.namespace for p in self.created))
        self.assertEqual(set(self.effects), set(self.identities))
        # This boundary only creates placement. It must never install a route,
        # source, credential or start an application before storage is qualified.
        self.assertTrue(
            all("env" not in p and "composeFile" not in p and "host" not in p for p in self.created)
        )

    def test_nested_provider_create_receipts_keep_parent_custody(self) -> None:
        provider = self.provider

        def wrapped(**kwargs: object) -> dict[str, object]:
            response = provider(**kwargs)
            key = next(iter(response))
            return {key.removesuffix("Id"): response}

        self.request.side_effect = wrapped
        self.create()
        self.assertEqual(tuple(self.identities.values()), self.inventory)
        self.assertEqual(self.created[-1]["environmentId"], self.identities["environment_id"])

    def test_uncertain_create_cannot_be_repeated_as_an_empty_new_placement(self) -> None:
        real_provider = self.provider

        def lost_response(**kwargs: object) -> dict[str, str]:
            real_provider(**kwargs)
            raise TimeoutError("lost create reply")

        self.request.side_effect = lost_response
        with self.assertRaises(TimeoutError):
            self.create()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.request.call_count, 1)
        self.assertFalse(self.identities)

    def test_recording_failure_keeps_partial_placement_and_does_not_create_children(self) -> None:
        with patch.object(
            self, "identities", Mock(__setitem__=Mock(side_effect=OSError("DB unavailable")))
        ):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(len(self.created), 1)
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(len(self.created), 1)

    def test_lost_guard_stops_before_next_effect(self) -> None:
        self.guard.side_effect = (None, None, PermissionError("lane lease lost"))
        with self.assertRaisesRegex(PermissionError, "lease lost"):
            self.create()
        self.assertEqual(len(self.created), 1)
        self.assertEqual(set(self.identities), {"project_id"})

    def test_provider_must_return_a_real_identifier_before_children_can_be_created(self) -> None:
        responses: tuple[dict[str, object], ...] = ({}, {"projectId": {}}, {"projectId": " "})
        for response in responses:
            with self.subTest(response=response):
                self.request.return_value = response
                self.request.side_effect = None
                self.request.reset_mock()
                with self.assertRaises(ValueError):
                    self.create()
                self.assertEqual(self.request.call_count, 1)
                self.assertFalse(self.identities)

    def test_arbitrary_namespace_is_refused_before_provider_or_authority_reads(self) -> None:
        self.namespace = "retained-testing"
        with self.assertRaises(ValueError):
            self.create()
        self.request.assert_not_called()
        self.guard.assert_not_called()
