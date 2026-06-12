import unittest
from typing import Any
from unittest.mock import patch

import click

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.workflows.dokploy_deploy import (
    DokployComposeSourceRefDeployRequest,
    execute_dokploy_compose_source_ref_deploy,
    update_dokploy_compose_source_ref,
    update_dokploy_target_artifact,
)


class _ComposeSourceRefStore:
    def __init__(self, *, target_type: str = "compose") -> None:
        self.target = DokployTargetRecord(
            context="repairshopr-sync",
            instance="prod",
            target_type=target_type,  # type: ignore[arg-type]
            target_name="cm-repairshopr-sync",
            custom_git_branch="main",
            deploy_timeout_seconds=42,
            updated_at="2026-06-12T00:00:00Z",
        )
        self.target_id = DokployTargetIdRecord(
            context="repairshopr-sync",
            instance="prod",
            target_id="compose-123",
            updated_at="2026-06-12T00:00:00Z",
        )

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if context_name == self.target.context and instance_name == self.target.instance:
            return self.target
        raise FileNotFoundError(f"{context_name}/{instance_name}")

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if context_name == self.target_id.context and instance_name == self.target_id.instance:
            return self.target_id
        raise FileNotFoundError(f"{context_name}/{instance_name}")


class DokployDeployTests(unittest.TestCase):
    def test_compose_source_ref_deploy_pins_tested_ref_and_restores_original_branch(
        self,
    ) -> None:
        updates: list[dict[str, object]] = []
        triggers: list[dict[str, object]] = []
        waits: list[dict[str, object]] = []
        fetches = 0

        def fake_fetch(**_kwargs: object) -> dict[str, object]:
            nonlocal fetches
            fetches += 1
            custom_git_branch = "main" if fetches == 1 else "dokploy-deploy"
            return {
                "customGitBranch": custom_git_branch,
                "name": "cm-repairshopr-sync",
                "environmentId": "env-123",
                "sourceType": "github",
                "composePath": "docker/coolify/compose.yml",
                "customGitUrl": "git@github.com:cbusillo/repairshopr_api.git",
            }

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=fake_fetch,
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.update_dokploy_compose_source_ref",
                side_effect=lambda **kwargs: updates.append(dict(kwargs)),
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.trigger_deployment",
                side_effect=lambda **kwargs: triggers.append(dict(kwargs)),
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.wait_for_target_deployment",
                side_effect=lambda **kwargs: waits.append(dict(kwargs)),
            ),
        ):
            result = execute_dokploy_compose_source_ref_deploy(
                host="https://dokploy.example",
                token="token",
                record_store=_ComposeSourceRefStore(),
                request=DokployComposeSourceRefDeployRequest(
                    context="repairshopr-sync",
                    instance="prod",
                    source_git_ref="abc123",
                    provider_source_ref="dokploy-deploy",
                ),
            )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.source_git_ref, "abc123")
        self.assertEqual(result.provider_source_ref, "dokploy-deploy")
        self.assertEqual(result.original_source_ref, "main")
        self.assertEqual(result.restored_source_ref, "main")
        self.assertEqual(updates[0]["provider_source_ref"], "dokploy-deploy")
        self.assertEqual(updates[1]["provider_source_ref"], "main")
        self.assertEqual(triggers[0]["target_type"], "compose")
        self.assertEqual(triggers[0]["target_id"], "compose-123")
        self.assertEqual(waits[0]["timeout_seconds"], 42)

    def test_compose_source_ref_deploy_rejects_application_targets(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "requires a compose target"):
            execute_dokploy_compose_source_ref_deploy(
                host="https://dokploy.example",
                token="token",
                record_store=_ComposeSourceRefStore(target_type="application"),
                request=DokployComposeSourceRefDeployRequest(
                    context="repairshopr-sync",
                    instance="prod",
                    source_git_ref="abc123",
                    provider_source_ref="dokploy-deploy",
                ),
            )

    def test_compose_source_ref_deploy_requires_live_original_ref_before_pin(
        self,
    ) -> None:
        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={},
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.latest_deployment_for_target",
            ) as latest,
            patch(
                "control_plane.workflows.dokploy_deploy.update_dokploy_compose_source_ref",
            ) as update,
        ):
            with self.assertRaisesRegex(click.ClickException, "cannot verify the live compose"):
                execute_dokploy_compose_source_ref_deploy(
                    host="https://dokploy.example",
                    token="token",
                    record_store=_ComposeSourceRefStore(),
                    request=DokployComposeSourceRefDeployRequest(
                        context="repairshopr-sync",
                        instance="prod",
                        source_git_ref="abc123",
                        provider_source_ref="dokploy-deploy",
                    ),
                )

        latest.assert_not_called()
        update.assert_not_called()

    def test_compose_source_ref_deploy_restores_original_branch_after_failed_deploy(
        self,
    ) -> None:
        updates: list[dict[str, object]] = []
        fetches = 0

        def fake_fetch(**_kwargs: object) -> dict[str, object]:
            nonlocal fetches
            fetches += 1
            custom_git_branch = "main" if fetches == 1 else "dokploy-deploy"
            return {
                "customGitBranch": custom_git_branch,
                "name": "cm-repairshopr-sync",
                "environmentId": "env-123",
                "sourceType": "github",
            }

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=fake_fetch,
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.update_dokploy_compose_source_ref",
                side_effect=lambda **kwargs: updates.append(dict(kwargs)),
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.trigger_deployment",
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.wait_for_target_deployment",
                side_effect=click.ClickException("deploy timed out"),
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "deploy timed out"):
                execute_dokploy_compose_source_ref_deploy(
                    host="https://dokploy.example",
                    token="token",
                    record_store=_ComposeSourceRefStore(),
                    request=DokployComposeSourceRefDeployRequest(
                        context="repairshopr-sync",
                        instance="prod",
                        source_git_ref="abc123",
                        provider_source_ref="dokploy-deploy",
                    ),
                )

        self.assertEqual(updates[0]["provider_source_ref"], "dokploy-deploy")
        self.assertEqual(updates[1]["provider_source_ref"], "main")

    def test_compose_source_ref_deploy_restores_after_ambiguous_initial_pin_failure(
        self,
    ) -> None:
        updates: list[dict[str, object]] = []
        fetches = 0

        def fake_update(**kwargs: object) -> None:
            updates.append(dict(kwargs))
            if len(updates) == 1:
                raise click.ClickException("pin failed")

        def fake_fetch(**_kwargs: object) -> dict[str, object]:
            nonlocal fetches
            fetches += 1
            custom_git_branch = "main" if fetches == 1 else "dokploy-deploy"
            return {"customGitBranch": custom_git_branch}

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=fake_fetch,
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.update_dokploy_compose_source_ref",
                side_effect=fake_update,
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "pin failed"):
                execute_dokploy_compose_source_ref_deploy(
                    host="https://dokploy.example",
                    token="token",
                    record_store=_ComposeSourceRefStore(),
                    request=DokployComposeSourceRefDeployRequest(
                        context="repairshopr-sync",
                        instance="prod",
                        source_git_ref="abc123",
                        provider_source_ref="dokploy-deploy",
                    ),
                )

        self.assertEqual(updates[0]["provider_source_ref"], "dokploy-deploy")
        self.assertEqual(updates[1]["provider_source_ref"], "main")

    def test_compose_source_ref_deploy_rejects_restore_after_concurrent_ref_change(
        self,
    ) -> None:
        fetches = 0

        def fake_fetch(**_kwargs: object) -> dict[str, object]:
            nonlocal fetches
            fetches += 1
            custom_git_branch = "main" if fetches == 1 else "other-deploy"
            return {"customGitBranch": custom_git_branch}

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=fake_fetch,
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch("control_plane.workflows.dokploy_deploy.update_dokploy_compose_source_ref"),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.wait_for_target_deployment"
            ),
        ):
            with self.assertRaisesRegex(
                click.ClickException, "restoring the compose source ref failed"
            ):
                execute_dokploy_compose_source_ref_deploy(
                    host="https://dokploy.example",
                    token="token",
                    record_store=_ComposeSourceRefStore(),
                    request=DokployComposeSourceRefDeployRequest(
                        context="repairshopr-sync",
                        instance="prod",
                        source_git_ref="abc123",
                        provider_source_ref="dokploy-deploy",
                    ),
                )

    def test_compose_source_ref_deploy_requires_live_ref_before_restore(self) -> None:
        fetches = 0

        def fake_fetch(**_kwargs: object) -> dict[str, object]:
            nonlocal fetches
            fetches += 1
            if fetches == 1:
                return {"customGitBranch": "main"}
            return {}

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=fake_fetch,
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch("control_plane.workflows.dokploy_deploy.update_dokploy_compose_source_ref"),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.wait_for_target_deployment"
            ),
        ):
            with self.assertRaisesRegex(
                click.ClickException, "restoring the compose source ref failed"
            ):
                execute_dokploy_compose_source_ref_deploy(
                    host="https://dokploy.example",
                    token="token",
                    record_store=_ComposeSourceRefStore(),
                    request=DokployComposeSourceRefDeployRequest(
                        context="repairshopr-sync",
                        instance="prod",
                        source_git_ref="abc123",
                        provider_source_ref="dokploy-deploy",
                    ),
                )

    def test_update_compose_source_ref_uses_strict_source_sync_payload(self) -> None:
        requests: list[dict[str, Any]] = []

        def _fake_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            return {}

        with patch(
            "control_plane.dokploy.dokploy_request",
            side_effect=_fake_request,
        ):
            update_dokploy_compose_source_ref(
                host="https://dokploy.example",
                token="token",
                target_record=DokployTargetRecord(
                    context="repairshopr-sync",
                    instance="prod",
                    target_type="compose",
                    target_name="cm-repairshopr-sync",
                    source_type="git",
                    custom_git_url="git@github.com:cbusillo/repairshopr_api.git",
                    custom_git_branch="main",
                    compose_path="docker/coolify/compose.yml",
                    updated_at="2026-06-12T00:00:00Z",
                ),
                target_id="compose-123",
                target_payload={
                    "environmentId": "env-123",
                    "triggerType": "push",
                    "autoDeploy": False,
                    "enableSubmodules": False,
                },
                provider_source_ref="dokploy-deploy",
            )

        self.assertEqual(requests[0]["path"], "/api/compose.update")
        payload = requests[0]["payload"]
        self.assertEqual(payload["composeId"], "compose-123")
        self.assertEqual(payload["customGitBranch"], "dokploy-deploy")
        self.assertEqual(payload["customGitUrl"], "git@github.com:cbusillo/repairshopr_api.git")
        self.assertEqual(payload["composePath"], "docker/coolify/compose.yml")

    def test_update_application_injects_runtime_identity_env(self) -> None:
        requests: list[dict[str, Any]] = []

        def _fake_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            return {}

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={"env": "EXISTING=true", "username": "u", "password": "p"},
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.dokploy_request",
                side_effect=_fake_request,
            ),
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example",
                token="token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/example/app@sha256:abc",
                runtime_identity=RuntimeIdentity(
                    product="example",
                    context="example-testing",
                    instance="testing",
                    deployment_record_id="deployment-123",
                    artifact_id="ghcr.io/example/app@sha256:abc",
                    source_git_ref="abc",
                ),
            )

        self.assertEqual(requests[0]["path"], "/api/application.saveEnvironment")
        env_text = str(requests[0]["payload"]["env"])
        self.assertIn("EXISTING=true", env_text)
        self.assertIn("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-123", env_text)
        self.assertIn("LAUNCHPLANE_ARTIFACT_ID=ghcr.io/example/app@sha256:abc", env_text)
        self.assertEqual(requests[1]["path"], "/api/application.saveDockerProvider")


if __name__ == "__main__":
    unittest.main()
