import unittest

from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)


_POLICY_ADMINISTRATOR_RULE = {
    "github_ids": [101],
    "roles": ["admin"],
    "actions": ["authz_policy_grant.write"],
    "products": ["launchplane"],
    "contexts": ["launchplane"],
}


def _human(*, github_id: int, role: str, login: str = "person") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="",
        email="",
        organizations=frozenset(),
        teams=frozenset({"example/admins"}),
        role=role,  # type: ignore[arg-type]
    )


def _allows(policy: LaunchplaneAuthzPolicy, identity: object, action: str) -> bool:
    return policy.allows(
        identity=identity,  # type: ignore[arg-type]
        action=action,
        product="any-product",
        context="any-context",
    )


class AdministratorAuthorityTests(unittest.TestCase):
    def test_person_named_administrator_by_id_may_do_anything_without_listed_actions(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [_POLICY_ADMINISTRATOR_RULE]}
        )

        self.assertTrue(
            _allows(policy, _human(github_id=101, role="admin"), "product_profile.write")
        )

    def test_administrator_role_reached_through_a_team_is_not_unrestricted(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_humans": [
                    {"teams": ["example/admins"], "roles": ["admin"], "actions": ["thing.read"]}
                ]
            }
        )
        team_admin = _human(github_id=202, role="admin")

        self.assertFalse(_allows(policy, team_admin, "product_profile.write"))

    def test_narrow_admin_role_rule_named_by_id_stays_narrow(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_humans": [
                    {
                        "github_ids": [404],
                        "roles": ["admin"],
                        "actions": ["product_environment.read"],
                        "products": ["any-product"],
                        "contexts": ["any-context"],
                    }
                ]
            }
        )
        evidence_reader = _human(github_id=404, role="admin")

        self.assertTrue(_allows(policy, evidence_reader, "product_environment.read"))
        self.assertFalse(_allows(policy, evidence_reader, "product_config.apply"))

    def test_read_only_person_and_machine_credentials_keep_their_listed_limits(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [_POLICY_ADMINISTRATOR_RULE]}
        )

        self.assertFalse(
            _allows(policy, _human(github_id=303, role="read_only"), "product_profile.write")
        )
        self.assertFalse(
            _allows(
                policy,
                TerminalAgentIdentity(subject="agent", token_label="label"),
                "product_profile.write",
            )
        )


if __name__ == "__main__":
    unittest.main()
