from pathlib import Path
from unittest import TestCase


class DocsContractsTests(TestCase):
    def test_post_v2_transition_plans_are_issue_backed(self) -> None:
        docs_index = Path("docs/README.md").read_text(encoding="utf-8")
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")

        self.assertFalse(Path("docs/v2-foundation-adr.md").exists())
        self.assertFalse(Path("docs/compatibility-retirement.md").exists())
        self.assertNotIn("v2-foundation-adr.md", docs_index)
        self.assertNotIn("compatibility-retirement.md", docs_index)
        self.assertIn("Service Route Checklist", service_boundary)
        self.assertIn("FastAPI owns the production path", service_boundary)
        self.assertIn("stable `operation_id`", service_boundary)
        self.assertIn("maximum body-size behavior", service_boundary)
        self.assertIn("`400`, `413`, `401`, and `403`", service_boundary)
        self.assertIn("deletes obsolete compatibility code", service_boundary)
        self.assertIn("issue-backed removal condition", service_boundary)

    def test_product_environment_evidence_includes_config_status(self) -> None:
        workflow_text = Path(".github/workflows/product-environment-evidence.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "/v1/products/${product}/environments/${environment}/config-status",
            workflow_text,
        )
        self.assertIn("(.config_status // .environment // .) as $config_status", workflow_text)
        self.assertIn("config-status-summary.json", workflow_text)
        self.assertIn("product-environment-evidence-results/*-summary.json", workflow_text)

    def test_post_v2_smoke_evidence_is_durable_and_sanitized(self) -> None:
        deploy_workflow = Path(".github/workflows/deploy-launchplane.yml").read_text(
            encoding="utf-8"
        )
        odoo_smoke_workflow = Path(".github/workflows/odoo-driver-route-smoke.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Capture v2 deployed smoke evidence", deploy_workflow)
        self.assertIn("launchplane-v2-deployed-smoke.json", deploy_workflow)
        self.assertIn("actions/upload-artifact@v7", deploy_workflow)
        self.assertIn("/v1/health", deploy_workflow)
        self.assertIn("/v1/service/runtime", deploy_workflow)
        self.assertIn("/openapi.json", deploy_workflow)
        self.assertIn("/v1/work-graph/snapshot", deploy_workflow)
        self.assertIn("image_reference", deploy_workflow)
        self.assertIn("jq -s '{status: \"ok\", results: .}'", deploy_workflow)

        self.assertIn("odoo-driver-route-smoke-registration.jsonl", odoo_smoke_workflow)
        self.assertIn("odoo-driver-route-smoke-results.jsonl", odoo_smoke_workflow)
        self.assertIn("Upload route smoke evidence", odoo_smoke_workflow)
        self.assertIn("not 404 and not 5xx", odoo_smoke_workflow)
        self.assertIn("Public registration probes", odoo_smoke_workflow)
        self.assertIn("Authenticated route probes", odoo_smoke_workflow)

    def test_product_repo_integration_contract_prefers_image_deploy(self) -> None:
        product_repo_contract = Path("docs/product-repo-contract.md").read_text(encoding="utf-8")
        dokploy_service_contract = Path("docs/dokploy-service-deployments.md").read_text(
            encoding="utf-8"
        )
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")

        self.assertIn("Canonical Image Deploy Connector", product_repo_contract)
        self.assertIn("Image-backed generic-web deploy", product_repo_contract)
        self.assertIn("repairshopr_api", product_repo_contract)
        self.assertIn("deployment-20260630T034901Z-repairshopr-sync-prod", product_repo_contract)
        self.assertIn("baseline for retiring older source-ref", product_repo_contract)
        self.assertIn("reusable-product-repo-config-authority.yml@main", product_repo_contract)
        self.assertIn("pinned Launchplane tool checkout", product_repo_contract)
        self.assertIn("Product repos build, test, smoke, and publish", product_repo_contract)
        self.assertIn("Launchplane derives lifecycle meaning", product_repo_contract)
        self.assertIn("Operators act through Launchplane, not around it", product_repo_contract)
        self.assertIn("changes the hiding place, not the ownership boundary", product_repo_contract)
        self.assertIn("operator-seeded GitHub variables", product_repo_contract)
        self.assertIn("scoped adapter", product_repo_contract)
        self.assertIn("inputs. They are not checked-in product topology", product_repo_contract)
        self.assertIn("#1528 owns reducing that bridge", product_repo_contract)

        self.assertIn("stable product-repo integration surface", dokploy_service_contract)
        self.assertIn("RepairShopr Sync is the first live canary", dokploy_service_contract)
        self.assertIn(
            "without source-ref deploy or direct Dokploy mutation", dokploy_service_contract
        )
        self.assertIn("source-ref deploy bridge is retired", dokploy_service_contract)
        self.assertIn("do not use the bridge", dokploy_service_contract)
        self.assertIn("publish immutable images", dokploy_service_contract)

        self.assertIn("canonical product-repo integration surface", service_boundary)
        self.assertIn("source-ref deploy route is retired", service_boundary)

    def test_odoo_base_image_promotion_owner_is_documented(self) -> None:
        records_doc = Path("docs/records.md").read_text(encoding="utf-8")

        self.assertIn("odoo-docker` owns Odoo base-image build and promotion", records_doc)
        self.assertIn("candidate, testing, and stable image tracks", records_doc)
        self.assertIn("Launchplane does not create a", records_doc)
        self.assertIn("separate base-image promotion record today", records_doc)
        self.assertIn("Add a Launchplane-owned base-image promotion record only if", records_doc)

    def test_driver_contract_keeps_lifecycle_fixtures_in_launchplane(self) -> None:
        driver_development = Path("docs/driver-development.md").read_text(encoding="utf-8")

        self.assertIn(
            "Drivers exist to move lifecycle knowledge out of product repos", driver_development
        )
        self.assertIn("product-specific hard-coding inside Launchplane", driver_development)
        self.assertIn("Lifecycle fixtures follow the same boundary", driver_development)
        self.assertIn("contract builders own fixtures", driver_development)
        self.assertIn("Odoo is the reference complex-product case", driver_development)

    def test_generic_web_reusable_workflows_keep_product_inputs_minimal(self) -> None:
        product_repo_contract = Path("docs/product-repo-contract.md").read_text(encoding="utf-8")
        preview_contract = Path("docs/preview-workflow-contract.md").read_text(encoding="utf-8")
        deploy_workflow = Path(
            ".github/workflows/reusable-generic-web-stable-deploy.yml"
        ).read_text(encoding="utf-8")
        promotion_workflow = Path(
            ".github/workflows/reusable-generic-web-prod-promotion.yml"
        ).read_text(encoding="utf-8")
        preview_workflow = Path(
            ".github/workflows/reusable-generic-web-preview-lifecycle.yml"
        ).read_text(encoding="utf-8")
        repo_metadata = Path(".github/github.json").read_text(encoding="utf-8")
        preview_prepare_action = Path(
            ".github/actions/setup-preview-prepare-client/action.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Reusable Generic-Web Lifecycle Workflows", product_repo_contract)
        self.assertIn("reusable-generic-web-stable-deploy.yml@main", product_repo_contract)
        self.assertIn("reusable-generic-web-prod-promotion.yml@main", product_repo_contract)
        self.assertIn("reusable-generic-web-preview-lifecycle.yml@main", product_repo_contract)
        self.assertIn("route path, request JSON shape", product_repo_contract)
        self.assertIn("product key from the caller repository name", product_repo_contract)
        self.assertIn("`testing` stable lane", product_repo_contract)
        self.assertIn("idempotency key", product_repo_contract)
        self.assertIn("Destroy calls set `operation: destroy`", product_repo_contract)
        self.assertIn("unsupported_notice", product_repo_contract)
        self.assertIn("do not accept provider targets", product_repo_contract)
        self.assertIn("`preview_slug`", product_repo_contract)
        self.assertIn("`preview_url` are compatibility overrides", product_repo_contract)
        self.assertIn("anchor_pr_number` and omit", preview_contract)
        self.assertIn("conflicts with the", preview_contract)
        self.assertIn("derived value", preview_contract)
        self.assertIn("reusable-generic-web-preview-lifecycle.yml@main", preview_contract)
        self.assertIn("setup-preview-prepare-client@main", preview_contract)
        self.assertIn("setup-preview-prepare-client@main", product_repo_contract)
        self.assertIn("does not accept", preview_contract)
        self.assertIn("idempotency keys as caller inputs", preview_contract)
        self.assertIn("read-only adapter", product_repo_contract)

        self.assertIn("workflow_call:", deploy_workflow)
        self.assertIn('PRODUCT="${GITHUB_REPOSITORY#*/}"', deploy_workflow)
        self.assertIn('INSTANCE="testing"', deploy_workflow)
        self.assertIn("route-path: /v1/drivers/generic-web/deploy", deploy_workflow)
        self.assertIn("generic-web-stable-deploy", deploy_workflow)
        self.assertIn("deploy.artifact_id=${{ inputs.artifact_id }}", deploy_workflow)
        self.assertNotIn("target_id", deploy_workflow)
        self.assertNotIn("health_url", deploy_workflow)
        self.assertNotIn("preview_url", deploy_workflow)

        self.assertIn("workflow_call:", promotion_workflow)
        self.assertIn("route-path: /v1/drivers/generic-web/prod-promotion", promotion_workflow)
        self.assertIn("generic-web-prod-promotion", promotion_workflow)
        self.assertIn("promotion.artifact_id=${{ inputs.artifact_id }}", promotion_workflow)
        self.assertNotIn("target_id", promotion_workflow)
        self.assertNotIn("health_url", promotion_workflow)
        self.assertNotIn("preview_url", promotion_workflow)

        self.assertIn("workflow_call:", preview_workflow)
        self.assertIn("operation:", preview_workflow)
        self.assertIn('PRODUCT="${GITHUB_REPOSITORY#*/}"', preview_workflow)
        self.assertIn("route-path: /v1/drivers/generic-web/preview-refresh", preview_workflow)
        self.assertIn("route-path: /v1/drivers/generic-web/preview-destroy", preview_workflow)
        self.assertIn("route-path: /v1/previews/pr-feedback", preview_workflow)
        self.assertIn("generic-web-preview-lifecycle", preview_workflow)
        self.assertIn(
            "refresh.anchor_pr_number=${{ needs.resolve.outputs.anchor_pr_number }}",
            preview_workflow,
        )
        self.assertIn("refresh.image_reference=${{ inputs.image_reference }}", preview_workflow)
        self.assertIn(
            "destroy.anchor_pr_number=${{ needs.resolve.outputs.anchor_pr_number }}",
            preview_workflow,
        )
        self.assertIn("status=${{ inputs.feedback_status }}", preview_workflow)
        self.assertNotIn("inputs.preview_slug", preview_workflow)
        self.assertNotIn("inputs.preview_url", preview_workflow)
        self.assertNotIn("target_id", preview_workflow)
        self.assertNotIn("provider_target", preview_workflow)
        self.assertNotIn("feedback_markdown", preview_workflow)

        self.assertIn("Reusable Generic Web Stable Deploy", repo_metadata)
        self.assertIn("Reusable Generic Web Prod Promotion", repo_metadata)
        self.assertIn("Reusable Generic Web Preview Lifecycle", repo_metadata)
        self.assertIn("using: node24", preview_prepare_action)
        self.assertIn("preview-prepare-client.mjs", preview_prepare_action)

    def test_product_driver_reusable_workflows_keep_route_shaping_in_launchplane(
        self,
    ) -> None:
        product_repo_contract = Path("docs/product-repo-contract.md").read_text(encoding="utf-8")
        stable_deploy_workflow = Path(
            ".github/workflows/reusable-product-driver-stable-deploy.yml"
        ).read_text(encoding="utf-8")
        prod_promotion_workflow = Path(
            ".github/workflows/reusable-product-driver-prod-promotion.yml"
        ).read_text(encoding="utf-8")
        app_maintenance_workflow = Path(
            ".github/workflows/reusable-product-driver-app-maintenance.yml"
        ).read_text(encoding="utf-8")
        post_deploy_workflow = Path(
            ".github/workflows/reusable-product-driver-post-deploy.yml"
        ).read_text(encoding="utf-8")
        prod_rollback_workflow = Path(
            ".github/workflows/reusable-product-driver-prod-rollback.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Reusable Product-Driver Workflows", product_repo_contract)
        self.assertIn(
            "defaults to the `testing` lane",
            product_repo_contract,
        )
        self.assertIn("reusable-product-driver-stable-deploy.yml@main", product_repo_contract)
        self.assertIn("reusable-product-driver-prod-promotion.yml@main", product_repo_contract)
        self.assertIn("reusable-product-driver-post-deploy.yml@main", product_repo_contract)
        self.assertIn("reusable-product-driver-prod-rollback.yml@main", product_repo_contract)
        self.assertIn("route path, envelope JSON, output mapping", product_repo_contract)
        self.assertIn("transitional connectors", product_repo_contract)
        self.assertIn("should not own Launchplane route construction", product_repo_contract)
        self.assertIn("preserves explicit driver", product_repo_contract)
        self.assertIn("post-deploy phases", product_repo_contract)
        self.assertIn("defaults to `driver: odoo`", product_repo_contract)
        self.assertIn(
            "same explicit `product`, `context`, `instance`, and `phase`", product_repo_contract
        )
        self.assertIn("keeps the existing VeriReel", product_repo_contract)
        self.assertIn("also supports explicit", product_repo_contract)
        self.assertIn("`driver: odoo` callers", product_repo_contract)
        self.assertIn("explicit `product`, explicit", product_repo_contract)
        self.assertIn("source channel fixed to `testing`", product_repo_contract)
        self.assertIn("legacy", product_repo_contract)
        self.assertIn("`opr:<context>:<run>`", product_repo_contract)

        self.assertIn("workflow_call:", stable_deploy_workflow)
        self.assertIn(
            "Stable lane instance to deploy. Defaults to testing.", stable_deploy_workflow
        )
        self.assertIn('default: "testing"', stable_deploy_workflow)
        self.assertIn('PRODUCT="${GITHUB_REPOSITORY#*/}"', stable_deploy_workflow)
        self.assertIn("route_path=/v1/drivers/verireel/$INSTANCE-deploy", stable_deploy_workflow)
        self.assertIn("deploy.artifact_id=${{ inputs.artifact_id }}", stable_deploy_workflow)
        self.assertIn("deploy.source_git_ref=${{ inputs.source_git_ref }}", stable_deploy_workflow)
        self.assertIn("target_category=result.target_category", stable_deploy_workflow)
        self.assertNotIn("target_type=result.target_type", stable_deploy_workflow)
        self.assertNotIn("provider_target", stable_deploy_workflow)

        self.assertIn("workflow_call:", prod_promotion_workflow)
        self.assertIn("driver:", prod_promotion_workflow)
        self.assertIn('route_path="/v1/drivers/verireel/prod-promotion"', prod_promotion_workflow)
        self.assertIn('route_path="/v1/drivers/odoo/prod-promotion-run"', prod_promotion_workflow)
        self.assertIn("Odoo prod promotion requires testing -> prod.", prod_promotion_workflow)
        self.assertIn('idempotency_key="opp:$CONTEXT:$run_scope"', prod_promotion_workflow)
        self.assertIn("run.context=${{ steps.request.outputs.context }}", prod_promotion_workflow)
        self.assertIn(
            "run.request_id=${{ steps.request.outputs.request_id }}", prod_promotion_workflow
        )
        self.assertIn(
            "promotion.backup_record_id=${{ inputs.backup_record_id }}", prod_promotion_workflow
        )
        self.assertIn(
            "promotion.source_health_status=${{ inputs.source_health_status }}",
            prod_promotion_workflow,
        )
        self.assertIn("target_category=result.target_category", prod_promotion_workflow)
        self.assertNotIn("target_type=result.target_type", prod_promotion_workflow)
        self.assertIn("existing VeriReel", product_repo_contract)
        self.assertIn("promotion surface", product_repo_contract)
        self.assertIn("`opp:<context>:<run>`", product_repo_contract)

        self.assertIn("workflow_call:", app_maintenance_workflow)
        self.assertIn("driver:", app_maintenance_workflow)
        self.assertIn('route_path="/v1/drivers/verireel/app-maintenance"', app_maintenance_workflow)
        self.assertIn('route_path="/v1/drivers/odoo/app-maintenance"', app_maintenance_workflow)
        self.assertIn("maintenance.intent=${{ inputs.intent }}", app_maintenance_workflow)
        self.assertIn("post_deploy_status=result.post_deploy_status", app_maintenance_workflow)
        self.assertIn("override_status=result.override_status", app_maintenance_workflow)
        self.assertIn("applied_at=result.applied_at", app_maintenance_workflow)

        self.assertIn("workflow_call:", post_deploy_workflow)
        self.assertIn("driver:", post_deploy_workflow)
        self.assertIn("default: odoo", post_deploy_workflow)
        self.assertIn(
            "product:\n        description: Launchplane product key.", post_deploy_workflow
        )
        self.assertIn("context:\n        description: Runtime context.", post_deploy_workflow)
        self.assertIn('route_path="/v1/drivers/odoo/post-deploy"', post_deploy_workflow)
        self.assertIn('idempotency_prefix="odp"', post_deploy_workflow)
        self.assertIn(
            "post_deploy.phase=${{ steps.request.outputs.phase }}",
            post_deploy_workflow,
        )
        self.assertIn("post_deploy_status=result.post_deploy_status", post_deploy_workflow)
        self.assertIn("override_status=result.override_status", post_deploy_workflow)
        self.assertIn("applied_at=result.applied_at", post_deploy_workflow)

        self.assertIn("workflow_call:", prod_rollback_workflow)
        self.assertIn("driver:", prod_rollback_workflow)
        self.assertIn("default: verireel", prod_rollback_workflow)
        self.assertIn(
            'route_path="/v1/drivers/verireel/prod-rollback"',
            prod_rollback_workflow,
        )
        self.assertIn('route_path="/v1/drivers/odoo/prod-rollback"', prod_rollback_workflow)
        self.assertIn(
            "Odoo prod rollback requires source_channel 'testing'.",
            prod_rollback_workflow,
        )
        self.assertIn('PRODUCT="${GITHUB_REPOSITORY#*/}"', prod_rollback_workflow)
        self.assertIn('CONTEXT="$PRODUCT"', prod_rollback_workflow)
        self.assertIn(
            'required_inputs="PRODUCT CONTEXT INSTANCE ARTIFACT_ID REASON"',
            prod_rollback_workflow,
        )
        self.assertIn('echo "${required} is required."', prod_rollback_workflow)
        self.assertIn('idempotency_key="opr:$CONTEXT:$run_scope"', prod_rollback_workflow)
        self.assertIn(
            "rollback.backup_record_id=${{ inputs.backup_record_id }}", prod_rollback_workflow
        )
        self.assertIn("rollback.artifact_id=${{ inputs.artifact_id }}", prod_rollback_workflow)
        self.assertIn("rollback.reason=${{ inputs.reason }}", prod_rollback_workflow)
        self.assertIn(
            "rollback_health_status=result.rollback_health_status", prod_rollback_workflow
        )
        self.assertIn("rollback_started_at=result.rollback_started_at", prod_rollback_workflow)
        self.assertIn("rollback_finished_at=result.rollback_finished_at", prod_rollback_workflow)
        self.assertIn(
            "rollback.source_channel=${{ steps.request.outputs.source_channel }}",
            prod_rollback_workflow,
        )
        self.assertIn("rollback.snapshot_name=${{ inputs.snapshot_name }}", prod_rollback_workflow)
        self.assertIn("deployment_record_id=result.deployment_record_id", prod_rollback_workflow)
        self.assertIn("release_tuple_id=result.release_tuple_id", prod_rollback_workflow)
        self.assertIn("post_deploy_status=result.post_deploy_status", prod_rollback_workflow)
