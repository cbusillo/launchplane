import unittest
from unittest.mock import patch

import click

from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.dokploy_deploy import (
    _canonical_registry_host,
    _docker_image_registry_host,
    execute_dokploy_artifact_deploy,
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
                "control_plane.workflows.dokploy_deploy.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.dokploy_deploy.dokploy_api.dokploy_request",
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

    def test_saved_matching_registry_logs_in_on_each_distinct_required_server(self) -> None:
        cases: tuple[
            tuple[str, dict[str, object], list[dict[str, str]]],
            ...,
        ] = (
            (
                "distinct servers",
                {
                    "serverId": "deploy-server-123",
                    "buildServerId": "build-server-123",
                },
                [
                    {"registryId": "registry-123", "serverId": "deploy-server-123"},
                    {"registryId": "registry-123", "serverId": "build-server-123"},
                ],
            ),
            (
                "shared server",
                {"serverId": "server-123", "buildServerId": "server-123"},
                [{"registryId": "registry-123", "serverId": "server-123"}],
            ),
            (
                "default deployment and remote build servers",
                {"serverId": None, "buildServerId": "build-server-123"},
                [
                    {"registryId": "registry-123"},
                    {"registryId": "registry-123", "serverId": "build-server-123"},
                ],
            ),
            (
                "default server only",
                {"serverId": None, "buildServerId": None},
                [{"registryId": "registry-123"}],
            ),
        )

        for case_name, server_fields, expected_login_payloads in cases:
            with self.subTest(case=case_name):
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
                        "control_plane.workflows.dokploy_deploy.dokploy_api.fetch_dokploy_target_payload",
                        return_value={
                            "applicationId": "app-123",
                            "dockerImage": "ghcr.io/every/example:old",
                            "username": None,
                            "password": None,
                            "registryUrl": None,
                            "registryId": "registry-123",
                            **server_fields,
                        },
                    ),
                    patch(
                        "control_plane.workflows.dokploy_deploy.dokploy_api.dokploy_request",
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
                    ["/api/registry.one"]
                    + ["/api/registry.testRegistryById"] * len(expected_login_payloads)
                    + ["/api/application.saveDockerProvider"],
                )
                self.assertEqual(
                    [request["payload"] for request in requests[1:-1]],
                    expected_login_payloads,
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
                "control_plane.workflows.dokploy_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "registryId": "registry-123",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.dokploy_request",
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
            if kwargs["payload"] == {
                "registryId": "registry-123",
                "serverId": "deploy-server-123",
            }:
                return True
            raise click.ClickException("Registry login failed with provider-secret.")

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "registryId": "registry-123",
                    "serverId": "deploy-server-123",
                    "buildServerId": "build-server-123",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.dokploy_request",
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
            [
                "/api/registry.one",
                "/api/registry.testRegistryById",
                "/api/registry.testRegistryById",
            ],
        )

    def test_inline_credentials_skip_saved_registry_login(self) -> None:
        requests: list[dict[str, object]] = []

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.dokploy_deploy.dokploy_api.dokploy_request",
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

    def test_digest_only_application_deploy_fails_before_provider_mutation(self) -> None:
        requests: list[dict[str, object]] = []

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "app-123",
                    "username": "every",
                    "password": "secret-password",
                    "registryUrl": "ghcr.io",
                },
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.dokploy_request",
                side_effect=lambda **kwargs: requests.append(kwargs),
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.update_dokploy_target_env",
                side_effect=lambda **kwargs: requests.append(kwargs),
            ),
            self.assertRaisesRegex(
                click.ClickException,
                "requires deploy_reference.*repo:sha-<commit>",
            ),
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/every/example@sha256:"
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            )

        self.assertEqual(requests, [])

    def test_compose_deploy_keeps_digest_when_deploy_reference_is_present(self) -> None:
        artifact_id = (
            "ghcr.io/every/example@sha256:"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        ship_request = ShipRequest(
            artifact_id=artifact_id,
            deploy_reference="ghcr.io/every/example:sha-abcdef1234567890",
            context="example",
            instance="testing",
            source_git_ref="abcdef1234567890",
            target_name="example-compose",
            target_type="compose",
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            deploy_mode="dokploy-compose-api",
            verify_health=False,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        resolved_target = ResolvedTargetEvidence(
            target_type="compose",
            target_id="compose-123",
            target_name="example-compose",
        )

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.update_dokploy_target_artifact"
            ) as update_target,
            patch("control_plane.workflows.dokploy_deploy.dokploy_api.trigger_deployment"),
            patch("control_plane.workflows.dokploy_deploy.dokploy_api.wait_for_target_deployment"),
        ):
            execute_dokploy_artifact_deploy(
                host="https://dokploy.example.com",
                token="secret-token",
                ship_request=ship_request,
                resolved_target=resolved_target,
                deploy_timeout_seconds=60,
            )

        self.assertEqual(update_target.call_args.kwargs["artifact_id"], artifact_id)


if __name__ == "__main__":
    unittest.main()
