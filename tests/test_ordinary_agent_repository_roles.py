"""One observation batches current positive admin evidence without cross-read cache."""

import unittest

from control_plane.ordinary_agent_repository_roles import OrdinaryRepositoryAdminObservation
from control_plane.merge_train_github import MergeTrainGitHubError


class OrdinaryRepositoryRoleTests(unittest.TestCase):
    def test_roles_bind_numeric_identity_and_login_and_share_one_read(self) -> None:
        calls: list[str] = []
        payload = [
            {"id": 10, "login": "Admin", "permissions": {"admin": True}},
            {
                "id": 11,
                "login": "Maintainer",
                "permissions": {"admin": False},
                "role_name": "maintain",
            },
            {
                "id": 12,
                "login": "Custom",
                "permissions": {"admin": False},
                "role_name": "Admin-lite",
            },
            {"id": 13, "login": "Writer", "permissions": {"admin": False}},
            {"id": 14, "login": "Malformed", "permissions": {"admin": "true"}},
            {"id": True, "login": "Malformed", "permissions": {"admin": True}},
            {"id": 15, "login": "Incomplete"},
        ]

        def request(path: str) -> object:
            calls.append(path)
            return payload

        observation = OrdinaryRepositoryAdminObservation(
            request=request, repository_path="example/repo"
        )
        self.assertEqual(calls, [])
        self.assertTrue(observation.is_admin(10, "ADMIN"))
        self.assertFalse(observation.is_admin(99, "Admin"))
        self.assertFalse(observation.is_admin(10, "Another"))
        for identity, login in (
            (11, "Maintainer"),
            (12, "Custom"),
            (13, "Writer"),
            (14, "Malformed"),
            (15, "Incomplete"),
        ):
            self.assertFalse(observation.is_admin(identity, login))
        self.assertTrue(observation.is_admin(10, "Admin"))
        self.assertEqual(
            calls, ["/repos/example/repo/collaborators?permission=admin&per_page=100&page=1"]
        )
        payload[0]["permissions"] = {"admin": False}
        current = OrdinaryRepositoryAdminObservation(
            request=request, repository_path="example/repo"
        )
        self.assertFalse(current.is_admin(10, "Admin"))
        self.assertEqual(len(calls), 2)

    def test_full_page_only_proves_present_admins_and_never_follows_pagination(self) -> None:
        calls: list[str] = []

        def request(path: str) -> object:
            calls.append(path)
            return [
                {"id": i + 1, "login": f"user-{i}", "permissions": {"admin": True}}
                for i in range(100)
            ]

        observation = OrdinaryRepositoryAdminObservation(
            request=request, repository_path="example/repo"
        )
        self.assertTrue(observation.is_admin(100, "user-99"))
        self.assertFalse(observation.is_admin(101, "user-100"))
        self.assertEqual(len(calls), 1)

    def test_failed_observation_is_not_retried_for_another_author(self) -> None:
        for failure in (
            MergeTrainGitHubError("provider denied", status_code=403),
            MergeTrainGitHubError("repository invisible", status_code=404),
            None,
        ):
            with self.subTest(failure=failure):
                calls: list[str] = []

                def request(path: str) -> object:
                    calls.append(path)
                    if failure is not None:
                        raise failure
                    return {"malformed": "response"}

                observation = OrdinaryRepositoryAdminObservation(
                    request=request, repository_path="example/repo"
                )
                for actor in (10, 11):
                    with self.assertRaises(Exception) as raised:
                        observation.is_admin(actor, "admin")
                    if failure is not None:
                        self.assertIs(raised.exception, failure)
                self.assertEqual(len(calls), 1)
