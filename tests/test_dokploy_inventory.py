import copy
import json
import unittest
from typing import Any
from unittest.mock import patch

import click

from control_plane.dokploy_inventory import (
    InventoryChanged,
    InventoryUnavailable,
    TARGET_COLLECTIONS,
    read_dokploy_inventory,
)
from control_plane.http_app import create_launchplane_fastapi_app
from tests.http_app_test_support import (
    _local_operator_bearer_config,
    _MissingProductReadStore,
    _RejectingVerifier,
)
from tests.support.auth import local_operator_policy, scoped_agent_test
from tests.support.http import get


def environment(environment_id: str, name: str) -> dict[str, Any]:
    return {
        "environmentId": environment_id,
        "name": name,
        **{key: [] for key in TARGET_COLLECTIONS},
    }


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = environment("env-untracked", "Preview grouping")
        self.environment["compose"] = [{"composeId": "self-host"}]
        self.environment["postgres"] = [{"postgresId": "database-only"}]
        self.projects: list[dict[str, Any]] = [
            {"projectId": "p-empty", "name": "Empty project", "environments": []},
            {
                "projectId": "p-untracked",
                "name": "Untracked project",
                "environments": [self.environment, environment("env-empty", "Empty environment")],
            },
        ]
        self.calls: list[str] = []

    def provider(self, **kwargs: Any) -> Any:
        self.assertEqual(kwargs["method"], "GET")
        self.assertNotIn("payload", kwargs)
        path = kwargs["path"]
        self.calls.append(path)
        sensitive = {
            "env": "PASSWORD=never-output",
            "composeFile": "never-output",
            "token": "never-output",
        }
        if path == "/api/project.all":
            return copy.deepcopy(self.projects)
        if path == "/api/compose.one":
            return {
                "composeId": "self-host",
                "environmentId": "env-untracked",
                "name": "Control plane",
                "serverId": None,
                **sensitive,
            }
        if path == "/api/postgres.one":
            return {
                "postgresId": "database-only",
                "environmentId": "env-untracked",
                "name": "Database",
                "serverId": "guest-1",
                **sensitive,
            }
        if path == "/api/server.one":
            return {
                "serverId": "guest-1",
                "name": "apps-example-testing",
                "sshKey": "never-output",
                "ipAddress": "never-output",
            }
        raise AssertionError(path)

    def read(self, **kwargs: Any) -> Any:
        with patch(
            "control_plane.dokploy_inventory.api.dokploy_request", side_effect=self.provider
        ):
            return read_dokploy_inventory(
                host="https://provider.invalid",
                token="opaque",
                offset=kwargs.get("offset", 0),
                limit=kwargs.get("limit", 25),
                inventory_digest=kwargs.get("inventory_digest", ""),
            )

    def test_pages_include_empty_untracked_and_database_only_placements(self) -> None:
        first = self.read(limit=1)
        self.assertEqual([project.name for project in first.projects], ["Empty project"])
        self.assertEqual(self.calls, ["/api/project.all"])
        second = self.read(
            offset=first.next_offset, limit=1, inventory_digest=first.inventory_digest
        )
        self.assertIsNone(second.next_offset)
        self.assertEqual(second.total_projects, len(first.projects + second.projects))
        self.assertEqual(second.total_environments, len(second.projects[0].environments))
        targets = second.projects[0].environments[1].targets
        self.assertEqual(targets[0].placement.kind, "dokploy_host")
        self.assertIsNone(targets[0].placement.server_name)
        self.assertEqual(targets[1].placement.server_name, "apps-example-testing")
        serialized = second.model_dump_json()
        for secret in ("never-output", "composeFile", "ipAddress"):
            self.assertNotIn(secret, serialized)

    def test_empty_provider_is_complete(self) -> None:
        self.projects = []
        page = self.read()
        self.assertEqual(page.projects, [])
        self.assertEqual(page.total_targets, 0)
        self.assertIsNone(page.next_offset)

    def test_all_provider_categories_hydrate_and_share_server_read(self) -> None:
        all_targets = environment("env-all", "All categories")
        expected = []
        for collection, (category, id_field) in TARGET_COLLECTIONS.items():
            all_targets[collection] = [{id_field: category + "-id"}]
            expected.append((category, category + "-id", category + " display"))
        self.projects = [{"projectId": "all", "name": "All targets", "environments": [all_targets]}]

        def provider(**kwargs: Any) -> Any:
            path = kwargs["path"]
            self.assertEqual(kwargs["method"], "GET")
            if path == "/api/project.all" or path == "/api/server.one":
                return self.provider(**kwargs)
            category = path.removeprefix("/api/").removesuffix(".one")
            id_field, target_id = next(iter(kwargs["query"].items()))
            return {
                id_field: target_id,
                "environmentId": "env-all",
                "name": category + " display",
                "serverId": "guest-1",
                "password": "never-output",
            }

        with patch("control_plane.dokploy_inventory.api.dokploy_request", side_effect=provider):
            page = read_dokploy_inventory(host="host", token="opaque", offset=0, limit=25)
        targets = page.projects[0].environments[0].targets
        self.assertCountEqual(
            [(target.category, target.target_id, target.name) for target in targets], expected
        )
        self.assertEqual(self.calls.count("/api/server.one"), 1)
        self.assertNotIn("never-output", page.model_dump_json())

    def test_budget_exhaustion_and_target_movement_return_no_page(self) -> None:
        with patch("control_plane.dokploy_inventory.time.monotonic", side_effect=[0, 1000000]):
            with self.assertRaises(InventoryUnavailable):
                self.read()
        provider = self.provider

        def moved(**kwargs: Any) -> Any:
            detail = provider(**kwargs)
            if kwargs["path"] == "/api/compose.one":
                detail["environmentId"] = "another-environment"
            return detail

        with patch("control_plane.dokploy_inventory.api.dokploy_request", side_effect=moved):
            with self.assertRaises(InventoryUnavailable):
                read_dokploy_inventory(host="host", token="opaque", offset=0, limit=25)

    def test_changed_snapshot_rejects_continuation(self) -> None:
        first = self.read(limit=1)
        self.projects[0]["name"] = "Renamed"
        with self.assertRaises(InventoryChanged):
            self.read(offset=1, inventory_digest=first.inventory_digest)
        with self.assertRaises(ValueError):
            self.read(offset=1)

    def test_incomplete_or_duplicate_inventory_is_never_empty_success(self) -> None:
        original = copy.deepcopy(self.projects)
        for mutate in (
            lambda: self.environment.pop("redis"),
            lambda: self.projects.append(copy.deepcopy(self.projects[0])),
            lambda: self.environment["compose"].append({"composeId": "self-host"}),
        ):
            self.projects = copy.deepcopy(original)
            self.environment = self.projects[1]["environments"][0]
            mutate()
            with self.assertRaises(InventoryUnavailable):
                self.read()

    def test_bounds_and_missing_placement_fail_closed(self) -> None:
        with patch("control_plane.dokploy_inventory.MAX_PROJECTS", 1):
            with self.assertRaises(InventoryUnavailable):
                self.read()
        with patch("control_plane.dokploy_inventory.MAX_PAGE_TARGETS", 1):
            with self.assertRaises(InventoryUnavailable):
                self.read()
        provider = self.provider

        def incomplete(**kwargs: Any) -> Any:
            detail = provider(**kwargs)
            if kwargs["path"] == "/api/compose.one":
                del detail["serverId"]
            return detail

        with patch("control_plane.dokploy_inventory.api.dokploy_request", side_effect=incomplete):
            with self.assertRaises(InventoryUnavailable):
                read_dokploy_inventory(host="host", token="opaque", offset=0, limit=25)


class InventoryHttpTests(unittest.IsolatedAsyncioTestCase):
    def app(self, action: str = "dokploy_target.inspect") -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=local_operator_policy(
                actions=(action,), contexts=("launchplane",), token_label="local-owner-read"
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

    async def request(
        self, app: Any, query: str = "", authorization: str = "Bearer local-operator-token"
    ) -> Any:
        return await get(
            app, "/v1/dokploy/inventory" + query, headers={"Authorization": authorization}
        )

    async def test_authenticated_read_and_validation(self) -> None:
        app = self.app()
        with (
            patch(
                "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                return_value=("host", "opaque"),
            ),
            patch(
                "control_plane.dokploy_inventory.api.dokploy_request", return_value=[]
            ) as provider,
        ):
            response = await self.request(app)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["inventory"]["projects"], [])
            self.assertTrue(response.json()["trace_id"].startswith("launchplane_req_"))
            self.assertEqual((await self.request(app, "?limit=101")).status_code, 400)
            self.assertEqual(provider.call_count, 1)

    @scoped_agent_test
    async def test_scoped_driver_read_cannot_enumerate_and_anonymous_cannot_read(self) -> None:
        with patch(
            "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config"
        ) as config:
            self.assertEqual((await self.request(self.app("driver.read"))).status_code, 403)
            self.assertEqual((await self.request(self.app(), authorization="")).status_code, 401)
            config.assert_not_called()

    async def test_provider_errors_are_value_free_and_never_partial(self) -> None:
        with (
            patch(
                "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                return_value=("host", "opaque"),
            ),
            patch(
                "control_plane.dokploy_inventory.api.dokploy_request",
                side_effect=click.ClickException("PASSWORD=never-output"),
            ),
        ):
            response = await self.request(self.app())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "dokploy_inventory_unavailable")
        self.assertNotIn("never-output", json.dumps(response.json()))
        self.assertNotIn("inventory", response.json())

    async def test_continuation_requires_digest_and_changed_inventory_is_conflict(self) -> None:
        app = self.app()
        with (
            patch(
                "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                return_value=("host", "opaque"),
            ),
            patch("control_plane.dokploy_inventory.api.dokploy_request", return_value=[]),
        ):
            self.assertEqual((await self.request(app, "?offset=1")).status_code, 400)
            response = await self.request(app, "?inventory_digest=" + "0" * 64)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "dokploy_inventory_changed")
