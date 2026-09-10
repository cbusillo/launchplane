from __future__ import annotations

import unittest

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_qualification import OrdinaryAgentQualificationSetup
from control_plane.contracts.ordinary_agent_qualification import OrdinaryRepositoryAdminObservation
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderEvidenceError
from control_plane.ordinary_agent_qualification import observe_repository_administrator


class _Transport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []
        self.rest_core_requests = 0
        self.graphql_requests = 0
        self.graphql_points = 0

    def request(self, *, method: str, path: str, body: object = None) -> object:
        self.calls.append((method, path))
        self.rest_core_requests += 1
        return self.payload


class OrdinaryAgentQualificationObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.setup = OrdinaryAgentQualificationSetup(
            source_activation_operation_id="activation-1",
            source_activation_binding_sha256="a" * 64,
            target=OrdinaryAgentTarget(
                repository_id=1, repository="owner/repository", base_branch="main"
            ),
            managed_set_id="ordinary-agent.qualification",
            managed_rule_id="agent.qualification.main",
            administrator_github_id=42,
            administrator_login="Admin-User",
            administrator_login_normalized="admin-user",
            attestation_expires_at=200,
        )

    def observe(self, payload: object) -> OrdinaryRepositoryAdminObservation:
        transport = _Transport(payload)
        result = observe_repository_administrator(
            transport=transport, setup=self.setup, observed_at=100
        )
        self.assertEqual(
            transport.calls,
            [("GET", "/repos/owner/repository/collaborators?permission=admin&per_page=100&page=1")],
        )
        return result

    def test_exact_id_casefolded_login_and_literal_admin_qualifies(self) -> None:
        result = self.observe([{"id": 42, "login": "ADMIN-user", "permissions": {"admin": True}}])
        self.assertEqual(result.status, "qualified")
        assert result.observed is not None
        self.assertEqual(result.observed.github_id, 42)

    def test_numeric_id_login_change_wins_over_conflicting_name(self) -> None:
        result = self.observe(
            [
                {"id": 42, "login": "renamed", "permissions": {"admin": True}},
                {"id": 77, "login": "admin-user", "permissions": {"admin": True}},
            ]
        )
        self.assertEqual(result.status, "administrator_login_changed")
        assert result.observed is not None
        self.assertEqual(result.observed.github_id, 42)

    def test_short_page_absence_is_terminal_and_full_page_is_inconclusive(self) -> None:
        absent = self.observe([{"id": 4, "login": "other", "permissions": {"admin": False}}])
        self.assertEqual(absent.status, "administrator_not_admin")
        full = [
            {"id": index + 100, "login": f"user-{index}", "permissions": {"admin": True}}
            for index in range(100)
        ]
        self.assertEqual(self.observe(full).status, "inconclusive_truncated_page")

    def test_full_page_login_collision_is_inconclusive_without_expected_id(self) -> None:
        full = [
            {"id": index + 100, "login": f"user-{index}", "permissions": {"admin": True}}
            for index in range(99)
        ]
        full.append({"id": 99, "login": "ADMIN-user", "permissions": {"admin": True}})
        result = self.observe(full)
        self.assertEqual(result.status, "inconclusive_truncated_page")
        self.assertIsNone(result.observed)

    def test_malformed_item_is_incomplete_not_negative(self) -> None:
        with self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "provider_incomplete"):
            self.observe([{"id": 42, "login": "Admin-User", "permissions": {"admin": 1}}])

    def test_whitespace_login_is_incomplete_not_negative(self) -> None:
        with self.assertRaisesRegex(OrdinaryAgentProviderEvidenceError, "provider_incomplete"):
            self.observe([{"id": 42, "login": "   ", "permissions": {"admin": True}}])
