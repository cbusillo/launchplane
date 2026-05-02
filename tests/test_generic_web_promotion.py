import unittest
from pathlib import Path
from unittest.mock import patch

import click
from pydantic import ValidationError

from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPromotionWorkflowProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    execute_generic_web_prod_promotion,
)
from control_plane.workflows.generic_web_promotion_workflow import (
    GenericWebPromotionWorkflowRequest,
    dispatch_generic_web_promotion_workflow,
)
from control_plane.workflows.ship import build_deployment_record


class _GenericWebPromotionStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile
        self.deployments = {}
        self.promotions = {}
        self.inventories = {}

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def write_deployment_record(self, record) -> None:
        self.deployments[record.record_id] = record

    def read_deployment_record(self, record_id: str):
        try:
            return self.deployments[record_id]
        except KeyError as exc:
            raise FileNotFoundError(record_id) from exc

    def write_promotion_record(self, record) -> None:
        self.promotions[record.record_id] = record

    def write_environment_inventory(self, record) -> None:
        self.inventories[(record.context, record.instance)] = record


def _profile(
    *,
    health_path: str = "/api/health",
    explicit_health_urls: bool = True,
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
        driver_id="generic-web",
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


def _request(**overrides) -> GenericWebProdPromotionRequest:
    payload = {
        "product": "sellyouroutboard",
        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        "source_git_ref": "abc123",
    }
    payload.update(overrides)
    return GenericWebProdPromotionRequest(**payload)


def _deployment_record():
    ship_request = ShipRequest(
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        context="sellyouroutboard-testing",
        instance="prod",
        source_git_ref="abc123",
        target_name="syo-prod-app",
        target_type="application",
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
    )


class GenericWebProdPromotionTests(unittest.TestCase):
    def test_execute_records_source_destination_health_promotion_and_inventory(self) -> None:
        store = _GenericWebPromotionStore(_profile())

        def fake_deploy(**kwargs):
            store.write_deployment_record(_deployment_record())
            return type(
                "DeployResult",
                (),
                {
                    "deployment_record_id": "deployment-syo-prod",
                    "deploy_status": "pass",
                    "target_name": "syo-prod-app",
                    "target_type": "application",
                    "target_id": "app-123",
                    "error_message": "",
                },
            )()

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

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.source_health_status, "pass")
        self.assertEqual(result.destination_health_status, "pass")
        self.assertEqual(result.inventory_record_id, "sellyouroutboard-testing-prod")
        self.assertEqual(len(store.promotions), 1)
        promotion = next(iter(store.promotions.values()))
        self.assertEqual(promotion.backup_gate.status, "skipped")
        self.assertEqual(promotion.source_health.status, "pass")
        self.assertEqual(promotion.destination_health.status, "pass")
        deployment = store.deployments["deployment-syo-prod"]
        self.assertEqual(deployment.destination_health.status, "pass")
        self.assertIn(("sellyouroutboard-testing", "prod"), store.inventories)
        self.assertEqual(healthcheck.call_count, 2)

    def test_dry_run_returns_pending_evidence_without_mutation(self) -> None:
        store = _GenericWebPromotionStore(_profile())

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

        def fake_deploy(**kwargs):
            store.write_deployment_record(_deployment_record())
            return type(
                "DeployResult",
                (),
                {
                    "deployment_record_id": "deployment-syo-prod",
                    "deploy_status": "pass",
                    "target_name": "syo-prod-app",
                    "target_type": "application",
                    "target_id": "app-123",
                    "error_message": "",
                },
            )()

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

        def fake_deploy(**kwargs):
            store.write_deployment_record(_deployment_record())
            return type(
                "DeployResult",
                (),
                {
                    "deployment_record_id": "deployment-syo-prod",
                    "deploy_status": "pass",
                    "target_name": "syo-prod-app",
                    "target_type": "application",
                    "target_id": "app-123",
                    "error_message": "",
                },
            )()

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

        self.assertEqual(result.promotion_status, "pass")
        health_urls = [call.kwargs["url"] for call in healthcheck.call_args_list]
        self.assertEqual(
            health_urls,
            [
                "https://testing.sellyouroutboard.com/healthz",
                "https://www.sellyouroutboard.com/healthz",
            ],
        )

    def test_source_health_failure_records_failed_promotion_without_deploy(self) -> None:
        store = _GenericWebPromotionStore(_profile())

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

        def fake_deploy(**kwargs):
            deployment_record = _deployment_record().model_copy(
                update={"deploy": _deployment_record().deploy.model_copy(update={"status": "fail"})}
            )
            store.write_deployment_record(deployment_record)
            return type(
                "DeployResult",
                (),
                {
                    "deployment_record_id": "deployment-syo-prod",
                    "deploy_status": "fail",
                    "target_name": "syo-prod-app",
                    "target_type": "application",
                    "target_id": "app-123",
                    "error_message": "provider failed",
                },
            )()

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


class GenericWebPromotionWorkflowTests(unittest.TestCase):
    def test_dispatches_product_workflow_and_observes_run(self) -> None:
        requests: list[tuple[str, str, dict[str, object] | None]] = []
        listed_once = False

        def fake_github_api_request(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ):
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
        ):
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
        ):
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
