import unittest
from typing import cast

import click

from control_plane.dokploy import JsonValue
from control_plane.workflows.dokploy_legacy_dev_cleanup import (
    DokployRequest,
    LegacyDevCleanupRequest,
    discover_legacy_dev_targets,
    execute_legacy_dev_cleanup,
)


class DokployLegacyDevCleanupTests(unittest.TestCase):
    def test_discover_legacy_dev_targets_excludes_stable_and_pr_targets(self) -> None:
        requests: list[dict[str, object]] = []

        def _request(**kwargs: object) -> JsonValue:
            requests.append(dict(kwargs))
            path = kwargs["path"]
            query = kwargs.get("query")
            if path == "/api/project.all":
                return [
                    {
                        "name": "odoo",
                        "applications": [
                            {"name": "cm-dev", "applicationId": "app-dev"},
                            {"name": "cm-testing", "applicationId": "app-testing"},
                            {"name": "pr-28", "applicationId": "app-pr"},
                        ],
                        "compose": [
                            {"name": "cm-website-dev", "composeId": "compose-dev"},
                            {"name": "cm-prod", "composeId": "compose-prod"},
                        ],
                    }
                ]
            if path == "/api/domain.byApplicationId" and query == {"applicationId": "app-dev"}:
                return [{"domainId": "domain-app-dev"}]
            if path == "/api/domain.byComposeId" and query == {"composeId": "compose-dev"}:
                return [{"domainId": "domain-compose-dev"}]
            return []

        targets = discover_legacy_dev_targets(
            host="host", token="token", request=cast(DokployRequest, _request)
        )

        self.assertEqual([target.name for target in targets], ["cm-dev", "cm-website-dev"])
        self.assertEqual(targets[0].domain_ids, ("domain-app-dev",))
        self.assertEqual(targets[1].domain_ids, ("domain-compose-dev",))

    def test_apply_requires_exact_discovered_names_and_confirmation(self) -> None:
        def _request(**kwargs: object) -> JsonValue:
            if kwargs["path"] == "/api/project.all":
                return [
                    {
                        "name": "odoo",
                        "applications": [{"name": "cm-dev", "applicationId": "app-dev"}],
                    }
                ]
            if kwargs["path"] == "/api/domain.byApplicationId":
                return []
            return {}

        with self.assertRaises(click.ClickException):
            execute_legacy_dev_cleanup(
                host="host",
                token="token",
                cleanup_request=LegacyDevCleanupRequest(
                    apply=True,
                    target_names=("cm-dev",),
                    confirmation="wrong",
                ),
                request=cast(DokployRequest, _request),
            )

        with self.assertRaises(click.ClickException):
            execute_legacy_dev_cleanup(
                host="host",
                token="token",
                cleanup_request=LegacyDevCleanupRequest(
                    apply=True,
                    target_names=("other-dev",),
                    confirmation="delete legacy cm dev targets",
                ),
                request=cast(DokployRequest, _request),
            )

    def test_apply_deletes_domains_before_targets(self) -> None:
        calls: list[tuple[str, object]] = []

        def _request(**kwargs: object) -> JsonValue:
            calls.append((str(kwargs["path"]), kwargs.get("payload") or kwargs.get("query")))
            if kwargs["path"] == "/api/project.all":
                return [
                    {
                        "name": "odoo",
                        "applications": [{"name": "cm-dev", "applicationId": "app-dev"}],
                    }
                ]
            if kwargs["path"] == "/api/domain.byApplicationId":
                return [{"domainId": "domain-dev"}]
            return {}

        result = execute_legacy_dev_cleanup(
            host="host",
            token="token",
            cleanup_request=LegacyDevCleanupRequest(
                apply=True,
                target_names=("cm-dev",),
                confirmation="delete legacy cm dev targets",
            ),
            request=cast(DokployRequest, _request),
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual([target.name for target in result.deleted_targets], ["cm-dev"])
        self.assertIn(("/api/domain.delete", {"domainId": "domain-dev"}), calls)
        self.assertIn(("/api/application.delete", {"applicationId": "app-dev"}), calls)


if __name__ == "__main__":
    unittest.main()
