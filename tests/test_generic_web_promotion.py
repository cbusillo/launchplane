import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

import click
from pydantic import ValidationError

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPromotionWorkflowProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    GenericWebDeployResult,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    execute_generic_web_prod_promotion,
)
from control_plane.workflows.generic_web_promotion_workflow import (
    GenericWebPromotionWorkflowRequest,
    _latest_workflow_dispatch_run,
    dispatch_generic_web_promotion_workflow,
)
from control_plane.workflows.runtime_identity_health import HealthcheckPass
from control_plane.workflows.ship import build_deployment_record


class _GenericWebPromotionStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile
        self.deployments: dict[str, DeploymentRecord] = {}
        self.promotions: dict[str, PromotionRecord] = {}
        self.inventories: dict[tuple[str, str], EnvironmentInventory] = {}

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployments[record.record_id] = record

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        try:
            return self.deployments[record_id]
        except KeyError as exc:
            raise FileNotFoundError(record_id) from exc

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        try:
            return self.inventories[(context_name, instance_name)]
        except KeyError as exc:
            raise FileNotFoundError(f"{context_name}/{instance_name}") from exc

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        raise FileNotFoundError(record_id)

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self.promotions[record.record_id] = record

    def write_promotion_evidence_records(
        self,
        *,
        promotion_record: PromotionRecord,
        inventory: EnvironmentInventory,
    ) -> None:
        self.promotions[promotion_record.record_id] = promotion_record
        self.inventories[(inventory.context, inventory.instance)] = inventory

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.inventories[(record.context, record.instance)] = record


def _profile(
    *,
    health_path: str = "/api/health",
    explicit_health_urls: bool = True,
    driver_id: str = "generic-web",
) -> LaunchplaneProductProfileRecord:
    testing_health_url = ""
    prod_health_url = ""
    if explicit_health_urls:
        testing_health_url = "https://testing.sellyouroutboard.com/api/health"
        prod_health_url = "https://www.sellyouroutboard.com/api/health"
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id=driver_id,
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path=health_path,
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="sellyouroutboard-testing",
                base_url="https://testing.sellyouroutboard.com",
                health_url=testing_health_url,
            ),
            ProductLaneProfile(
                instance="prod",
                context="sellyouroutboard-testing",
                base_url="https://www.sellyouroutboard.com",
                health_url=prod_health_url,
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="sellyouroutboard-testing",
            slug_template="pr-{number}",
        ),
        updated_at="2026-05-01T21:00:00Z",
        source="test",
    )


def _request(**overrides: object) -> GenericWebProdPromotionRequest:
    payload: dict[str, object] = {
        "product": "sellyouroutboard",
        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        "source_git_ref": "abc123",
    }
    payload.update(overrides)
    return GenericWebProdPromotionRequest.model_validate(payload)


def _deployment_record() -> DeploymentRecord:
    runtime_identity = RuntimeIdentity(
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="prod",
        deployment_record_id="deployment-syo-prod",
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        source_git_ref="abc123",
    )
    ship_request = ShipRequest(
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        context="sellyouroutboard-testing",
        instance="prod",
        source_git_ref="abc123",
        target_name="syo-prod-app",
        target_type="application",
        provider_id="dokploy",
        target_category="application",
        provider_target_type="application",
        deploy_mode="dokploy-application-api",
        verify_health=False,
        destination_health=HealthcheckEvidence(status="skipped"),
    )
    return build_deployment_record(
        request=ship_request,
        record_id="deployment-syo-prod",
        deployment_id="control-plane-dokploy",
        deployment_status="pass",
        started_at="2026-05-01T21:00:00Z",
        finished_at="2026-05-01T21:01:00Z",
        resolved_target=ResolvedTargetEvidence(
            target_type="application",
            target_id="app-123",
            target_name="syo-prod-app",
        ),
        runtime_identity=runtime_identity,
    )


def _runtime_identity_payload(**overrides: object) -> dict[str, object]:
    payload = RuntimeIdentity(
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="prod",
        deployment_record_id="deployment-syo-prod",
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        source_git_ref="abc123",
    ).model_dump(mode="json")
    payload.update(overrides)
    return payload


def _testing_inventory(**overrides: object) -> EnvironmentInventory:
    deployment_record = _deployment_record().model_copy(update={"instance": "testing"})
    payload = EnvironmentInventory(
        context="sellyouroutboard-testing",
        instance="testing",
        artifact_identity=ArtifactIdentityReference(
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
        ),
        source_git_ref="abc123",
        deploy=deployment_record.deploy,
        destination_health=HealthcheckEvidence(status="pass"),
        updated_at="2026-05-01T21:01:00Z",
        deployment_record_id="deployment-syo-testing",
    ).model_dump(mode="python")
    payload.update(overrides)
    return EnvironmentInventory.model_validate(payload)


def _deploy_result(*, deploy_status: Literal["pass", "fail"] = "pass") -> GenericWebDeployResult:
    return GenericWebDeployResult(
        deployment_record_id="deployment-syo-prod",
        deploy_status=deploy_status,
        deploy_started_at="2026-05-01T21:00:00Z",
        deploy_finished_at="2026-05-01T21:01:00Z",
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="prod",
        target_name="syo-prod-app",
        target_id="app-123",
        target_category="application",
        provider_id="dokploy",
        provider_target_type="application",
        error_message="provider failed" if deploy_status == "fail" else "",
    )


class GenericWebProdPromotionTests(unittest.TestCase):
    def test_execute_accepts_based_driver_product_profile(self) -> None:
        store = _GenericWebPromotionStore(_profile(driver_id="odoo"))
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            profile = cast(LaunchplaneProductProfileRecord, kwargs["profile"])
            self.assertEqual(profile.driver_id, "odoo")
            return _deploy_result()

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ) as deploy,
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={"runtime_identity": _runtime_identity_payload()}
                ),
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "pass")
        deploy.assert_called_once()

    def test_execute_records_source_destination_health_promotion_and_inventory(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ) as healthcheck,
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={"runtime_identity": _runtime_identity_payload()}
                ),
            ) as identity_healthcheck,
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.source_health_status, "pass")
        self.assertEqual(result.destination_health_status, "pass")
        self.assertEqual(result.inventory_record_id, "sellyouroutboard-testing-prod")
        self.assertEqual(result.target_category, "application")
        self.assertEqual(result.provider_id, "dokploy")
        self.assertEqual(result.provider_target_type, "application")
        self.assertFalse(hasattr(result, "target_type"))
        self.assertEqual(len(store.promotions), 1)
        promotion = next(iter(store.promotions.values()))
        self.assertEqual(promotion.backup_gate.status, "skipped")
        self.assertEqual(promotion.source_health.status, "pass")
        self.assertEqual(promotion.destination_health.status, "pass")
        deployment = store.deployments["deployment-syo-prod"]
        self.assertEqual(deployment.destination_health.status, "pass")
        self.assertIn(("sellyouroutboard-testing", "prod"), store.inventories)
        self.assertEqual(healthcheck.call_count, 1)
        identity_healthcheck.assert_called_once()

    def test_dry_run_prod_promotion_normalizes_padded_profile_lane_contexts(self) -> None:
        profile = _profile().model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"context": f"  {lane.context}  "})
                    for lane in _profile().lanes
                )
            }
        )
        store = _GenericWebPromotionStore(profile)
        store.write_environment_inventory(_testing_inventory())

        result = execute_generic_web_prod_promotion(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(dry_run=True),
        )

        self.assertEqual(result.promotion_status, "pending")
        self.assertEqual(result.context, "sellyouroutboard-testing")
        self.assertTrue(
            result.promotion_record_id.endswith("-sellyouroutboard-testing-testing-to-prod")
        )
        self.assertNotIn("  ", result.promotion_record_id)

    def test_execute_prod_promotion_qualifies_bare_release_tag(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        expected_artifact_id = (
            "ghcr.io/cbusillo/sellyouroutboard:sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c"
        )
        store.write_environment_inventory(
            _testing_inventory(
                artifact_identity=ArtifactIdentityReference(artifact_id=expected_artifact_id),
                source_git_ref="2da6435e10cade0870ed5cbdf40c8048594f8b1c",
            )
        )
        seen_artifact_ids: list[str] = []

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            deploy_request = cast(GenericWebDeployRequest, kwargs["request"])
            self.assertIsInstance(deploy_request, GenericWebDeployRequest)
            seen_artifact_ids.append(deploy_request.artifact_id)
            store.write_deployment_record(
                _deployment_record().model_copy(
                    update={
                        "artifact_identity": ArtifactIdentityReference(
                            artifact_id=deploy_request.artifact_id
                        )
                    }
                )
            )
            return _deploy_result()

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={
                        "runtime_identity": _runtime_identity_payload(
                            artifact_id=expected_artifact_id,
                            source_git_ref="2da6435e10cade0870ed5cbdf40c8048594f8b1c",
                        )
                    }
                ),
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(
                    artifact_id="sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c",
                    source_git_ref="2da6435e10cade0870ed5cbdf40c8048594f8b1c",
                ),
            )

        self.assertEqual(seen_artifact_ids, [expected_artifact_id])
        self.assertEqual(result.artifact_id, expected_artifact_id)
        promotion = next(iter(store.promotions.values()))
        self.assertEqual(promotion.artifact_identity.artifact_id, expected_artifact_id)

    def test_execute_prod_promotion_creates_github_release(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())
        github_requests: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            github_requests.append((method, path, body))
            self.assertEqual(token, "release-token")
            if path.endswith("/git/ref/tags/v0.3.0"):
                raise click.ClickException("GitHub API request failed: HTTP Error 404: Not Found")
            if path.endswith("/releases/tags/v0.3.0"):
                raise click.ClickException("GitHub API request failed: HTTP Error 404: Not Found")
            if method == "POST" and path.endswith("/releases"):
                return {
                    "html_url": "https://github.com/cbusillo/sellyouroutboard/releases/tag/v0.3.0"
                }
            raise AssertionError(path)

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={"runtime_identity": _runtime_identity_payload()}
                ),
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.resolve_launchplane_github_token",
                return_value="release-token",
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.github_api_request",
                side_effect=fake_github_api_request,
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(release_tag="v0.3.0"),
            )

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.release_status, "pass")
        self.assertEqual(result.release_tag, "v0.3.0")
        self.assertEqual(
            result.release_url,
            "https://github.com/cbusillo/sellyouroutboard/releases/tag/v0.3.0",
        )
        self.assertEqual(
            github_requests[-1],
            (
                "POST",
                "/repos/cbusillo/sellyouroutboard/releases",
                {
                    "tag_name": "v0.3.0",
                    "target_commitish": "abc123",
                    "name": "v0.3.0",
                    "body": "\n".join(
                        (
                            "Promoted sellyouroutboard to prod.",
                            "",
                            "- Artifact: `ghcr.io/cbusillo/sellyouroutboard@sha256:abc123`",
                            "- Source git ref: `abc123`",
                            f"- Promotion record: `{result.promotion_record_id}`",
                            "- Deployment record: `deployment-syo-prod`",
                            "- Inventory record: `sellyouroutboard-testing-prod`",
                        )
                    ),
                    "draft": False,
                    "prerelease": False,
                },
            ),
        )

    def test_execute_prod_promotion_rejects_release_tag_mismatch(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            if path.endswith("/git/ref/tags/v0.3.0"):
                return {"object": {"type": "commit", "sha": "different"}}
            raise AssertionError(path)

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={"runtime_identity": _runtime_identity_payload()}
                ),
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.resolve_launchplane_github_token",
                return_value="release-token",
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.github_api_request",
                side_effect=fake_github_api_request,
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(release_tag="v0.3.0"),
            )

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.release_status, "fail")
        self.assertIn("not promoted revision", result.error_message)

    def test_dry_run_returns_pending_evidence_without_mutation(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        result = execute_generic_web_prod_promotion(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(dry_run=True),
        )

        self.assertTrue(result.dry_run)
        self.assertEqual(result.promotion_status, "pending")
        self.assertEqual(result.source_health_status, "pending")
        self.assertEqual(result.destination_health_status, "pending")
        self.assertEqual(store.deployments, {})
        self.assertEqual(store.promotions, {})

    def test_request_requires_testing_to_prod(self) -> None:
        with self.assertRaises(ValidationError):
            _request(from_instance="staging", to_instance="prod")

    def test_execute_refreshes_inventory_when_health_is_skipped(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        with patch(
            "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
            side_effect=fake_deploy,
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(verify_health=False),
            )

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.destination_health_status, "skipped")
        self.assertEqual(result.inventory_record_id, "sellyouroutboard-testing-prod")
        self.assertIn(("sellyouroutboard-testing", "prod"), store.inventories)

    def test_health_fallback_uses_product_health_path(self) -> None:
        store = _GenericWebPromotionStore(
            _profile(health_path="/healthz", explicit_health_urls=False)
        )
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ) as healthcheck,
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={"runtime_identity": _runtime_identity_payload()}
                ),
            ) as identity_healthcheck,
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "pass")
        health_urls = [call.kwargs["url"] for call in healthcheck.call_args_list]
        identity_health_urls = [call.kwargs["url"] for call in identity_healthcheck.call_args_list]
        self.assertEqual(
            health_urls,
            ["https://testing.sellyouroutboard.com/healthz"],
        )
        self.assertEqual(
            identity_health_urls,
            ["https://www.sellyouroutboard.com/healthz"],
        )

    def test_source_health_failure_records_failed_promotion_without_deploy(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy"
            ) as deploy,
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                side_effect=click.ClickException("source unhealthy"),
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "fail")
        self.assertEqual(result.source_health_status, "fail")
        self.assertEqual(result.destination_health_status, "skipped")
        self.assertIn("source unhealthy", result.error_message)
        self.assertEqual(store.deployments, {})
        self.assertEqual(len(store.promotions), 1)
        deploy.assert_not_called()

    def test_deploy_failure_marks_destination_health_skipped(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            deployment_record = _deployment_record().model_copy(
                update={"deploy": _deployment_record().deploy.model_copy(update={"status": "fail"})}
            )
            store.write_deployment_record(deployment_record)
            return _deploy_result(deploy_status="fail")

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ) as healthcheck,
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "fail")
        self.assertEqual(result.source_health_status, "pass")
        self.assertEqual(result.destination_health_status, "skipped")
        self.assertEqual(healthcheck.call_count, 1)

    def test_missing_runtime_identity_fails_promotion_without_inventory_refresh(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(payload={"status": "ok"}),
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "fail")
        self.assertEqual(result.destination_health_status, "fail")
        self.assertIn("Runtime identity was not reported", result.error_message)
        self.assertNotIn(("sellyouroutboard-testing", "prod"), store.inventories)
        promotion = next(iter(store.promotions.values()))
        self.assertEqual(promotion.destination_health.status, "fail")
        self.assertEqual(promotion.destination_health.runtime_identity_status, "missing")
        self.assertEqual(promotion.deploy.status, "pass")

    def test_runtime_identity_mismatch_fails_promotion_without_inventory_refresh(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(_testing_inventory())
        observed_identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="prod",
            deployment_record_id="deployment-syo-prod",
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:different",
            source_git_ref="abc123",
        )

        def fake_deploy(**kwargs: object) -> GenericWebDeployResult:
            store.write_deployment_record(_deployment_record())
            return _deploy_result()

        with (
            patch(
                "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy",
                side_effect=fake_deploy,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion._wait_for_healthcheck",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                return_value=HealthcheckPass(
                    payload={"runtime_identity": observed_identity.model_dump(mode="json")}
                ),
            ),
        ):
            result = execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.promotion_status, "fail")
        self.assertEqual(result.destination_health_status, "fail")
        self.assertIn("Runtime identity mismatched fields", result.error_message)
        self.assertNotIn(("sellyouroutboard-testing", "prod"), store.inventories)
        promotion = next(iter(store.promotions.values()))
        self.assertEqual(promotion.destination_health.status, "fail")
        self.assertEqual(promotion.destination_health.runtime_identity_status, "mismatch")
        self.assertEqual(promotion.destination_health.observed_runtime_identity, observed_identity)

    def test_execute_rejects_stale_source_inventory(self) -> None:
        store = _GenericWebPromotionStore(_profile())
        store.write_environment_inventory(
            _testing_inventory(
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:newer"
                ),
                source_git_ref="newer",
            )
        )

        with self.assertRaises(click.ClickException) as caught:
            execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertIn("does not match current source inventory", str(caught.exception))
        self.assertEqual(store.deployments, {})
        self.assertEqual(store.promotions, {})

    def test_execute_rejects_missing_source_inventory(self) -> None:
        store = _GenericWebPromotionStore(_profile())

        with self.assertRaises(click.ClickException) as caught:
            execute_generic_web_prod_promotion(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertIn("requires current source environment inventory", str(caught.exception))
        self.assertEqual(store.deployments, {})
        self.assertEqual(store.promotions, {})


class GenericWebPromotionWorkflowTests(unittest.TestCase):
    def test_dispatch_accepts_based_driver_product_profile(self) -> None:
        with (
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                return_value={"workflow_runs": []},
            ),
        ):
            result = dispatch_generic_web_promotion_workflow(
                control_plane_root=Path("."),
                profile=_profile(driver_id="odoo"),
                request=GenericWebPromotionWorkflowRequest(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    dry_run=True,
                    observe_timeout_seconds=0,
                ),
            )

        self.assertEqual(result.repository, "cbusillo/sellyouroutboard")
        self.assertTrue(result.dry_run)

    def test_dispatch_rejects_unbased_driver_product_profile(self) -> None:
        with self.assertRaises(click.ClickException):
            dispatch_generic_web_promotion_workflow(
                control_plane_root=Path("."),
                profile=_profile(driver_id="missing-driver"),
                request=GenericWebPromotionWorkflowRequest(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    dry_run=True,
                    observe_timeout_seconds=0,
                ),
            )

    def test_dispatches_product_workflow_and_observes_run(self) -> None:
        requests: list[tuple[str, str, dict[str, object] | None]] = []
        listed_once = False

        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            nonlocal listed_once
            requests.append((method, path, body))
            if method == "POST":
                self.assertEqual(token, "github-token")
                return None
            if not listed_once:
                listed_once = True
                return {
                    "workflow_runs": [
                        {
                            "id": 100,
                            "html_url": "https://github.com/cbusillo/sellyouroutboard/actions/runs/100",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            return {
                "workflow_runs": [
                    {
                        "id": 25237186636,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/actions/runs/25237186636",
                        "status": "queued",
                        "conclusion": None,
                        "created_at": "2099-01-01T00:00:00Z",
                    }
                ]
            }

        with (
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                side_effect=fake_github_api_request,
            ),
        ):
            profile = _profile().model_copy(
                update={"promotion_workflow": ProductPromotionWorkflowProfile(default_bump="minor")}
            )
            result = dispatch_generic_web_promotion_workflow(
                control_plane_root=Path("."),
                profile=profile,
                request=GenericWebPromotionWorkflowRequest(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    dry_run=False,
                    observe_timeout_seconds=0,
                ),
            )

        self.assertEqual(result.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(result.workflow_id, "promote-prod.yml")
        self.assertEqual(result.ref, "main")
        self.assertFalse(result.dry_run)
        self.assertEqual(result.bump, "minor")
        self.assertEqual(result.run_id, 25237186636)
        self.assertEqual(result.run_status, "queued")
        self.assertEqual(requests[1][0], "POST")
        self.assertEqual(
            requests[1][1],
            "/repos/cbusillo/sellyouroutboard/actions/workflows/promote-prod.yml/dispatches",
        )
        self.assertEqual(
            requests[1][2],
            {"ref": "main", "inputs": {"dry_run": "false", "bump": "minor"}},
        )

    def test_observation_declines_ambiguous_new_runs(self) -> None:
        list_count = 0

        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            nonlocal list_count
            if method == "POST":
                return None
            list_count += 1
            if list_count == 1:
                return {"workflow_runs": []}
            return {
                "workflow_runs": [
                    {"id": 101, "html_url": "https://github.example/runs/101", "status": "queued"},
                    {"id": 102, "html_url": "https://github.example/runs/102", "status": "queued"},
                ]
            }

        with (
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                side_effect=fake_github_api_request,
            ),
        ):
            result = dispatch_generic_web_promotion_workflow(
                control_plane_root=Path("."),
                profile=_profile(),
                request=GenericWebPromotionWorkflowRequest(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    observe_timeout_seconds=0,
                ),
            )

        self.assertEqual(result.run_id, 0)
        self.assertEqual(result.run_url, "")
        self.assertEqual(result.run_status, "pending")

    def test_observation_ignores_new_run_created_before_dispatch(self) -> None:
        list_count = 0

        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            nonlocal list_count
            if method == "POST":
                return None
            list_count += 1
            if list_count == 1:
                return {"workflow_runs": []}
            return {
                "workflow_runs": [
                    {
                        "id": 101,
                        "html_url": "https://github.example/runs/101",
                        "status": "queued",
                        "created_at": "2000-01-01T00:00:00Z",
                    }
                ]
            }

        with (
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
                side_effect=fake_github_api_request,
            ),
        ):
            result = dispatch_generic_web_promotion_workflow(
                control_plane_root=Path("."),
                profile=_profile(),
                request=GenericWebPromotionWorkflowRequest(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    observe_timeout_seconds=0,
                ),
            )

        self.assertEqual(result.run_id, 0)
        self.assertEqual(result.run_url, "")
        self.assertEqual(result.run_status, "pending")

    def test_observation_accepts_run_created_in_same_second_as_dispatch(self) -> None:
        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            self.assertEqual(method, "GET")
            return {
                "workflow_runs": [
                    {
                        "id": 101,
                        "html_url": "https://github.example/runs/101",
                        "status": "queued",
                        "created_at": "2099-01-01T00:00:00Z",
                    }
                ]
            }

        with patch(
            "control_plane.workflows.generic_web_promotion_workflow.github_api_request",
            side_effect=fake_github_api_request,
        ):
            run = _latest_workflow_dispatch_run(
                owner="cbusillo",
                repo="sellyouroutboard",
                workflow_id="promote-prod.yml",
                ref="main",
                token="github-token",
                previous_run_ids=set(),
                min_created_at=datetime(2099, 1, 1, 0, 0, 0, 999999, UTC),
            )

        self.assertEqual(run.get("id"), 101)

    def test_dispatch_requires_managed_github_token(self) -> None:
        with patch(
            "control_plane.workflows.generic_web_promotion_workflow.resolve_launchplane_github_token",
            return_value="",
        ):
            with self.assertRaises(click.ClickException) as raised:
                dispatch_generic_web_promotion_workflow(
                    control_plane_root=Path("."),
                    profile=_profile(),
                    request=GenericWebPromotionWorkflowRequest(
                        product="sellyouroutboard",
                        context="sellyouroutboard-testing",
                    ),
                )

        self.assertIn("GITHUB_TOKEN", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
