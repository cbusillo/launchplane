import unittest
from unittest.mock import patch

import click

from control_plane.workflows.dokploy_deploy import (
    _canonical_registry_host,
    _docker_image_registry_host,
    update_dokploy_target_artifact,
)


class DokployDeployRegistryTests(unittest.TestCase):
    def test_registry_host_normalization_matches_docker_image_rules(self) -> None:
        self.assertEqual(_docker_image_registry_host("ubuntu:24.04"), "docker.io")
        self.assertEqual(_docker_image_registry_host("library/postgres:16"), "docker.io")
        self.assertEqual(
            _docker_image_registry_host("ghcr.io/every/example@sha256:abc"),
            "ghcr.io",
        )
        self.assertEqual(
            _docker_image_registry_host("localhost:5000/every/example:latest"),
            "localhost:5000",
        )
        self.assertEqual(_canonical_registry_host("https://USER@GHCR.IO/v2/"), "ghcr.io")
        self.assertEqual(_canonical_registry_host("registry-1.docker.io"), "docker.io")

    def test_saved_matching_registry_logs_in_before_provider_update(self) -> None:
        requests: list[dict[str, object]] = []

        def request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/registry.one":
                return {
                    "registryId": "registry-123",
                    "registryUrl": "https://ghcr.io",
                    "username": "every",
                }
            return True

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "dockerImage": "ghcr.io/every/example:old",
                    "username": None,
                    "password": None,
                    "registryUrl": None,
                    "registryId": "registry-123",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.dokploy_request",
                side_effect=request,
            ),
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/every/example:new",
            )

        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/registry.one",
                "/api/registry.testRegistryById",
                "/api/application.saveDockerProvider",
            ],
        )
        self.assertEqual(
            requests[1]["payload"],
            {"registryId": "registry-123", "serverId": "server-123"},
        )

    def test_saved_mismatched_registry_blocks_provider_update(self) -> None:
        requests: list[dict[str, object]] = []

        def request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/registry.one":
                return {
                    "registryId": "registry-123",
                    "registryUrl": "registry.example.com",
                }
            return True

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "registryId": "registry-123",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.dokploy_request",
                side_effect=request,
            ),
            self.assertRaisesRegex(
                click.ClickException,
                "saved registry does not match",
            ),
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/every/example:new",
            )

        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/registry.one"],
        )

    def test_saved_registry_login_failure_blocks_provider_update(self) -> None:
        requests: list[dict[str, object]] = []

        def request(**kwargs: object) -> object:
            requests.append(kwargs)
            if kwargs["path"] == "/api/registry.one":
                return {"registryId": "registry-123", "registryUrl": "ghcr.io"}
            raise click.ClickException("Registry login failed with provider-secret.")

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "registryId": "registry-123",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.dokploy_request",
                side_effect=request,
            ),
            self.assertRaisesRegex(
                click.ClickException,
                "Dokploy saved registry login failed for the target server",
            ) as error_context,
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/every/example:new",
            )

        self.assertNotIn("provider-secret", str(error_context.exception))
        self.assertIsNone(error_context.exception.__cause__)
        self.assertTrue(error_context.exception.__suppress_context__)
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/registry.one", "/api/registry.testRegistryById"],
        )

    def test_inline_credentials_skip_saved_registry_login(self) -> None:
        requests: list[dict[str, object]] = []

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "username": "every",
                    "password": "secret-password",
                    "registryUrl": "ghcr.io",
                    "registryId": "registry-123",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.dokploy_request",
                side_effect=lambda **kwargs: requests.append(kwargs),
            ),
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/every/example:new",
            )

        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/application.saveDockerProvider"],
        )


if __name__ == "__main__":
    unittest.main()
