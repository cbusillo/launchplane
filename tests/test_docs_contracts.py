import json
from pathlib import Path
from unittest import TestCase

from tests.support.workflows import WorkflowInvariantViolation
from tests.support.workflows import check_ci_aggregate_gate
from tests.support.workflows import check_frontend_browser_smoke
from tests.support.workflows import check_fork_runner_isolation
from tests.support.workflows import check_security_aggregate_gate
from tests.support.workflows import check_unittest_timing_snapshot
from tests.support.workflows import load_workflow


def _assert_no_workflow_violations(
    test_case: TestCase,
    violations: tuple[WorkflowInvariantViolation, ...],
) -> None:
    test_case.assertEqual([], [str(violation) for violation in violations])


class DocsContractsTests(TestCase):
    def test_generic_web_preview_retirement_authority_is_documented(self) -> None:
        operations = Path("docs/operations.md").read_text(encoding="utf-8")
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")
        records = Path("docs/records.md").read_text(encoding="utf-8")
        new_product_repo = Path("docs/new-product-repo.md").read_text(encoding="utf-8")

        self.assertIn("without querying GitHub", new_product_repo)
        self.assertIn("mints no repository-scoped", service_boundary)
        self.assertIn("Caller repository values are optional assertions", service_boundary)
        self.assertIn("SHA-256 digest of the", records)
        self.assertIn("after archival or GitHub App offboarding", operations)

    def test_protected_operator_workflows_require_exact_run_babysitter(self) -> None:
        agents = Path("AGENTS.md").read_text(encoding="utf-8")
        operations = Path("docs/operations.md").read_text(encoding="utf-8")

        for required_text in (
            "github_workflow_babysit.py",
            "gh workflow run",
            "generic run waiter",
        ):
            self.assertIn(required_text, agents)
        for required_text in (
            "github_workflow_babysit.py",
            "status=waiting",
            "pending_deployments",
            "split-identity",
        ):
            self.assertIn(required_text, operations)

    def test_required_status_checks_use_fork_aware_aggregate_gates(self) -> None:
        metadata = json.loads(Path(".github/github.json").read_text(encoding="utf-8"))
        ci_workflow = load_workflow(".github/workflows/ci.yml")
        security_workflow = load_workflow(".github/workflows/security.yml")

        self.assertEqual(["ci-gate", "security-gate"], metadata["requiredStatusChecks"])
        _assert_no_workflow_violations(
            self,
            check_fork_runner_isolation(
                ci_workflow,
                same_repo_jobs=(
                    "static_checks",
                    "container_scan",
                    "frontend_validate",
                    "test_timing_snapshot",
                    "test_shards",
                    "test",
                    "postgres_integration",
                ),
                fork_jobs=(
                    "static_checks_fork",
                    "container_scan_fork",
                    "frontend_validate_fork",
                    "test_fork",
                ),
            ),
        )
        _assert_no_workflow_violations(self, check_ci_aggregate_gate(ci_workflow))
        _assert_no_workflow_violations(
            self,
            check_fork_runner_isolation(
                security_workflow,
                same_repo_jobs=("workflow_lint", "secret_scan"),
                fork_jobs=("workflow_lint_fork", "secret_scan_fork"),
            ),
        )
        _assert_no_workflow_violations(
            self,
            check_security_aggregate_gate(security_workflow),
        )

    def test_ci_shards_share_one_unittest_timing_snapshot(self) -> None:
        metadata = json.loads(Path(".github/github.json").read_text(encoding="utf-8"))
        ci_workflow = load_workflow(".github/workflows/ci.yml")
        testing_docs = Path("docs/style/testing.md").read_text(encoding="utf-8")

        self.assertEqual(
            "uv run --extra dev launchplane ci unittest-shard local",
            metadata["qualityGate"]["test"]["default"],
        )
        self.assertEqual(
            "uv run --extra dev python -m unittest {modules}",
            metadata["qualityGate"]["test"]["targeted"],
        )
        self.assertIn(
            "uv run --extra dev launchplane ci postgres-integration",
            metadata["qualityGate"]["test"]["postgresIntegration"],
        )
        self.assertIn("uv run --extra dev launchplane ci unittest-shard local", testing_docs)
        self.assertIn("uv run --extra dev launchplane ci postgres-integration", testing_docs)
        self.assertIn("12 shards with a 20-test/30-second split threshold", testing_docs)
        self.assertIn("GitHub Actions remains the source of truth", testing_docs)
        _assert_no_workflow_violations(self, check_unittest_timing_snapshot(ci_workflow))

    def test_frontend_browser_smoke_is_documented_and_wired(self) -> None:
        metadata = json.loads(Path(".github/github.json").read_text(encoding="utf-8"))
        ci_workflow = load_workflow(".github/workflows/ci.yml")
        testing_docs = Path("docs/style/testing.md").read_text(encoding="utf-8")
        normalized_testing_docs = " ".join(testing_docs.split())

        self.assertEqual(
            "pnpm --dir frontend test:browser",
            metadata["qualityGate"]["build"]["browser"],
        )
        self.assertIn("pnpm --dir frontend test:browser", testing_docs)
        self.assertIn(
            "uses only repo-local development fixtures",
            normalized_testing_docs,
        )
        for fixture in ("`?fixture=products`", "`?fixture=empty`", "`?fixture=error`"):
            self.assertIn(fixture, testing_docs)
        self.assertIn("Deployed OIDC smoke remains a separate", testing_docs)
        _assert_no_workflow_violations(self, check_frontend_browser_smoke(ci_workflow))

    def test_frontend_openapi_codegen_gates_are_documented_and_wired(self) -> None:
        metadata = json.loads(Path(".github/github.json").read_text(encoding="utf-8"))
        ci_workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")
        agent_context_boundary = Path("docs/agent-context-boundary.md").read_text(encoding="utf-8")
        agent_operator_contract = Path("docs/agent-operator-contract.md").read_text(
            encoding="utf-8"
        )
        docs_index = Path("docs/README.md").read_text(encoding="utf-8")
        canonical_openapi = json.loads(
            Path("frontend/generated/openapi-canonical.json").read_text(encoding="utf-8")
        )
        ui_openapi = json.loads(
            Path("frontend/generated/openapi-ui.json").read_text(encoding="utf-8")
        )
        frontend_types = Path("frontend/src/types.ts").read_text(encoding="utf-8")
        frontend_api = Path("frontend/src/api.ts").read_text(encoding="utf-8")
        engineering_model = Path("frontend/src/engineering-model.ts").read_text(encoding="utf-8")
        browser_operation = Path("frontend/src/browser-operation.ts").read_text(encoding="utf-8")
        browser_write_contract = Path("frontend/src/browser-write-contract.ts").read_text(
            encoding="utf-8"
        )
        action_model = Path("frontend/src/action-model.ts").read_text(encoding="utf-8")
        frontend_package = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))
        openapi_drift = Path("frontend/scripts/check-openapi-drift.mjs").read_text(encoding="utf-8")

        self.assertEqual(
            "uv run launchplane service export-openapi --output frontend/generated/openapi-canonical.json",
            metadata["qualityGate"]["openapi"]["export"],
        )
        self.assertEqual(
            "pnpm --dir frontend generate:openapi",
            metadata["qualityGate"]["openapi"]["frontendGenerate"],
        )
        self.assertEqual(
            "pnpm --dir frontend check:openapi-drift",
            metadata["qualityGate"]["openapi"]["frontendDrift"],
        )
        self.assertEqual(
            "contracts/agent-operator-contract.json",
            metadata["qualityGate"]["agentContract"]["artifact"],
        )
        self.assertEqual(
            "uv run launchplane service export-agent-contract --output contracts/agent-operator-contract.json",
            metadata["qualityGate"]["agentContract"]["export"],
        )
        self.assertEqual(
            "pnpm --dir frontend check:openapi-drift",
            metadata["qualityGate"]["agentContract"]["drift"],
        )
        self.assertIn(
            "`uv run launchplane service export-openapi --output frontend/generated/openapi-canonical.json`",
            service_boundary,
        )
        self.assertIn("`pnpm --dir frontend check:openapi-drift`", service_boundary)
        self.assertIn(
            "`uv run launchplane service export-agent-contract --output contracts/agent-operator-contract.json`",
            service_boundary,
        )
        self.assertIn("agent-operator-contract.md", docs_index)
        self.assertIn("contracts/agent-operator-contract.json", agent_context_boundary)
        self.assertIn("`semantic_digest_sha256`", agent_operator_contract)
        self.assertIn("`normalization_version`", agent_operator_contract)
        self.assertEqual(
            "uv run launchplane service export-agent-contract --output ../contracts/agent-operator-contract.json",
            frontend_package["scripts"]["generate:agent-contract"],
        )
        self.assertIn(
            "pnpm generate:agent-contract",
            frontend_package["scripts"]["generate:openapi"],
        )
        self.assertIn("checkedAgentContract.schema_version", openapi_drift)
        self.assertIn("Install uv", ci_workflow)
        self.assertIn("Install Python", ci_workflow)
        read_operations = canonical_openapi["x-launchplane-ui-read-operations"]
        write_operations = canonical_openapi["x-launchplane-ui-write-operations"]
        self.assertEqual(
            set(read_operations) | set(write_operations),
            set(ui_openapi["paths"]),
        )
        for route_path, operation_id in read_operations.items():
            self.assertEqual(set(ui_openapi["paths"][route_path]), {"get"})
            self.assertEqual(ui_openapi["paths"][route_path]["get"]["operationId"], operation_id)
        for route_path, operation_id in write_operations.items():
            self.assertEqual(set(ui_openapi["paths"][route_path]), {"post"})
            self.assertEqual(ui_openapi["paths"][route_path]["post"]["operationId"], operation_id)
        for generated_alias in (
            "export type AuthSessionPayload = AuthSessionResponse",
            "export type ApiErrorPayload = LaunchplaneErrorResponse",
            "export type ProductListPayload = ProductEnvironmentListResponse",
        ):
            self.assertIn(generated_alias, frontend_types)
        self.assertNotIn("export interface DriverDescriptor", frontend_types)
        self.assertNotIn("export interface LaneSummary", frontend_types)
        self.assertNotIn("WorkGraphSnapshotPayload", frontend_types)
        self.assertNotIn("ProductConfigApplyRequest", frontend_types)
        self.assertNotIn("GenericWebProdPromotionPayload", frontend_types)
        self.assertNotIn("GitHubIssueInboxReconcilePayload", frontend_types)
        self.assertNotIn(
            "/v1/work-graph/github/issues/reconcile",
            write_operations,
        )
        for operation_data_type in (
            "ApplyProductEnvironmentConfigData",
            "DispatchProductPromotionWorkflowData",
            "DryRunProductPromotionData",
            "RankWorkGraphSnapshotData",
        ):
            self.assertIn(operation_data_type, frontend_api)
        self.assertNotIn("ReconcileWorkGraphIssueInboxData", frontend_api)
        self.assertIn("WorkGraphSnapshotResponse", frontend_api)
        self.assertIn("EveryCodeSummaryResponse", frontend_api)
        self.assertIn("ISSUE_RECONCILIATION_BROWSER_BOUNDARY", engineering_model)
        self.assertIn("MERGE_TRAIN_BROWSER_BOUNDARY", engineering_model)
        self.assertIn("requestGeneratedPost", frontend_api)
        self.assertIn('payload: ApplyProductEnvironmentConfigData["body"]', frontend_api)
        self.assertNotIn("ApplyProductConfigData", frontend_api)
        self.assertIn('headers: { "Idempotency-Key": options.idempotencyKey }', frontend_api)
        self.assertIn("errorPayload.error.code", frontend_api)
        self.assertIn("BrowserOperationOptions", frontend_api)
        for route_path in write_operations:
            self.assertIn(route_path, browser_write_contract)
        self.assertIn("BrowserWriteRoute", browser_write_contract)
        self.assertIn("BrowserOperationState", browser_operation)
        self.assertIn("markBrowserOperationDispatched", browser_operation)
        self.assertIn("originalTraceId", browser_operation)
        self.assertIn("previous result is uncertain", browser_operation)
        self.assertIn("requiresIdempotencyContinuity", browser_operation)
        self.assertIn("dryRunProductPromotion", frontend_api)
        self.assertIn("dispatchProductPromotionWorkflow", frontend_api)
        self.assertNotIn("dryRunGenericWebProdPromotion", frontend_api)
        self.assertIn("browserActionPresentation", action_model)
        self.assertIn("No generated browser operation is registered", action_model)
        self.assertIn("pnpm test", frontend_package["scripts"]["validate"])

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
            "config_status_route=",
            workflow_text,
        )
        self.assertIn("/config-status", workflow_text)
        self.assertIn('quote(product, safe="")', workflow_text)
        self.assertIn('quote(environment, safe="")', workflow_text)
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
        self.assertIn("actions/upload-artifact@", deploy_workflow)
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
        self.assertIn(
            "reusable-product-repo-config-authority.yml@<launchplane-sha>",
            product_repo_contract,
        )
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
        rollback_workflow = Path(
            ".github/workflows/reusable-generic-web-prod-rollback.yml"
        ).read_text(encoding="utf-8")
        preview_workflow = Path(
            ".github/workflows/reusable-generic-web-preview-lifecycle.yml"
        ).read_text(encoding="utf-8")
        preview_verification_workflow = Path(
            ".github/workflows/reusable-generic-web-preview-verification.yml"
        ).read_text(encoding="utf-8")
        stable_verification_workflow = Path(
            ".github/workflows/reusable-generic-web-stable-verification.yml"
        ).read_text(encoding="utf-8")
        preview_feedback_status_workflow = Path(
            ".github/workflows/reusable-preview-feedback-status.yml"
        ).read_text(encoding="utf-8")
        repo_metadata = Path(".github/github.json").read_text(encoding="utf-8")
        preview_prepare_action = Path(
            ".github/actions/setup-preview-prepare-client/action.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Reusable Generic-Web Lifecycle Workflows", product_repo_contract)
        self.assertIn(
            "reusable-generic-web-stable-deploy.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-generic-web-prod-promotion.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn("product-specific semantic release tags", product_repo_contract)
        self.assertIn("source-inventory lookup and validation", product_repo_contract)
        self.assertIn(
            "reusable-generic-web-prod-rollback.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-generic-web-stable-verification.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-generic-web-preview-lifecycle.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-generic-web-preview-verification.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn("route path, request JSON shape", product_repo_contract)
        self.assertIn("product key from the caller repository name", product_repo_contract)
        self.assertIn("`testing` stable lane", product_repo_contract)
        self.assertIn("Production rollback uses stored Launchplane", product_repo_contract)
        self.assertIn(
            "Stable verification uses product-owned smoke evidence", product_repo_contract
        )
        self.assertIn("idempotency key", product_repo_contract)
        self.assertIn("dry_run:", promotion_workflow)
        self.assertIn("default: true\n        type: boolean", promotion_workflow)
        self.assertIn("release_tag:", promotion_workflow)
        self.assertIn("health-timeout-seconds:", promotion_workflow)
        self.assertIn(
            "payload-file: .launchplane/generic-web-prod-promotion-payload.json",
            promotion_workflow,
        )
        self.assertIn("release_url=result.release_url", promotion_workflow)
        self.assertIn("dry_run=result.dry_run", promotion_workflow)
        self.assertIn("launchplane-generic-web-prod-", promotion_workflow)
        self.assertIn("Verify release tag is unused", promotion_workflow)
        self.assertIn("if: ${{ inputs.release_tag != '' }}", promotion_workflow)
        self.assertIn("if ! grep -q 'HTTP 404'", promotion_workflow)
        self.assertIn("dry-run release preflight", promotion_workflow)
        self.assertIn("always() && steps.lp.outcome != 'skipped'", promotion_workflow)
        self.assertIn('>>"$GITHUB_STEP_SUMMARY"', promotion_workflow)
        self.assertIn("Destroy calls set `operation: destroy`", product_repo_contract)
        self.assertIn("unsupported_notice", product_repo_contract)
        self.assertIn("do not accept provider targets", product_repo_contract)
        self.assertIn("`preview_slug`", product_repo_contract)
        self.assertIn("`preview_url` are compatibility overrides", product_repo_contract)
        self.assertIn("anchor_pr_number` and omit", preview_contract)
        self.assertIn("conflicts with the", preview_contract)
        self.assertIn("derived value", preview_contract)
        self.assertIn(
            "reusable-generic-web-preview-lifecycle.yml@<launchplane-sha>",
            preview_contract,
        )
        self.assertIn("reusable-preview-feedback-status.yml@<launchplane-sha>", preview_contract)
        self.assertIn(
            "reusable-preview-feedback-status.yml@<launchplane-sha>", product_repo_contract
        )
        self.assertIn("setup-preview-prepare-client@<launchplane-sha>", preview_contract)
        self.assertIn("setup-preview-prepare-client@<launchplane-sha>", product_repo_contract)
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
        self.assertIn("artifact_id: requestedArtifact", promotion_workflow)
        self.assertIn("artifact_id=result.artifact_id", promotion_workflow)
        self.assertNotIn("target_id", promotion_workflow)
        self.assertNotIn("health_url", promotion_workflow)
        self.assertNotIn("preview_url", promotion_workflow)

        self.assertIn("workflow_call:", rollback_workflow)
        self.assertIn("route-path: /v1/drivers/generic-web/prod-rollback", rollback_workflow)
        self.assertIn("generic-web-prod-rollback", rollback_workflow)
        self.assertIn(
            "payload-file: .launchplane/generic-web-prod-rollback-payload.json",
            rollback_workflow,
        )
        self.assertIn(
            "ROLLBACK_DEPLOYMENT_RECORD_ID: ${{ steps.request.outputs.rollback_deployment_record_id }}",
            rollback_workflow,
        )
        self.assertIn("backup_required: booleanInput('BACKUP_REQUIRED')", rollback_workflow)
        self.assertIn("verify_health: booleanInput('VERIFY_HEALTH')", rollback_workflow)
        self.assertIn("generic_web_rollback_plan_id", rollback_workflow)
        self.assertNotIn("target_id", rollback_workflow)
        self.assertNotIn("provider_target", rollback_workflow)
        self.assertNotIn("health_url", rollback_workflow)
        self.assertNotIn("preview_url", rollback_workflow)

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

        self.assertIn("workflow_call:", preview_verification_workflow)
        self.assertIn(
            "route-path: /v1/drivers/generic-web/preview-verification",
            preview_verification_workflow,
        )
        self.assertIn("generic-web-preview-verification", preview_verification_workflow)
        self.assertIn('ANCHOR_REPO="${GITHUB_REPOSITORY#*/}"', preview_verification_workflow)
        self.assertIn("skipped|neutral", preview_verification_workflow)
        self.assertIn(
            "payload-file: .launchplane/generic-web-preview-verification-payload.json",
            preview_verification_workflow,
        )
        self.assertIn(
            "anchor_repo: process.env.ANCHOR_REPO",
            preview_verification_workflow,
        )
        self.assertIn(
            "checked_urls: jsonInput('CHECKED_URLS', [])",
            preview_verification_workflow,
        )
        self.assertIn(
            "failure_summary: process.env.FAILURE_SUMMARY ?? ''",
            preview_verification_workflow,
        )
        self.assertIn(
            "error_message=result.error_message",
            preview_verification_workflow,
        )
        self.assertNotIn("payload-fields:", preview_verification_workflow)
        self.assertNotIn("target_id", preview_verification_workflow)
        self.assertNotIn("provider_target", preview_verification_workflow)
        self.assertNotIn("health_url", preview_verification_workflow)
        self.assertNotIn("preview_url", preview_verification_workflow)

        self.assertIn("workflow_call:", stable_verification_workflow)
        self.assertIn(
            "route-path: /v1/drivers/generic-web/stable-verification",
            stable_verification_workflow,
        )
        self.assertIn("generic-web-stable-verification", stable_verification_workflow)
        self.assertIn(
            "payload-file: .launchplane/generic-web-stable-verification-payload.json",
            stable_verification_workflow,
        )
        self.assertIn(
            "DEPLOYMENT_RECORD_ID: ${{ steps.request.outputs.deployment_record_id }}",
            stable_verification_workflow,
        )
        self.assertIn(
            "checked_urls: jsonInput('CHECKED_URLS', [])",
            stable_verification_workflow,
        )
        self.assertIn(
            "health_payload: jsonInput('HEALTH_PAYLOAD', null)",
            stable_verification_workflow,
        )
        self.assertIn(
            "failure_summary: process.env.FAILURE_SUMMARY ?? ''",
            stable_verification_workflow,
        )
        self.assertIn("skipped|neutral", stable_verification_workflow)
        self.assertIn(
            "deployment_health_status=result.deployment_health_status",
            stable_verification_workflow,
        )
        self.assertNotIn("target_id", stable_verification_workflow)
        self.assertNotIn("provider_target", stable_verification_workflow)
        self.assertNotIn("health_url", stable_verification_workflow)
        self.assertNotIn("preview_url", stable_verification_workflow)

        self.assertIn("workflow_call:", preview_feedback_status_workflow)
        self.assertIn("mode:", preview_feedback_status_workflow)
        self.assertIn(
            "uses: ./.github/workflows/reusable-preview-pr-feedback.yml",
            preview_feedback_status_workflow,
        )
        self.assertIn(
            "status: ${{ needs.resolve.outputs.status }}", preview_feedback_status_workflow
        )
        self.assertIn(
            "failure_summary: ${{ needs.resolve.outputs.failure_summary }}",
            preview_feedback_status_workflow,
        )
        self.assertNotIn("route-path", preview_feedback_status_workflow)
        self.assertNotIn("idempotency-key", preview_feedback_status_workflow)

        self.assertIn("Reusable Generic Web Stable Deploy", repo_metadata)
        self.assertIn("Reusable Generic Web Prod Promotion", repo_metadata)
        self.assertIn("Reusable Generic Web Prod Rollback", repo_metadata)
        self.assertIn("Reusable Generic Web Stable Verification", repo_metadata)
        self.assertIn("Reusable Generic Web Preview Lifecycle", repo_metadata)
        self.assertIn("Reusable Generic Web Preview Verification", repo_metadata)
        self.assertIn("using: node24", preview_prepare_action)
        self.assertIn("preview-prepare-client.mjs", preview_prepare_action)

    def test_product_driver_reusable_workflows_keep_route_shaping_in_launchplane(
        self,
    ) -> None:
        product_repo_contract = Path("docs/product-repo-contract.md").read_text(encoding="utf-8")
        stable_deploy_workflow = Path(
            ".github/workflows/reusable-product-driver-stable-deploy.yml"
        ).read_text(encoding="utf-8")
        testing_verification_workflow = Path(
            ".github/workflows/reusable-product-driver-testing-verification.yml"
        ).read_text(encoding="utf-8")
        testing_deploy_workflow = Path(
            ".github/workflows/reusable-product-driver-testing-deploy.yml"
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
        self.assertIn(
            "reusable-product-driver-stable-deploy.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-product-driver-testing-deploy.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-product-driver-prod-promotion.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-product-driver-post-deploy.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn(
            "reusable-product-driver-prod-rollback.yml@<launchplane-sha>",
            product_repo_contract,
        )
        self.assertIn("route path, envelope JSON, output mapping", product_repo_contract)
        self.assertIn("transitional connectors", product_repo_contract)
        self.assertIn("should not own Launchplane route construction", product_repo_contract)
        self.assertIn("preserves explicit driver", product_repo_contract)
        self.assertIn("post-deploy phases", product_repo_contract)
        self.assertIn("must pass\n`driver: odoo`", product_repo_contract)
        self.assertIn(
            "same explicit `product`, `context`, `instance`, and\n`phase`",
            product_repo_contract,
        )
        self.assertIn("keeps the existing VeriReel", product_repo_contract)
        self.assertIn("also supports explicit", product_repo_contract)
        self.assertIn("`driver: odoo` callers", product_repo_contract)
        self.assertIn("explicit `product`, explicit", product_repo_contract)
        self.assertIn(
            "pass explicit `product`, `context`,\n`driver`, stored `artifact_id`, and `source_git_ref`",
            product_repo_contract,
        )
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

        self.assertIn("workflow_call:", testing_verification_workflow)
        self.assertIn("product-driver-testing-verification \\", testing_verification_workflow)
        self.assertIn("RUN_ATTEMPT: ${{ github.run_attempt }}", testing_verification_workflow)
        self.assertIn('"attempt-${RUN_ATTEMPT}"', testing_verification_workflow)
        self.assertIn(
            "route_path=/v1/drivers/verireel/testing-verification",
            testing_verification_workflow,
        )
        self.assertIn(
            "deployment_health_status=result.deployment_health_status",
            testing_verification_workflow,
        )

        self.assertIn("workflow_call:", testing_deploy_workflow)
        self.assertIn(
            "driver:\n        description: Product driver id.\n        required: true",
            testing_deploy_workflow,
        )
        self.assertNotIn("reusable-odoo-testing-deploy.yml", testing_deploy_workflow)
        self.assertIn(
            "route-path: /v1/drivers/odoo/target-replacement-apply",
            testing_deploy_workflow,
        )
        self.assertIn(
            "if: ${{ needs.resolve.outputs.driver == 'odoo' }}",
            testing_deploy_workflow,
        )
        self.assertIn("Unsupported product driver for testing deploy", testing_deploy_workflow)
        self.assertIn(
            "replacement.artifact_id=${{ needs.resolve.outputs.artifact_id }}",
            testing_deploy_workflow,
        )
        self.assertNotIn("default: odoo", testing_deploy_workflow)

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
        self.assertIn("idempotency_scope:", app_maintenance_workflow)
        self.assertIn(
            "IDEMPOTENCY_SCOPE: ${{ inputs.idempotency_scope }}",
            app_maintenance_workflow,
        )
        self.assertIn(
            'idempotency_key="${idempotency_key}:${IDEMPOTENCY_SCOPE}"',
            app_maintenance_workflow,
        )
        self.assertIn("deployment or operation record id", product_repo_contract)
        self.assertIn("checked-in Prisma binary directly", product_repo_contract)
        self.assertIn("post_deploy_status=result.post_deploy_status", app_maintenance_workflow)
        self.assertIn("override_status=result.override_status", app_maintenance_workflow)
        self.assertIn("applied_at=result.applied_at", app_maintenance_workflow)

        self.assertIn("workflow_call:", post_deploy_workflow)
        self.assertIn(
            "driver:\n        description: Product driver id.\n        required: true",
            post_deploy_workflow,
        )
        self.assertNotIn("default: odoo", post_deploy_workflow)
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
