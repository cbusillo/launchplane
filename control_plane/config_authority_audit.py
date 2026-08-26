from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import cast


MAX_SCANNED_FILE_BYTES = 1_000_000
MAX_EVIDENCE_VALUE_LENGTH = 96
HASH_VERSION = "config-authority-audit-v1"

ALLOW_REASON_DOCS_EXAMPLE = "docs_example"
ALLOW_REASON_TEST_FIXTURE = "test_fixture"
ALLOW_REASON_SCHEMA_ONLY = "schema_only"
ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP = "launchplane_self_bootstrap"
ALLOW_REASON_THIN_CONNECTOR_INPUT = "thin_connector_input"
ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT = "operator_supplied_runtime_input"
ALLOW_REASON_PRODUCT_OWNED_ADDON = "product_owned_addon"
ALLOW_REASON_REPO_METADATA_ERGONOMICS = "repo_metadata_ergonomics"

VERIREEL_DOKPLOY_MANAGED_SECRET_BINDINGS = frozenset(
    (
        "BETTER_AUTH_SECRET",
        "VERIREEL_CRON_SECRET",
        "VERIREEL_INDEXNOW_KEY",
        "VERIREEL_INDEXNOW_SUBMIT_SECRET",
        "VERIREEL_SECRETS_MASTER_KEY",
        "VERIREEL_SMOKE_MAINTENANCE_SECRET",
    )
)

SCAN_MODES = ("full-audit", "changed-files-gate")
OUTPUT_FORMATS = ("json", "markdown")
GATE_PROFILES = ("default", "product-repo")

SECRET_SHAPED_KEY_PARTS = frozenset(("PASSWORD", "TOKEN", "SECRET", "KEY"))
BOOTSTRAP_ENV_KEYS = frozenset(
    (
        "DOCKER_IMAGE_REFERENCE",
        "LAUNCHPLANE_APP_ROOT",
        "LAUNCHPLANE_DATABASE_URL",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET",
        "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
        "LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_HOST",
        "LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_RUNTIME_ID",
        "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL",
        "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY",
        "LAUNCHPLANE_POLICY_B64",
        "LAUNCHPLANE_POLICY_FILE",
        "LAUNCHPLANE_POLICY_TOML",
        "LAUNCHPLANE_SERVICE_AUDIENCE",
        "LAUNCHPLANE_SERVICE_HOST",
        "LAUNCHPLANE_SERVICE_PORT",
        "LAUNCHPLANE_STATE_DIR",
        "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT",
        "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL",
    )
)
PRODUCT_REPO_REJECTED_TEST_FIXTURE_RULE_IDS = frozenset(
    (
        "authz_or_operator_authority",
        "provider_target_authority",
    )
)
PRODUCT_REPO_REJECTED_TEST_FIXTURE_KEY_PARTS = frozenset(
    (
        "AUTHZ",
        "CATALOG",
        "COMPOSE_ID",
        "OPERATOR",
        "POLICY",
        "ROUTE_BATCH",
        "ROUTES",
        "SUBJECT",
        "TARGET_ID",
        "TARGETS",
        "TOPOLOGY",
    )
)
PRODUCT_REPO_REJECTED_TEST_FIXTURE_KEY_PHRASES = frozenset(
    (
        ("AUTHZ",),
        ("COMPOSE", "ID"),
        ("MANAGED", "SECRET"),
        ("OPERATOR",),
        ("POLICY",),
        ("PROVIDER", "TARGET"),
        ("ROUTE", "BATCH"),
        ("RUNTIME", "ENVIRONMENT"),
        ("SECRET", "BINDING"),
        ("TARGET", "ID"),
        ("TOPOLOGY",),
    )
)

RUNTIME_IDENTITY_KEY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(^|_)(AUTHZ|OPERATOR|TENANT|PRODUCT|CONTEXT|INSTANCE|LANE|DOMAIN|TARGET|REPO|REPOSITORY|BRANCH|ENVIRONMENT)($|_)",
        r"(^|_)(BASE_URL|HEALTH_URL|PUBLIC_URL|PREVIEW_URL)($|_)",
        r"(^|_)(DOKPLOY|NPMPLUS|ODOO|GITHUB)($|_)",
    )
)
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
OWNER_REPO_PATTERN = re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b")
PROVIDER_TARGET_PATTERN = re.compile(r"\b(?:dokploy|npmplus|provider)[-_]?[A-Za-z0-9_.-]+\b", re.I)
CATALOG_KEY_PATTERN = re.compile(
    r"(?:^|_)(DEFAULT|POLICIES|POLICY|CATALOG|REGISTRY|TARGETS|DOMAINS|LANES|ENVIRONMENTS|REPOSITORIES|REPOS)(?:_|$)",
    re.I,
)
SEMANTIC_FIELD_PATTERN = re.compile(
    r"(?:^|[._\[])(repository|repo|product|tenant|context|instance|branch|domain|lane|target|target_id|provider|operator|subject|authz)(?:$|[.\]])",
    re.I,
)
ENV_ASSIGNMENT_PATTERN = re.compile(
    r"^(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.+?)\s*$"
)
SHELL_ENV_PATTERN = re.compile(
    r"\b(?P<key>[A-Z][A-Z0-9_]{2,})\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s#]+)"
)
YAML_KEY_PATTERN = r"(?:[A-Za-z0-9_.-]+|\"[^\"]+\"|'[^']+')"
YAML_SCALAR_PATTERN = re.compile(rf"^\s*(?P<key>{YAML_KEY_PATTERN})\s*:\s*(?P<value>.+?)\s*$")
YAML_EMPTY_MAPPING_PATTERN = re.compile(rf"^\s*(?P<key>{YAML_KEY_PATTERN})\s*:\s*(?:#.*)?$")
YAML_LIST_ITEM_PATTERN = re.compile(r"^\s*-\s*(?P<value>.+?)\s*$")
YAML_BLOCK_ASSIGNMENT_PATTERN = re.compile(r"^(?P<key>[A-Za-z0-9_.-]+)\s*=\s*(?P<value>.+?)\s*$")
GITHUB_EXPRESSION_PATTERN = re.compile(r"^\$\{\{\s*(?P<body>[^}]+?)\s*\}\}$")
GITHUB_CONTEXT_REFERENCE_PATTERN = re.compile(
    r"^(?:env|github|inputs|matrix|needs|secrets|steps|vars)\.[A-Za-z0-9_.-]+$"
)
GITHUB_DIRECT_INPUT_REFERENCE_PATTERN = re.compile(
    r"^(?:inputs|github\.event\.inputs)\.[A-Za-z0-9_.-]+$"
)
GITHUB_ACTION_INPUT_REFERENCE_PATTERN = re.compile(r"^inputs\.[A-Za-z0-9_.-]+$")
GITHUB_ENV_REFERENCE_PATTERN = re.compile(r"^env\.[A-Za-z0-9_.-]+$")
GITHUB_STEP_OUTPUT_REFERENCE_PATTERN = re.compile(
    r"^steps\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_.-]+$"
)
GITHUB_NEEDS_OUTPUT_REFERENCE_PATTERN = re.compile(
    r"^needs\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_.-]+$"
)
GITHUB_CONTEXT_OR_STEP_OUTPUT_REFERENCE_PATTERN = re.compile(
    r"^(?:github|steps\.[A-Za-z0-9_-]+\.outputs)\.[A-Za-z0-9_.-]+$"
)
GIT_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
GITHUB_ROUTE_PATH_FORWARDING_PATTERN = re.compile(
    r"^(?:inputs\.[A-Za-z0-9_.-]+|steps\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_.-]+)$"
)
GITHUB_INPUT_REFERENCE_PATTERN = re.compile(
    r"^(?:inputs|github\.event\.inputs)\.(?P<input_name>[A-Za-z0-9_.-]+)$"
)
LAUNCHPLANE_REUSABLE_WORKFLOW_PATTERN = re.compile(
    r"^cbusillo/launchplane/\.github/workflows/[A-Za-z0-9_.-]+\.yml@(?:main|[0-9a-f]{40})$"
)
LAUNCHPLANE_CONFIG_AUTHORITY_WORKFLOW_PATH = ".github/workflows/launchplane-config-authority.yml"
LAUNCHPLANE_CONFIG_AUTHORITY_REUSABLE_WORKFLOW_PATTERN = re.compile(
    r"^cbusillo/launchplane/\.github/workflows/"
    r"reusable-product-repo-config-authority\.yml@(?P<revision>[^\s]+)$"
)
LAUNCHPLANE_GENERIC_WEB_PREVIEW_FACADE_PATTERN = re.compile(
    r"^cbusillo/launchplane/\.github/workflows/"
    r"reusable-generic-web-preview\.yml@[0-9a-f]{40}$"
)
LAUNCHPLANE_GENERIC_WEB_PREVIEW_CALLER_WORKFLOW_PATH = ".github/workflows/launchplane-preview.yml"
LAUNCHPLANE_GENERIC_WEB_PREVIEW_FACADE_INPUTS = frozenset(
    ("verification_command", "image_repository")
)
LAUNCHPLANE_DEPENDENCY_HEALTH_ACTION_REFERENCE_PATTERN = re.compile(
    r"^cbusillo/launchplane/\.github/actions/dependency-health-trivy@[^\s]+$"
)
IMMUTABLE_LAUNCHPLANE_DEPENDENCY_HEALTH_ACTION_PATTERN = re.compile(
    r"^cbusillo/launchplane/\.github/actions/dependency-health-trivy@[0-9a-f]{40}$"
)
PINNED_TRIVY_IMAGE_PATTERN = re.compile(
    r"^ghcr\.io/aquasecurity/trivy:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$"
)
WORKFLOW_RUNTIME_AUTHORITY_KEYS = frozenset(
    ("GITHUB_TOKEN", "ID_TOKEN", "LAUNCHPLANE_PRODUCT", "LAUNCHPLANE_URL")
)
WORKFLOW_OPERATOR_INPUT_VALUE_KEYS = frozenset(
    (
        "APP_NAME",
        "CANARY_KEY",
        "CONTEXT",
        "CONFIRMATION",
        "DEPLOY_TIMEOUT_SECONDS",
        "DESCRIPTION",
        "DOMAIN",
        "COMPOSE_PATH",
        "EDGE_ENDPOINT_KEY",
        "ENVIRONMENT_ID",
        "ENVIRONMENT_NAME",
        "EXPECTED_CURRENT_PROVIDER_TARGET_JSON",
        "HEALTHCHECK_PATH",
        "INSTANCE",
        "MODE",
        "OPERATION",
        "PRODUCT",
        "PROJECT_ID",
        "PROJECT_NAME",
        "REASON",
        "REPOSITORY",
        "RUNTIME_PORT",
        "ROUTE_CONTEXT",
        "ROUTE_INSTANCE",
        "SERVER_ID",
        "SOURCE_GIT_REF",
        "SOURCE_TYPE",
        "BASE_BRANCH",
        "TARGET_ID",
        "TARGET_NAME",
        "TARGET_TYPE",
    )
)
WORKFLOW_LAUNCHPLANE_OPERATOR_VAR_KEYS = frozenset(
    (
        "LAUNCHPLANE_CONTEXT",
        "LAUNCHPLANE_INSTANCE",
        "LAUNCHPLANE_PRODUCT",
        "LAUNCHPLANE_PUBLIC_URL",
    )
)
LAUNCHPLANE_SELF_MANAGEMENT_WORKFLOW_PATHS = frozenset(
    (
        ".github/workflows/dokploy-target-setup.yml",
        ".github/workflows/provider-target-operations.yml",
    )
)
WORKFLOW_RESPONSE_SUMMARY_PATH_VALUES = {
    ".github/workflows/product-environment-evidence.yml": {
        "context": frozenset(("$environment_detail.context",)),
        "environment": frozenset(("$environment",)),
        "provider_target_type": frozenset(("$target.provider_target_type",)),
        "status": frozenset(('"blocked"', '"ok"')),
        "target_id_recorded": frozenset(("$target.target_id_recorded",)),
        "target_type": frozenset(("$target.target_type",)),
        "trust_state": frozenset(("$config_status.trust_state",)),
    },
    ".github/workflows/provider-target-operations.yml": {
        "context": frozenset((".result.context",)),
        "instance": frozenset((".result.instance",)),
    },
}
WORKFLOW_BLOCK_MECHANIC_FIELD_PATH_VALUES = {
    ".github/workflows/cleanup-ghcr.yml": {
        "GITHUB_DELETE_TOKEN": frozenset(("${{ secrets.ODOO_GHCR_CLEANUP_TOKEN }}",)),
        "GITHUB_TOKEN": frozenset(("${{ github.token }}",)),
    },
    ".github/workflows/deploy-launchplane.yml": {
        "environment": frozenset(("launchplane-authz-admin", "launchplane-break-glass")),
    },
    ".github/workflows/odoo-driver-route-smoke.yml": {
        "ROUTE_PATHS": frozenset(
            (
                "/v1/drivers/odoo/artifact-publish-inputs "
                "/v1/drivers/odoo/preview-apply-inputs "
                "/v1/drivers/odoo/preview-apply /v1/previews/pr-feedback",
            )
        )
    },
    ".github/workflows/provider-target-operations.yml": {
        "path": frozenset(
            (
                "provider-target-operation-results/*.json",
                "provider-target-routes.json",
            )
        )
    },
    ".github/workflows/reusable-odoo-artifact-publish.yml": {
        "GITHUB_TOKEN": frozenset(("${{ github.token }}",))
    },
}
WORKFLOW_INPUT_MECHANIC_DEFAULT_PATH_VALUES = {
    ".github/workflows/deploy-launchplane.yml": {
        "inputs.omit_every_code_env.default": frozenset(("false",)),
        "inputs.omit_npmplus_env.default": frozenset(("false",)),
        "inputs.omit_owner_agent_env.default": frozenset(("false",)),
        "inputs.omit_terminal_agent_env.default": frozenset(("false",)),
    },
    ".github/workflows/dokploy-target-setup.yml": {
        "inputs.mode.default": frozenset(("dry-run",)),
    },
    ".github/workflows/edge-endpoint-apply.yml": {
        "inputs.mode.default": frozenset(("dry-run",)),
    },
    ".github/workflows/ingress-route-audit-read.yml": {
        "inputs.limit.default": frozenset(("25",)),
    },
    ".github/workflows/live-target-runtime.yml": {
        "inputs.deploy.default": frozenset(("false",)),
        "inputs.mode.default": frozenset(("dry-run",)),
        "inputs.no_cache.default": frozenset(("false",)),
    },
    ".github/workflows/merge-train-policy-import.yml": {
        "inputs.apply.default": frozenset(("false",)),
    },
    ".github/workflows/merge-train-runner.yml": {
        "inputs.batch_candidate_mode.default": frozenset(("none",)),
        "inputs.batch_landing_mode.default": frozenset(("none",)),
        "inputs.mutate.default": frozenset(("false",)),
        "inputs.runner_mode.default": frozenset(("level1",)),
        "inputs.stack_collapse_mode.default": frozenset(("none",)),
    },
    ".github/workflows/odoo-stable-bootstrap.yml": {
        "inputs.verify_canonical.default": frozenset(("true",)),
        "inputs.verify_health.default": frozenset(("true",)),
        "inputs.verify_logo.default": frozenset(("true",)),
    },
    ".github/workflows/odoo-target-replacement-apply.yml": {
        "inputs.allow_empty_data.default": frozenset(("false",)),
        "inputs.no_cache.default": frozenset(("false",)),
        "inputs.verify_canonical.default": frozenset(("true",)),
        "inputs.verify_health.default": frozenset(("true",)),
        "inputs.verify_logo.default": frozenset(("true",)),
    },
    ".github/workflows/odoo-target-replacement-plan.yml": {
        "inputs.allow_empty_data.default": frozenset(("false",)),
    },
    ".github/workflows/preview-lifecycle.yml": {
        "inputs.apply.default": frozenset(("false",)),
    },
    ".github/workflows/product-environment-evidence.yml": {
        "inputs.routes_json.default": frozenset(("[]",)),
        "inputs.target_set.default": frozenset(("configured-json",)),
    },
    ".github/workflows/provider-target-operations.yml": {
        "inputs.mode.default": frozenset(("audit",)),
        "inputs.routes_json.default": frozenset(("[]",)),
        "inputs.target_set.default": frozenset(("single",)),
    },
    ".github/workflows/public-ingress-monitor.yml": {
        "inputs.notify.default": frozenset(("true",)),
        "inputs.timeout_seconds.default": frozenset(("10",)),
    },
    ".github/workflows/reusable-odoo-artifact-publish.yml": {
        "inputs.timeout-ms.default": frozenset(("600000",)),
    },
    ".github/workflows/reusable-odoo-preview.yml": {
        "inputs.runs_on.default": frozenset(('"ubuntu-latest"',)),
        "inputs.tenant_path.default": frozenset(("tenant",)),
        "inputs.timeout-ms.default": frozenset(("660000",)),
        "inputs.wait_for_deploy.default": frozenset(("true",)),
    },
    ".github/workflows/reusable-product-driver-testing-deploy.yml": {
        "inputs.timeout-ms.default": frozenset(("2700000",)),
    },
    ".github/workflows/reusable-generic-web-preview-lifecycle.yml": {
        "inputs.feedback_status.default": frozenset(("unsupported",)),
        "inputs.timeout-ms.default": frozenset(("1800000",)),
        "inputs.timeout-seconds.default": frozenset(("300",)),
    },
    ".github/workflows/reusable-generic-web-prod-rollback.yml": {
        "inputs.backup_required.default": frozenset(("false",)),
        "inputs.instance.default": frozenset(("prod",)),
        "inputs.no_cache.default": frozenset(("false",)),
        "inputs.timeout-ms.default": frozenset(("1800000",)),
        "inputs.timeout-seconds.default": frozenset(("null",)),
        "inputs.verify_health.default": frozenset(("true",)),
    },
    ".github/workflows/reusable-generic-web-preview-verification.yml": {
        "inputs.timeout-ms.default": frozenset(("300000",)),
        "inputs.timeout-seconds.default": frozenset(("null",)),
    },
    ".github/workflows/reusable-generic-web-stable-verification.yml": {
        "inputs.checked_urls.default": frozenset(("[]",)),
        "inputs.health_payload.default": frozenset(("null",)),
        "inputs.instance.default": frozenset(("testing",)),
        "inputs.timeout-ms.default": frozenset(("300000",)),
        "inputs.timeout-seconds.default": frozenset(("null",)),
    },
    ".github/workflows/reusable-preview-feedback-status.yml": {
        "inputs.timeout-ms.default": frozenset(("300000",)),
    },
    ".github/workflows/reusable-product-driver-prod-promotion.yml": {
        "inputs.driver.default": frozenset(("verireel",)),
    },
    ".github/workflows/reusable-product-driver-prod-rollback.yml": {
        "inputs.driver.default": frozenset(("verireel",)),
        "inputs.source_channel.default": frozenset(("testing",)),
    },
    ".github/workflows/runner-host-hygiene.yml": {
        "inputs.action.default": frozenset(("prune_docker_cache",)),
        "inputs.minimum_free_disk_bytes.default": frozenset(("0",)),
        "inputs.mutate.default": frozenset(("false",)),
        "inputs.prune_until.default": frozenset(("168h",)),
        "inputs.timeout_seconds.default": frozenset(("300",)),
    },
    ".github/workflows/runner-lane-registration.yml": {
        "inputs.mutate.default": frozenset(("false",)),
        "inputs.operation.default": frozenset(("register",)),
        "inputs.registration_root.default": frozenset(("auto",)),
    },
    ".github/workflows/tracked-target-logs.yml": {
        "inputs.lines.default": frozenset(("200",)),
        "inputs.source.default": frozenset(("runtime",)),
        "inputs.since.default": frozenset(("1h",)),
    },
}
WORKFLOW_RUNS_ON_MECHANIC_VALUES = frozenset(
    (
        "self-hosted",
        "ubuntu-latest",
        "${{ vars.LAUNCHPLANE_RUNNER_LABEL }}",
    )
)
WORKFLOW_LAUNCHPLANE_URL_REFERENCE_PATH_VALUES = {
    ".github/workflows/odoo-driver-route-smoke.yml": {
        "LAUNCHPLANE_URL": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
    ".github/workflows/preview-lifecycle.yml": {
        "LAUNCHPLANE_URL": frozenset(("${{ vars.LAUNCHPLANE_PREVIEW_LIFECYCLE_URL }}",))
    },
    ".github/workflows/reusable-odoo-artifact-publish.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
    ".github/workflows/reusable-odoo-preview.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
    ".github/workflows/reusable-product-driver-testing-deploy.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        ),
    },
    ".github/workflows/reusable-generic-web-preview-lifecycle.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
    ".github/workflows/reusable-generic-web-prod-rollback.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
    ".github/workflows/reusable-generic-web-preview-verification.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
    ".github/workflows/reusable-generic-web-stable-verification.yml": {
        "launchplane-url": frozenset(
            ("${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",)
        )
    },
}
PRODUCT_DRIVER_REUSABLE_WORKFLOW_PATH_PREFIX = ".github/workflows/reusable-product-driver-"
PRODUCT_DRIVER_REUSABLE_WORKFLOW_PATH_SUFFIX = ".yml"
PRODUCT_DRIVER_REUSABLE_INPUT_DEFAULT_VALUES = frozenset(
    (
        "",
        "5",
        "300",
        "30000",
        "300000",
        "600000",
        "1800000",
        "2400000",
        "2700000",
        "prod",
        "skipped",
        "testing",
    )
)
PRODUCT_DRIVER_REUSABLE_PAYLOAD_FIELD_KEYS = frozenset(
    (
        "backup_gate.backup_record_id",
        "backup_gate.context",
        "backup_gate.instance",
        "deploy.artifact_id",
        "deploy.context",
        "deploy.instance",
        "deploy.source_git_ref",
        "environment.context",
        "environment.instance",
        "maintenance.action",
        "maintenance.application_name",
        "maintenance.context",
        "maintenance.email",
        "maintenance.instance",
        "maintenance.intent",
        "maintenance.preview_slug",
        "maintenance.timeout_seconds",
        "post_deploy.context",
        "post_deploy.instance",
        "post_deploy.phase",
        "product",
        "promotion.artifact_id",
        "promotion.backup_record_id",
        "promotion.context",
        "promotion.expected_build_revision",
        "promotion.expected_build_tag",
        "promotion.from_instance",
        "promotion.promotion_record_id",
        "promotion.source_git_ref",
        "promotion.source_health_status",
        "promotion.to_instance",
        "rollback.backup_record_id",
        "rollback.context",
        "rollback.artifact_id",
        "rollback.expected_build_revision",
        "rollback.expected_build_tag",
        "rollback.instance",
        "rollback.promotion_record_id",
        "rollback.reason",
        "rollback.source_channel",
        "rollback.snapshot_name",
        "run.context",
        "run.request_id",
        "verification.context",
        "verification.deployment_record_id",
        "verification.expected_build_revision",
        "verification.expected_build_tag",
        "verification.instance",
        "verification.interval_seconds",
        "verification.migration_status",
        "verification.owner_routes_status",
        "verification.timeout_seconds",
        "verification.verification_status",
    )
)
PRODUCT_DRIVER_TESTING_DEPLOY_PAYLOAD_FIELD_KEYS = frozenset(
    (
        "product",
        "replacement.artifact_id",
        "replacement.product",
        "replacement.source_git_ref",
    )
)
PRODUCT_DRIVER_REUSABLE_WITH_INPUT_KEYS = frozenset(
    (
        "artifact_id",
        "context",
        "launchplane_audience",
        "launchplane_url",
        "product",
        "source_git_ref",
    )
)
PRODUCT_DRIVER_REUSABLE_WRAPPER_LITERAL_VALUES = {
    ".github/workflows/reusable-product-driver-prod-launch-readiness.yml": {
        "INSTANCE": frozenset(("prod",)),
    },
    ".github/workflows/reusable-product-driver-testing-reset.yml": {
        "ACTION": frozenset(("reset-testing",)),
        "INSTANCE": frozenset(("testing",)),
        "INTENT": frozenset(("stable-testing-reset",)),
    },
    ".github/workflows/reusable-product-driver-testing-deploy.yml": {
        "fail-result-paths": frozenset(('""',)),
        "route-path": frozenset(
            (
                "/v1/drivers/odoo/target-replacement-apply",
                "${{ steps.lp.outputs.poll_url }}",
            )
        ),
    },
}
WORKFLOW_LAUNCHPLANE_BOOTSTRAP_CONTEXT_PATH_VALUES = {
    ".github/workflows/deploy-launchplane.yml": {
        "DEFAULT_GITHUB_TOKEN": frozenset(("${{ secrets.GITHUB_TOKEN }}",)),
        "GHCR_TOKEN": frozenset(("${{ secrets.GHCR_TOKEN }}",)),
        "GHCR_USERNAME": frozenset(("${{ secrets.GHCR_USERNAME }}",)),
        "LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK": frozenset(
            ("${{ vars.LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK }}",)
        ),
        "LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS": frozenset(
            ("${{ vars.LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS }}",)
        ),
        "LAUNCHPLANE_DOKPLOY_TARGET_ID": frozenset(("${{ vars.LAUNCHPLANE_DOKPLOY_TARGET_ID }}",)),
        "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": frozenset(
            ("${{ vars.LAUNCHPLANE_DOKPLOY_TARGET_TYPE }}",)
        ),
        "LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST": frozenset(
            ("${{ secrets.LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST }}",)
        ),
        "LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN": frozenset(
            ("${{ secrets.LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN }}",)
        ),
        "LAUNCHPLANE_GITHUB_CLIENT_ID": frozenset(("${{ vars.LAUNCHPLANE_GITHUB_CLIENT_ID }}",)),
        "LAUNCHPLANE_GITHUB_CLIENT_SECRET": frozenset(
            ("${{ secrets.LAUNCHPLANE_GITHUB_CLIENT_SECRET }}",)
        ),
        "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET": frozenset(
            ("${{ secrets.LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET }}",)
        ),
        "LAUNCHPLANE_IMAGE_REPOSITORY": frozenset(("${{ vars.LAUNCHPLANE_IMAGE_REPOSITORY }}",)),
        "LAUNCHPLANE_NPMPLUS_BASE_URL": frozenset(("${{ vars.LAUNCHPLANE_NPMPLUS_BASE_URL }}",)),
        "LAUNCHPLANE_NPMPLUS_IDENTITY": frozenset(("${{ secrets.LAUNCHPLANE_NPMPLUS_IDENTITY }}",)),
        "LAUNCHPLANE_NPMPLUS_SECRET": frozenset(("${{ secrets.LAUNCHPLANE_NPMPLUS_SECRET }}",)),
        "LAUNCHPLANE_PUBLIC_URL": frozenset(("${{ vars.LAUNCHPLANE_PUBLIC_URL }}",)),
        "LAUNCHPLANE_SERVICE_AUDIENCE": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN": frozenset(
            ("${{ secrets.LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN }}",)
        ),
        "LAUNCHPLANE_SESSION_SECRET": frozenset(("${{ secrets.LAUNCHPLANE_SESSION_SECRET }}",)),
        "LAUNCHPLANE_WORK_GRAPH_GH_TOKEN": frozenset(
            ("${{ secrets.LAUNCHPLANE_WORK_GRAPH_GH_TOKEN }}",)
        ),
    },
}
WORKFLOW_JQ_OPERATOR_FIELD_PATH_KEYS = {
    ".github/workflows/edge-endpoint-apply.yml": frozenset(("endpoint_key",)),
    ".github/workflows/odoo-config-parameter-override.yml": frozenset(("key",)),
    ".github/workflows/provider-target-operations.yml": frozenset(
        ("context", "instance", "provider_id")
    ),
}
WORKFLOW_OPERATOR_INPUT_REFERENCE_PATH_VALUES = {
    ".github/workflows/deploy-launchplane.yml": {
        "BREAK_GLASS_CONFIRM": frozenset(("${{ inputs.break_glass_confirm }}",)),
        "BREAK_GLASS_IMAGE_REFERENCE": frozenset(("${{ inputs.break_glass_image_reference }}",)),
        "BREAK_GLASS_REASON": frozenset(("${{ inputs.break_glass_reason }}",)),
        "DEPLOY_GIT_REF": frozenset(("${{ inputs.git_ref }}",)),
        "DEPLOY_IMAGE_REFERENCE": frozenset(("${{ inputs.image_reference }}",)),
        "OMIT_EVERY_CODE_ENV": frozenset(
            (
                "${{ inputs.omit_every_code_env }}",
                "${{ inputs.omit_every_code_env || false }}",
            )
        ),
        "OMIT_NPMPLUS_ENV": frozenset(
            (
                "${{ inputs.omit_npmplus_env }}",
                "${{ inputs.omit_npmplus_env || false }}",
            )
        ),
        "OMIT_OWNER_AGENT_ENV": frozenset(
            (
                "${{ inputs.omit_owner_agent_env }}",
                "${{ inputs.omit_owner_agent_env || false }}",
            )
        ),
        "OMIT_TERMINAL_AGENT_ENV": frozenset(
            (
                "${{ inputs.omit_terminal_agent_env }}",
                "${{ inputs.omit_terminal_agent_env || false }}",
            )
        ),
    },
    ".github/workflows/edge-endpoint-apply.yml": {
        "ENDPOINT_KEY": frozenset(("${{ inputs.endpoint_key }}",)),
        "IDEMPOTENCY_KEY": frozenset(("${{ inputs.idempotency_key }}",)),
        "idempotency-key": frozenset(("${{ inputs.idempotency_key }}",)),
    },
    ".github/workflows/ingress-route-apply.yml": {
        "IDEMPOTENCY_KEY": frozenset(("${{ inputs.idempotency_key }}",)),
        "idempotency-key": frozenset(("${{ inputs.idempotency_key }}",)),
    },
    ".github/workflows/ingress-route-canary-apply.yml": {
        "IDEMPOTENCY_KEY": frozenset(("${{ inputs.idempotency_key }}",)),
        "idempotency-key": frozenset(("${{ inputs.idempotency_key }}",)),
    },
    ".github/workflows/merge-train-policy-import.yml": {
        "POLICY_BLOCKED_LABEL": frozenset(("${{ inputs.blocked_label }}",)),
        "POLICY_BASE_BRANCH": frozenset(("${{ inputs.base_branch }}",)),
        "POLICY_ENQUEUE_LABEL": frozenset(("${{ inputs.enqueue_label }}",)),
        "POLICY_FAILURE_POLICY": frozenset(("${{ inputs.failure_policy }}",)),
        "POLICY_MERGE_METHOD": frozenset(("${{ inputs.merge_method }}",)),
        "POLICY_REASON": frozenset(("${{ inputs.reason }}",)),
        "POLICY_REPOSITORY": frozenset(("${{ inputs.repository }}",)),
        "POLICY_SOURCE_LABEL": frozenset(("${{ inputs.source_label }}",)),
        "POLICY_STACK_CHILD_DISPOSITION_LABEL": frozenset(
            ("${{ inputs.stack_child_disposition_label }}",)
        ),
    },
    ".github/workflows/merge-train-runner.yml": {
        "REQUESTED_REPOSITORY": frozenset(("${{ inputs.repository }}",)),
        "REQUESTED_BASE_BRANCH": frozenset(("${{ inputs.base_branch }}",)),
    },
    ".github/workflows/product-environment-evidence.yml": {
        "ENVIRONMENT": frozenset(("${{ matrix.route.environment }}",)),
        "PROVIDER_ID": frozenset(("${{ inputs.provider_id }}",)),
        "PRODUCT": frozenset(("${{ matrix.route.product }}",)),
        "TARGET_SET": frozenset(("${{ inputs.target_set }}",)),
    },
    ".github/workflows/provider-target-operations.yml": {
        "PROVIDER_ID": frozenset(("${{ inputs.provider_id }}",)),
        "ROUTE_CONTEXT": frozenset(("${{ matrix.route.context }}",)),
        "ROUTE_INSTANCE": frozenset(("${{ matrix.route.instance }}",)),
        "TARGET_SET": frozenset(("${{ inputs.target_set }}",)),
    },
    ".github/workflows/reusable-odoo-artifact-publish.yml": {
        "CONTEXT_NAME": frozenset(("${{ inputs.context }}",)),
        "INPUT_PRODUCT": frozenset(("${{ inputs.product }}",)),
        "INSTANCE_NAME": frozenset(("${{ inputs.instance }}",)),
    },
    ".github/workflows/reusable-odoo-preview.yml": {
        "INPUT_CONTEXT": frozenset(("${{ inputs.context }}",)),
        "INPUT_PRODUCT": frozenset(("${{ inputs.product }}",)),
        "INPUT_TENANT_PATH": frozenset(("${{ inputs.tenant_path }}",)),
        "INPUT_TENANT_REPOSITORY": frozenset(("${{ inputs.tenant_repository }}",)),
        "SOURCE_ACCESS_PROBE_REPOSITORY": frozenset(
            ("${{ inputs.source_access_probe_repository }}",)
        ),
    },
    ".github/workflows/reusable-generic-web-preview-lifecycle.yml": {
        "ANCHOR_PR_NUMBER": frozenset(("${{ inputs.anchor_pr_number }}",)),
        "CONTEXT": frozenset(("${{ inputs.context }}",)),
        "DESTROY_REASON": frozenset(("${{ inputs.destroy_reason }}",)),
        "GITHUB_EVENT_NAME": frozenset(("${{ github.event_name }}",)),
        "IMAGE_REFERENCE": frozenset(("${{ inputs.image_reference }}",)),
        "OPERATION": frozenset(("${{ inputs.operation }}",)),
        "PRODUCT": frozenset(("${{ inputs.product }}",)),
        "TIMEOUT_SECONDS": frozenset(("${{ inputs['timeout-seconds'] }}",)),
    },
    ".github/workflows/odoo-config-parameter-override.yml": {
        "CONTEXT_NAME": frozenset(("${{ inputs.context }}",)),
        "KEY_NAME": frozenset(("${{ inputs.key }}",)),
    },
    ".github/workflows/odoo-driver-route-smoke.yml": {
        "CONTEXT_NAME": frozenset(("${{ inputs.context }}",))
    },
    ".github/workflows/odoo-website-bootstrap-override.yml": {
        "CONTEXT_NAME": frozenset(("${{ inputs.context }}",))
    },
    ".github/workflows/runner-lane-registration.yml": {
        "AUDIT_RECORD_KEY": frozenset(("${{ inputs.audit_record_key }}",)),
        "LANE_NAME": frozenset(("${{ inputs.lane_name }}",)),
        "OPERATION": frozenset(("${{ inputs.operation }}",)),
        "RUNNER_REGISTRATION_EXECUTION_LANE": frozenset(
            ("${{ vars.LAUNCHPLANE_RUNNER_HOST_HYGIENE_EXECUTION_LANE }}",)
        ),
        "TARGET_REPOSITORY": frozenset(("${{ inputs.repository }}",)),
    },
    ".github/workflows/runner-host-hygiene.yml": {
        "RUNNER_EXECUTION_LANE": frozenset(
            ("${{ vars.LAUNCHPLANE_RUNNER_HOST_HYGIENE_EXECUTION_LANE }}",)
        ),
    },
}
WORKFLOW_OPERATOR_VARIABLE_FORWARD_PATHS = frozenset(
    (
        ".github/workflows/dokploy-target-setup.yml",
        ".github/workflows/ingress-route-apply.yml",
        ".github/workflows/ingress-route-canary-apply.yml",
        ".github/workflows/ingress-route-dry-run.yml",
        ".github/workflows/live-target-runtime.yml",
        ".github/workflows/merge-train-runner.yml",
        ".github/workflows/odoo-config-parameter-override.yml",
        ".github/workflows/odoo-stable-bootstrap.yml",
        ".github/workflows/odoo-target-replacement-apply.yml",
        ".github/workflows/odoo-target-replacement-plan.yml",
        ".github/workflows/odoo-website-bootstrap-override.yml",
        ".github/workflows/product-environment-evidence.yml",
        ".github/workflows/tracked-target-logs.yml",
    )
)
WORKFLOW_SERVICE_ENV_PAYLOAD_PATH_VALUES = {
    ".github/workflows/deploy-launchplane.yml": {
        "GH_TOKEN": frozenset(("$work_graph_gh_token",)),
        "LAUNCHPLANE_GITHUB_CLIENT_ID": frozenset(("$github_client_id",)),
        "LAUNCHPLANE_GITHUB_CLIENT_SECRET": frozenset(("$github_client_secret",)),
        "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET": frozenset(
            ("$manager_preview_webhook_secret",)
        ),
        "LAUNCHPLANE_PUBLIC_URL": frozenset(("$public_url",)),
        "LAUNCHPLANE_SESSION_SECRET": frozenset(("$session_secret",)),
    },
}
WORKFLOW_THIN_CONNECTOR_PATH_VALUES = {
    ".github/workflows/ci.yml": {
        "context": frozenset((".",)),
        "password": frozenset(("${{ github.token }}",)),
    },
    ".github/workflows/deploy-launchplane.yml": {
        "audience": frozenset(
            (
                "${{ env.LAUNCHPLANE_SERVICE_AUDIENCE }}",
                "${{ steps.service.outputs.service_audience }}",
            )
        ),
        "context": frozenset((".",)),
        "expected-status": frozenset(('"200"', '"200,202"')),
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(
            (
                "launchplane-self-deploy:${{ steps.image.outputs.image_reference }}:${{ "
                "github.run_id }}:${{ github.run_attempt }}:db-authz",
                "${{ steps.rollback_request.outputs.idempotency_key }}",
            )
        ),
        "launchplane-url": frozenset(("${{ env.LAUNCHPLANE_PUBLIC_URL }}",)),
        "log-response-body": frozenset(('"false"',)),
        "method": frozenset(("GET",)),
        "payload-file": frozenset(
            (
                "${{ steps.self_deploy.outputs.payload_file }}",
                "${{ steps.rollback_request.outputs.payload_file }}",
            )
        ),
        "poll-interval-ms": frozenset(('"1000"', '"5000"')),
        "poll-retry-on-request-error": frozenset(('"true"',)),
        "poll-retry-on-unexpected-status": frozenset(('"true"',)),
        "poll-timeout-ms": frozenset(
            (
                "${{ steps.deploy_wait_timeout.outputs.timeout_ms }}",
                "${{ steps.deployed_health.outputs.remaining_timeout_ms }}",
                "${{ steps.rollback_wait_timeout.outputs.timeout_ms }}",
                "${{ steps.rollback_health.outputs.remaining_timeout_ms }}",
            )
        ),
        "poll-until-path": frozenset(
            ("runtime.deployment_marker", "runtime.docker_image_reference")
        ),
        "poll-until-value": frozenset(
            (
                "${{ steps.image.outputs.image_reference }}",
                "${{ steps.prep.outputs.forward_deployment_marker }}",
                "${{ steps.rollback_request.outputs.deployment_marker }}",
                "${{ steps.rollback_request.outputs.previous_image_reference }}",
            )
        ),
        "response-output-file": frozenset(
            (
                "${{ runner.temp }}/launchplane-deployed-marker-final.json",
                "${{ runner.temp }}/launchplane-deployed-marker-wait.json",
                "${{ runner.temp }}/launchplane-deployed-runtime-wait.json",
                "${{ runner.temp }}/launchplane-deployed-runtime-final.json",
                "${{ runner.temp }}/launchplane-previous-runtime.json",
                "${{ runner.temp }}/launchplane-rollback-marker-final.json",
                "${{ runner.temp }}/launchplane-rollback-marker-wait.json",
                "${{ runner.temp }}/launchplane-rollback-runtime-final.json",
                "${{ runner.temp }}/launchplane-rollback-runtime-wait.json",
                "${{ runner.temp }}/launchplane-runtime-smoke.json",
                "${{ runner.temp }}/launchplane-self-deploy-response.json",
                "${{ runner.temp }}/launchplane-self-deploy-rollback-response.json",
            )
        ),
        "route-path": frozenset(("/v1/service/runtime", "/v1/drivers/launchplane/self-deploy")),
        "timeout-ms": frozenset(('"30000"',)),
    },
    ".github/workflows/launchplane-config-authority.yml": {
        "uses": frozenset(
            (
                "cbusillo/launchplane/.github/workflows/"
                "reusable-product-repo-config-authority.yml@main",
            )
        ),
    },
    ".github/workflows/odoo-driver-route-smoke.yml": {
        "IMAGE_REPOSITORY": frozenset(("${{ steps.publish_inputs.outputs.image_repository }}",)),
        "idempotency-key": frozenset(
            (
                "odoo-driver-route-smoke:${{ env.PRODUCT }}:${{ env.CONTEXT_NAME }}:${{ "
                "env.INSTANCE }}:run-${{ github.run_id }}-attempt-${{ github.run_attempt }}",
                "odoo-driver-route-smoke:preview-apply-inputs:${{ "
                "github.run_id }}:${{ github.run_attempt }}",
                "odoo-driver-route-smoke:preview-apply:${{ "
                "github.run_id }}:${{ github.run_attempt }}",
                "odoo-driver-route-smoke:preview-pr-feedback:${{ "
                "github.run_id }}:${{ github.run_attempt }}",
            )
        ),
        "odoo-driver-route-smoke": frozenset(
            (
                "${{ env.PRODUCT }}:${{",
                "${{ env.PRODUCT }}:${{ env.CONTEXT_NAME }}",
            )
        ),
    },
    ".github/workflows/dokploy-target-setup.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset(("dokploy-target-setup-payload.json",)),
        "response-output-file": frozenset(("dokploy-target-setup.json",)),
    },
    ".github/workflows/dokploy-target-inspect.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "method": frozenset(("GET",)),
        "response-output-file": frozenset(("dokploy-target-inspect-response.json",)),
        "route-path": frozenset(("${{ steps.request.outputs.route_path }}",)),
    },
    ".github/workflows/ingress-route-audit-read.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "log-response-body": frozenset(('"false"',)),
        "method": frozenset(("GET",)),
        "response-output-file": frozenset(("ingress-route-audit-read-raw.json",)),
        "route-path": frozenset(("${{ steps.route.outputs.route_path }}",)),
    },
    ".github/workflows/live-target-runtime.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "log-response-body": frozenset(('"false"',)),
        "payload-file": frozenset(("${{ steps.request.outputs.payload_file }}",)),
        "response-output-file": frozenset(("${{ steps.request.outputs.response_file }}",)),
        "route-path": frozenset(("/v1/live-target-runtime/apply",)),
    },
    ".github/workflows/merge-train-policy-import.yml": {
        "audience": frozenset(("${{ steps.service.outputs.audience }}",)),
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(("${{ steps.apply_policy.outputs.idempotency_key }}",)),
        "log-response-body": frozenset(('"false"',)),
        "method": frozenset(("POST",)),
        "payload-file": frozenset(
            (
                "${{ steps.apply_policy.outputs.payload }}",
                "${{ steps.policy.outputs.dry_run_payload }}",
            )
        ),
        "response-output-file": frozenset(
            ("merge-train-policy-apply.json", "merge-train-policy-dry-run.json")
        ),
        "route-path": frozenset(("/v1/merge-train/policies/import",)),
    },
    ".github/workflows/merge-train-runner.yml": {
        "audience": frozenset(
            (
                "${{ steps.admission_request.outputs.service_audience }}",
                "${{ steps.scheduled_target_request.outputs.service_audience }}",
                "${{ steps.admission.outputs.service_audience }}",
            )
        ),
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(
            (
                "${{ steps.batch_candidate_request.outputs.idempotency_key }}",
                "${{ steps.batch_landing_request.outputs.idempotency_key }}",
                "${{ steps.controller_request.outputs.idempotency_key }}",
                "${{ steps.level1_request.outputs.idempotency_key }}",
                "${{ steps.stack_collapse_request.outputs.idempotency_key }}",
            )
        ),
        "idempotency-key-prefix": frozenset(
            (
                "${{ steps.controller_summary.outputs.feedback_idempotency_key_prefix }}",
                "${{ steps.manual_phase_feedback.outputs.feedback_idempotency_key_prefix }}",
            )
        ),
        "log-response-body": frozenset(('"false"',)),
        "method": frozenset(("GET", "POST")),
        "payload-file": frozenset(
            (
                "${{ steps.batch_candidate_request.outputs.payload_file }}",
                "${{ steps.batch_landing_request.outputs.payload_file }}",
                "${{ steps.controller_request.outputs.payload_file }}",
                "${{ steps.level1_request.outputs.payload_file }}",
                "${{ steps.stack_collapse_request.outputs.payload_file }}",
            )
        ),
        "payload-list-file": frozenset(
            (
                "${{ steps.controller_summary.outputs.feedback_payloads }}",
                "${{ steps.manual_phase_feedback.outputs.feedback_payloads }}",
            )
        ),
        "response-output-file": frozenset(
            (
                "${{ steps.admission_request.outputs.response_file }}",
                "${{ steps.batch_candidate_request.outputs.response_file }}",
                "${{ steps.batch_landing_request.outputs.response_file }}",
                "${{ steps.controller_request.outputs.response_file }}",
                "${{ steps.controller_summary.outputs.feedback_response }}",
                "${{ steps.level1_request.outputs.response_file }}",
                "${{ steps.manual_phase_feedback.outputs.feedback_response }}",
                "${{ steps.scheduled_target_request.outputs.response_file }}",
                "${{ steps.stack_collapse_request.outputs.response_file }}",
            )
        ),
        "route-path": frozenset(
            (
                "/v1/work-graph/merge-train/batch-candidate/run-once",
                "/v1/work-graph/merge-train/batch-landing/run-once",
                "/v1/work-graph/merge-train/controller/run-once",
                "/v1/work-graph/merge-train/pr-feedback",
                "/v1/work-graph/merge-train/run-once",
                "/v1/work-graph/merge-train/policy-targets",
                "/v1/work-graph/merge-train/stack-collapse/run-once",
                "${{ steps.admission_request.outputs.route_path }}",
            )
        ),
    },
    ".github/workflows/preview-lifecycle.yml": {
        "audience": frozenset(("${{ env.LAUNCHPLANE_AUDIENCE }}",)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset(("${{ steps.request.outputs.request_file }}",)),
        "response-output-file": frozenset(("launchplane-preview-lifecycle-sweep-response.json",)),
        "route-path": frozenset(("/v1/previews/lifecycle-sweep",)),
    },
    ".github/workflows/product-environment-evidence.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "log-response-body": frozenset(('"false"',)),
        "method": frozenset(("GET",)),
        "response-output-file": frozenset(
            (
                "${{ steps.request.outputs.config_status_response_file }}",
                "${{ steps.request.outputs.environment_response_file }}",
            )
        ),
        "route-path": frozenset(
            (
                "${{ steps.request.outputs.config_status_route }}",
                "${{ steps.request.outputs.environment_route }}",
            )
        ),
    },
    ".github/workflows/product-onboarding.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(("${{ steps.onboarding.outputs.idempotency_key }}",)),
        "payload-file": frozenset(("${{ steps.onboarding.outputs.request_file }}",)),
        "response-output-file": frozenset(("product-onboarding.json",)),
    },
    ".github/workflows/provider-target-operations.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(("result.operation_status",)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset(("${{ steps.request.outputs.payload_file }}",)),
        "response-output-file": frozenset(("${{ steps.request.outputs.response_file }}",)),
    },
    ".github/workflows/work-graph-snapshot-validate.yml": {
        "audience": frozenset(("${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",)),
        "fail-result-paths": frozenset(('""',)),
        "method": frozenset(("GET",)),
        "response-output-file": frozenset(("launchplane-work-graph-snapshot.json",)),
    },
    ".github/workflows/odoo-config-parameter-override.yml": {
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset((".launchplane/odoo-config-parameter-override-payload.json",)),
    },
    ".github/workflows/odoo-stable-bootstrap.yml": {
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset((".launchplane/odoo-stable-bootstrap-payload.json",)),
        "route-path": frozenset(("${{ steps.create_bootstrap.outputs.poll_url }}",)),
    },
    ".github/workflows/odoo-target-replacement-apply.yml": {
        "fail-result-paths": frozenset(('""',)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset((".launchplane/odoo-target-replacement-apply-payload.json",)),
        "route-path": frozenset(("${{ steps.create_replacement.outputs.poll_url }}",)),
    },
    ".github/workflows/odoo-target-replacement-plan.yml": {
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset((".launchplane/odoo-target-replacement-plan-payload.json",)),
    },
    ".github/workflows/odoo-website-bootstrap-override.yml": {
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "payload-file": frozenset((".launchplane/odoo-website-bootstrap-override-payload.json",)),
    },
    ".github/workflows/reusable-odoo-website-bootstrap-override.yml": {
        "CONTEXT_NAME": frozenset(("${{ inputs.context }}",)),
        "idempotency-key": frozenset(("${{ steps.payload.outputs.idempotency_key }}",)),
        "path": frozenset(
            (
                ".launchplane/odoo-website-bootstrap-override-evidence.json "
                "odoo-website-bootstrap-override.json",
            )
        ),
        "payload-file": frozenset((".launchplane/odoo-website-bootstrap-override-payload.json",)),
    },
    ".github/workflows/reusable-odoo-artifact-publish.yml": {
        "EXPECTED_PRODUCT_REPOSITORY": frozenset(("${{ inputs.product_repository }}",)),
        "GITHUB_TOKEN": frozenset(("${{ github.token }}",)),
        "GHCR_TOKEN": frozenset(("${{ secrets.ODOO_GHCR_PUBLISH_TOKEN }}",)),
        "GHCR_USERNAME": frozenset(("${{ github.repository_owner }}",)),
        "ODOO_SOURCE_GITHUB_TOKEN": frozenset(
            ("${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || github.token }}",)
        ),
        "RESOLVED_DEVKIT_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.devkit_repository }}",)
        ),
        "RESOLVED_IMAGE_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.image_repository }}",)
        ),
        "RESOLVED_PRODUCT_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.repository }}",)
        ),
        "RESOLVED_SHARED_ADDONS_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.shared_addons_repository }}",)
        ),
        "idempotency-key": frozenset(
            (
                "${{ steps.product.outputs.publish_idempotency_key }}",
                "${{ steps.product.outputs.publish_inputs_idempotency_key }}",
            )
        ),
        "password": frozenset(("${{ secrets.ODOO_GHCR_PUBLISH_TOKEN }}",)),
        "repository": frozenset(
            (
                "${{ steps.publish_inputs.outputs.devkit_repository }}",
                "${{ steps.publish_inputs.outputs.shared_addons_repository }}",
                "${{ steps.publish_inputs.outputs.repository }}",
            )
        ),
        "token": frozenset(("${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || github.token }}",)),
        "username": frozenset(("${{ github.repository_owner }}",)),
    },
    ".github/workflows/reusable-odoo-preview.yml": {
        "DEFAULT_REPOSITORY": frozenset(("${{ github.repository }}",)),
        "GHCR_TOKEN": frozenset(("${{ secrets.ODOO_GHCR_PUBLISH_TOKEN }}",)),
        "GHCR_USERNAME": frozenset(("${{ github.repository_owner }}",)),
        "GITHUB_TOKEN": frozenset(("${{ secrets.ODOO_SOURCE_GITHUB_TOKEN }}",)),
        "IMAGE_REPOSITORY": frozenset(("${{ steps.publish_inputs.outputs.image_repository }}",)),
        "ODOO_GHCR_PUBLISH_TOKEN": frozenset(("${{ secrets.ODOO_GHCR_PUBLISH_TOKEN }}",)),
        "ODOO_SOURCE_GITHUB_TOKEN": frozenset(("${{ secrets.ODOO_SOURCE_GITHUB_TOKEN }}",)),
        "RESOLVED_DEVKIT_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.devkit_repository }}",)
        ),
        "RESOLVED_IMAGE_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.image_repository }}",)
        ),
        "RESOLVED_SHARED_ADDONS_REPOSITORY": frozenset(
            ("${{ steps.publish_inputs.outputs.shared_addons_repository }}",)
        ),
        "checkout.repository[1]": frozenset(("${{ steps.facts.outputs.tenant_repository }}",)),
        "checkout.repository[2]": frozenset(
            ("${{ steps.publish_inputs.outputs.devkit_repository }}",)
        ),
        "checkout.repository[3]": frozenset(
            ("${{ steps.publish_inputs.outputs.shared_addons_repository }}",)
        ),
        "idempotency-key": frozenset(
            (
                "${{ steps.dry_run_request.outputs.idempotency-key }}",
                "${{ steps.launchplane_request.outputs.idempotency-key }}",
                "${{ steps.publish_inputs_request.outputs.idempotency-key }}",
            )
        ),
        "launchplane_url": frozenset(
            (
                "${{ inputs.launchplane_url }}",
                "${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",
            )
        ),
        "password": frozenset(("${{ secrets.ODOO_GHCR_PUBLISH_TOKEN }}",)),
        "preview_url": frozenset(
            (
                "${{ needs.preview-refresh.outputs.preview_url }}",
                "${{ steps.launchplane.outputs.preview_url }}",
            )
        ),
        "run_url": frozenset(
            ("${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",)
        ),
        "runs-on": frozenset(("${{ fromJSON(inputs.runs_on) }}",)),
        "token": frozenset(("${{ secrets.ODOO_SOURCE_GITHUB_TOKEN }}",)),
        "username": frozenset(("${{ github.repository_owner }}",)),
    },
    ".github/workflows/reusable-preview-feedback-status.yml": {
        "launchplane_url": frozenset(("${{ inputs.launchplane_url }}",)),
        "preview_url": frozenset(("${{ inputs.preview_url }}",)),
    },
    ".github/workflows/reusable-generic-web-preview-lifecycle.yml": {
        "anchor_pr_number": frozenset(
            (
                "${{ needs.resolve.outputs.anchor_pr_number }}",
                "${{ steps.request.outputs.anchor_pr_number }}",
            )
        ),
        "anchor_repo": frozenset(("${{ github.repository }}",)),
        "context": frozenset(
            (
                "${{ needs.resolve.outputs.context }}",
                "${{ steps.request.outputs.context }}",
            )
        ),
        "destroy.anchor_pr_number": frozenset(("${{ needs.resolve.outputs.anchor_pr_number }}",)),
        "destroy.destroy_reason": frozenset(("${{ needs.resolve.outputs.destroy_reason }}",)),
        "destroy.product": frozenset(("${{ needs.resolve.outputs.product }}",)),
        "destroy.timeout_seconds": frozenset(("${{ inputs['timeout-seconds'] }}",)),
        "destroy_reason": frozenset(("${{ steps.request.outputs.destroy_reason }}",)),
        "idempotency-key": frozenset(("${{ needs.resolve.outputs.idempotency_key }}",)),
        "idempotency_key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "preview_slug": frozenset(("${{ steps.lp.outputs.preview_slug }}",)),
        "preview_url": frozenset(("${{ steps.lp.outputs.preview_url }}",)),
        "product": frozenset(
            (
                "${{ needs.resolve.outputs.product }}",
                "${{ steps.request.outputs.product }}",
            )
        ),
        "refresh.anchor_pr_number": frozenset(("${{ needs.resolve.outputs.anchor_pr_number }}",)),
        "refresh.source": frozenset(("${{ needs.resolve.outputs.run_url }}",)),
        "refresh.product": frozenset(("${{ needs.resolve.outputs.product }}",)),
        "refresh.timeout_seconds": frozenset(("${{ inputs['timeout-seconds'] }}",)),
        "repository": frozenset(("${{ github.repository }}",)),
        "run_url": frozenset(
            (
                "${{ needs.resolve.outputs.run_url }}",
                "${{ steps.request.outputs.run_url }}",
            )
        ),
        "source": frozenset(("${{ needs.resolve.outputs.run_url }}",)),
    },
    ".github/workflows/reusable-generic-web-prod-rollback.yml": {
        "backup_record_id": frozenset(("${{ steps.request.outputs.backup_record_id }}",)),
        "deployment_record_id": frozenset(("${{ steps.lp.outputs.deployment_record_id }}",)),
        "deploy_status": frozenset(("${{ steps.lp.outputs.deploy_status }}",)),
        "generic_web_rollback_plan_id": frozenset(
            ("${{ steps.lp.outputs.generic_web_rollback_plan_id }}",)
        ),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "idempotency_key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "instance": frozenset(("${{ steps.request.outputs.instance }}",)),
        "post_deploy_status": frozenset(("${{ steps.lp.outputs.post_deploy_status }}",)),
        "product": frozenset(("${{ steps.request.outputs.product }}",)),
        "rollback_deployment_record_id": frozenset(
            ("${{ steps.request.outputs.rollback_deployment_record_id }}",)
        ),
        "rollback_status": frozenset(("${{ steps.lp.outputs.rollback_status }}",)),
    },
    ".github/workflows/reusable-generic-web-preview-verification.yml": {
        "anchor_pr_number": frozenset(("${{ steps.request.outputs.anchor_pr_number }}",)),
        "anchor_repo": frozenset(("${{ steps.request.outputs.anchor_repo }}",)),
        "context": frozenset(("${{ inputs.context }}",)),
        "error_message": frozenset(("${{ steps.lp.outputs.error_message }}",)),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "idempotency_key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "product": frozenset(("${{ steps.request.outputs.product }}",)),
        "verification_status": frozenset(("${{ steps.lp.outputs.verification_status }}",)),
        "verified_at": frozenset(("${{ steps.request.outputs.verified_at }}",)),
    },
    ".github/workflows/reusable-generic-web-stable-verification.yml": {
        "context": frozenset(("${{ steps.request.outputs.context }}",)),
        "deployment_health_status": frozenset(
            ("${{ steps.lp.outputs.deployment_health_status }}",)
        ),
        "deployment_record_id": frozenset(
            (
                "${{ steps.lp.outputs.deployment_record_id }}",
                "${{ steps.request.outputs.deployment_record_id }}",
            )
        ),
        "idempotency-key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "idempotency_key": frozenset(("${{ steps.request.outputs.idempotency_key }}",)),
        "instance": frozenset(("${{ steps.request.outputs.instance }}",)),
        "inventory_record_id": frozenset(("${{ steps.lp.outputs.inventory_record_id }}",)),
        "product": frozenset(("${{ steps.request.outputs.product }}",)),
        "promotion_health_status": frozenset(("${{ steps.lp.outputs.promotion_health_status }}",)),
        "promotion_record_id": frozenset(
            (
                "${{ steps.lp.outputs.promotion_record_id }}",
                "${{ steps.request.outputs.promotion_record_id }}",
            )
        ),
        "verification_status": frozenset(("${{ steps.request.outputs.verification_status }}",)),
        "verified_at": frozenset(("${{ steps.request.outputs.verified_at }}",)),
    },
    ".github/workflows/public-ingress-monitor.yml": {
        "idempotency-key": frozenset(
            ("public-ingress-monitor:${{ github.run_id }}:${{ github.run_attempt }}",)
        )
    },
    ".github/workflows/runner-host-hygiene.yml": {
        "GH_TOKEN": frozenset(("${{ steps.github-read-token.outputs.token }}",)),
        "RUNNER_REPOSITORY_SCOPE": frozenset(("${{ github.repository }}",)),
    },
    ".github/workflows/runner-lane-registration.yml": {
        "GH_TOKEN": frozenset(("${{ secrets.LAUNCHPLANE_RUNNER_REGISTRATION_GITHUB_TOKEN }}",))
    },
}
PYTHON_SCHEMA_ONLY_PATH_KEY_VALUES = {
    "control_plane/workflows/odoo_artifact_publish.py": {
        "DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY": frozenset(
            ("ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON",)
        ),
        "PUBLISH_DEPENDENCY_REPOSITORY_KEYS.devkit_repository": frozenset(
            ("ODOO_DEVKIT_REPOSITORY",)
        ),
        "PUBLISH_DEPENDENCY_REPOSITORY_KEYS.shared_addons_repository": frozenset(
            ("ODOO_SHARED_ADDONS_REPOSITORY",)
        ),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[0]": frozenset(("ODOO_VERSION",)),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[1]": frozenset(("ODOO_BASE_RUNTIME_IMAGE",)),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[2]": frozenset(("ODOO_BASE_DEVTOOLS_IMAGE",)),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[3]": frozenset(("ODOO_ADDON_REPOSITORIES",)),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[4]": frozenset(("OPENUPGRADE_ADDON_REPOSITORY",)),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[5]": frozenset(("OPENUPGRADELIB_INSTALL_SPEC",)),
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS[6]": frozenset(("ODOO_PYTHON_SYNC_SKIP_ADDONS",)),
        "instance": frozenset(("testing",)),
    },
}
PYTHON_SCHEMA_ONLY_PATH_CONTAINER_VALUES = {
    "control_plane/workflows/odoo_artifact_publish.py": {
        "PUBLISH_DEPENDENCY_REPOSITORY_KEYS": {
            "devkit_repository": "ODOO_DEVKIT_REPOSITORY",
            "shared_addons_repository": "ODOO_SHARED_ADDONS_REPOSITORY",
        },
        "PUBLISH_RUNTIME_ENVIRONMENT_KEYS": (
            "ODOO_VERSION",
            "ODOO_BASE_RUNTIME_IMAGE",
            "ODOO_BASE_DEVTOOLS_IMAGE",
            "ODOO_ADDON_REPOSITORIES",
            "OPENUPGRADE_ADDON_REPOSITORY",
            "OPENUPGRADELIB_INSTALL_SPEC",
            "ODOO_PYTHON_SYNC_SKIP_ADDONS",
        ),
    },
}
INGRESS_ROUTE_WORKFLOW_PATHS = frozenset(
    (
        ".github/workflows/ingress-route-apply.yml",
        ".github/workflows/ingress-route-canary-apply.yml",
        ".github/workflows/ingress-route-dry-run.yml",
    )
)
IGNORED_YAML_SCALAR_KEYS = frozenset(("description", "id", "name", "run", "uses"))
YAML_BLOCK_SCALAR_OPENERS = frozenset(("|", "|-", "|+", ">", ">-", ">+"))
DOCKER_ENV_PATTERN = re.compile(r"^\s*(?:ENV|ARG)\s+(?P<assignment>.+?)\s*$", re.I)

IGNORED_DIR_NAMES = frozenset(
    (
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    )
)
TEXT_SCAN_SUFFIXES = frozenset(
    (
        "",
        ".Dockerfile",
        ".env",
        ".example",
        ".ini",
        ".json",
        ".just",
        ".make",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    )
)
SCRIPT_NAMES = frozenset(("Dockerfile", "Makefile", "Justfile", "justfile", "makefile"))
SKIPPED_DEPENDENCY_MANIFEST_NAMES = frozenset(
    (
        "bun.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "uv.lock",
        "yarn.lock",
    )
)


@dataclass(frozen=True)
class AuditSourceFile:
    path: Path
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str
    git_status: str
    head_blob_sha: str
    index_blob_sha: str
    worktree_sha256: str

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "git_status": self.git_status,
            "head_blob_sha": self.head_blob_sha,
            "index_blob_sha": self.index_blob_sha,
            "worktree_sha256": self.worktree_sha256,
        }


@dataclass(frozen=True)
class ConfigAuthorityFinding:
    finding_id: str
    path: str
    line: int
    rule_id: str
    severity: str
    key: str
    value_hash: str
    evidence: str
    classification: str
    allow_reason: str
    parser: str
    git_status: str

    @property
    def fingerprint(self) -> tuple[str, str, str, str]:
        return (self.path, self.rule_id, self.key, self.value_hash)

    def as_payload(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "path": self.path,
            "line": self.line,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "key": self.key,
            "value_hash": self.value_hash,
            "evidence": self.evidence,
            "classification": self.classification,
            "allow_reason": self.allow_reason,
            "parser": self.parser,
            "git_status": self.git_status,
        }


@dataclass(frozen=True)
class CoverageGap:
    path: str
    reason: str
    detail: str

    def as_payload(self) -> dict[str, object]:
        return {"path": self.path, "reason": self.reason, "detail": self.detail}


def build_config_authority_audit(
    *,
    control_plane_root: Path,
    mode: str = "full-audit",
    include_untracked: bool = False,
    include_ignored: bool = False,
    paths: Sequence[Path] = (),
) -> dict[str, object]:
    if mode not in SCAN_MODES:
        raise ValueError(f"Unsupported config authority audit mode: {mode}")

    root = control_plane_root.resolve()
    repo_metadata = _repo_metadata(root)
    git_file_state = _git_file_state(root)
    changed_file_merge_base = _git_merge_base(root) if mode == "changed-files-gate" else ""
    changed_file_status_entries = _git_status_entries(root) if mode == "changed-files-gate" else []
    baseline_fingerprint_counts = (
        _changed_file_baseline_fingerprint_counts(
            root,
            merge_base=changed_file_merge_base,
            status_entries=changed_file_status_entries,
        )
        if mode == "changed-files-gate"
        else Counter()
    )
    source_files, coverage_gaps = _discover_source_files(
        root=root,
        mode=mode,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
        paths=paths,
        git_file_state=git_file_state,
    )
    findings: list[ConfigAuthorityFinding] = []
    raw_findings: list[dict[str, object]] = []
    for source_file in source_files:
        file_findings, file_gaps = _scan_source_file(source_file)
        if baseline_fingerprint_counts:
            file_findings = _mark_preexisting_changed_file_findings(
                file_findings,
                baseline_fingerprint_counts=baseline_fingerprint_counts,
            )
        findings.extend(file_findings)
        coverage_gaps.extend(file_gaps)
        raw_findings.extend(_raw_finding_payload(finding) for finding in file_findings)
    if (
        mode == "changed-files-gate"
        and not paths
        and not changed_file_merge_base
        and not changed_file_status_entries
    ):
        gap = CoverageGap(
            path=".",
            reason="merge_base_unavailable",
            detail="Changed-files gate could not resolve origin/main or main and found no dirty files to compare against HEAD.",
        )
        finding = _changed_files_gate_base_finding()
        coverage_gaps.append(gap)
        findings.append(finding)
        raw_findings.append(_raw_finding_payload(finding))

    findings = sorted(findings, key=lambda item: (item.path, item.line, item.rule_id, item.key))
    finding_payloads = [finding.as_payload() for finding in findings]
    source_payloads = [source_file.as_payload() for source_file in source_files]
    scanner_payload: dict[str, object] = {
        "version": HASH_VERSION,
        "include_untracked": include_untracked,
        "include_ignored": include_ignored,
        "max_scanned_file_bytes": MAX_SCANNED_FILE_BYTES,
    }
    if mode == "changed-files-gate":
        scanner_payload["changed_files_gate"] = {
            "preexisting_findings_are_report_only": True,
        }

    payload: dict[str, object] = {
        "status": "ok",
        "mode": mode,
        "control_plane_root": str(root),
        "repo": repo_metadata,
        "scanner": scanner_payload,
        "coverage": {
            "source_file_count": len(source_files),
            "finding_count": len(findings),
            "coverage_gap_count": len(coverage_gaps),
            "gaps": [gap.as_payload() for gap in coverage_gaps],
        },
        "hashes": {
            "input_set_hash": _stable_hash(source_payloads),
            "finding_set_hash": _stable_hash(finding_payloads),
        },
        "source_files": source_payloads,
        "findings": finding_payloads,
    }
    if raw_findings:
        payload["raw_finding_count"] = len(raw_findings)
    return payload


def evaluate_config_authority_gate(
    payload: Mapping[str, object], *, profile: str = "default"
) -> dict[str, object]:
    if profile not in GATE_PROFILES:
        raise ValueError(f"Unsupported config authority gate profile: {profile}")

    rejected_findings: list[dict[str, object]] = []
    for finding in _list_payload(payload.get("findings")):
        if not isinstance(finding, dict):
            continue
        rejection_reason = _gate_rejection_reason(finding, profile=profile)
        if rejection_reason:
            rejected_findings.append(
                {
                    "finding_id": finding.get("finding_id", ""),
                    "path": finding.get("path", ""),
                    "line": finding.get("line", 0),
                    "rule_id": finding.get("rule_id", ""),
                    "key": finding.get("key", ""),
                    "classification": finding.get("classification", ""),
                    "allow_reason": finding.get("allow_reason", ""),
                    "rejection_reason": rejection_reason,
                }
            )
    return {
        "profile": profile,
        "status": "fail" if rejected_findings else "pass",
        "rejected_finding_count": len(rejected_findings),
        "rejected_findings": rejected_findings,
    }


def render_config_authority_markdown(payload: Mapping[str, object]) -> str:
    coverage = _mapping_payload(payload.get("coverage"))
    hashes = _mapping_payload(payload.get("hashes"))
    findings = _list_payload(payload.get("findings"))
    gaps = _list_payload(coverage.get("gaps"))
    lines = [
        "# Config Authority Audit",
        "",
        f"- Mode: `{payload.get('mode', '')}`",
        f"- Source files: `{coverage.get('source_file_count', 0)}`",
        f"- Findings: `{coverage.get('finding_count', 0)}`",
        f"- Coverage gaps: `{coverage.get('coverage_gap_count', 0)}`",
        f"- Input set hash: `{hashes.get('input_set_hash', '')}`",
        f"- Finding set hash: `{hashes.get('finding_set_hash', '')}`",
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("No findings.")
    else:
        lines.append("| ID | Severity | Path | Key | Classification | Allow Reason |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            path = f"{finding.get('path', '')}:{finding.get('line', '')}"
            lines.append(
                "| {finding_id} | {severity} | `{path}` | `{key}` | {classification} | {allow_reason} |".format(
                    finding_id=finding.get("finding_id", ""),
                    severity=finding.get("severity", ""),
                    path=path,
                    key=finding.get("key", ""),
                    classification=finding.get("classification", ""),
                    allow_reason=finding.get("allow_reason", ""),
                )
            )
    lines.extend(("", "## Coverage Gaps", ""))
    if not gaps:
        lines.append("No coverage gaps.")
    else:
        lines.append("| Path | Reason | Detail |")
        lines.append("| --- | --- | --- |")
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            lines.append(
                "| `{path}` | {reason} | {detail} |".format(
                    path=gap.get("path", ""),
                    reason=gap.get("reason", ""),
                    detail=str(gap.get("detail", "")).replace("|", "\\|"),
                )
            )
    return "\n".join(lines) + "\n"


def _discover_source_files(
    *,
    root: Path,
    mode: str,
    include_untracked: bool,
    include_ignored: bool,
    paths: Sequence[Path],
    git_file_state: Mapping[str, Mapping[str, str]],
) -> tuple[list[AuditSourceFile], list[CoverageGap]]:
    candidate_paths = _candidate_paths(
        root=root,
        mode=mode,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
        paths=paths,
    )
    source_files: list[AuditSourceFile] = []
    coverage_gaps: list[CoverageGap] = []
    for path in candidate_paths:
        relative_path = _relative_path(root=root, path=path)
        if not _is_text_scan_candidate(path):
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="unscanned_file_class",
                    detail="File extension or name is not in the MVP scanner surface.",
                )
            )
            continue
        if path.name in SKIPPED_DEPENDENCY_MANIFEST_NAMES:
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="skipped_dependency_manifest",
                    detail="Dependency lockfiles are not config-authority surfaces in the MVP scanner.",
                )
            )
            continue
        try:
            stat = path.stat()
        except OSError as error:
            coverage_gaps.append(
                CoverageGap(path=relative_path, reason="unreadable_file", detail=str(error))
            )
            continue
        if stat.st_size > MAX_SCANNED_FILE_BYTES:
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="skipped_large_file",
                    detail=f"File is {stat.st_size} bytes; limit is {MAX_SCANNED_FILE_BYTES} bytes.",
                )
            )
            continue
        try:
            content = path.read_bytes()
        except OSError as error:
            coverage_gaps.append(
                CoverageGap(path=relative_path, reason="unreadable_file", detail=str(error))
            )
            continue
        if _looks_binary(content):
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="skipped_binary_file",
                    detail="File contains NUL bytes or cannot be scanned as text.",
                )
            )
            continue
        git_state = git_file_state.get(relative_path, {})
        sha256 = hashlib.sha256(content).hexdigest()
        source_files.append(
            AuditSourceFile(
                path=path,
                relative_path=relative_path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                sha256=sha256,
                git_status=str(git_state.get("git_status") or "untracked"),
                head_blob_sha=str(git_state.get("head_blob_sha") or ""),
                index_blob_sha=str(git_state.get("index_blob_sha") or ""),
                worktree_sha256=sha256,
            )
        )
    return source_files, coverage_gaps


def _candidate_paths(
    *,
    root: Path,
    mode: str,
    include_untracked: bool,
    include_ignored: bool,
    paths: Sequence[Path],
) -> list[Path]:
    if paths:
        return sorted(
            {
                discovered_path
                for path in paths
                for discovered_path in _explicit_scan_paths(
                    root=root,
                    path=_resolve_scan_path(root=root, path=path),
                )
            }
        )
    if mode == "changed-files-gate":
        return _git_changed_files(root)
    if include_ignored:
        discovered: list[Path] = []
        for directory, dir_names, file_names in os.walk(root):
            dir_names[:] = [
                name
                for name in dir_names
                if name not in IGNORED_DIR_NAMES and (include_ignored or name != ".code")
            ]
            for file_name in file_names:
                discovered.append(Path(directory) / file_name)
        return sorted(discovered)
    discovered_paths = [root / relative_path for relative_path in _git_tracked_relative_paths(root)]
    if include_untracked:
        discovered_paths.extend(
            root / relative_path for relative_path in _git_untracked_relative_paths(root)
        )
    return sorted(discovered_paths)


def _explicit_scan_paths(*, root: Path, path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and root in (candidate, *candidate.parents):
            yield candidate


def _scan_source_file(
    source_file: AuditSourceFile,
) -> tuple[list[ConfigAuthorityFinding], list[CoverageGap]]:
    try:
        text = source_file.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        return [], [
            CoverageGap(
                path=source_file.relative_path,
                reason="decode_failure",
                detail=str(error),
            )
        ]
    return _scan_source_text(source_file=source_file, text=text)


def _mark_preexisting_changed_file_findings(
    findings: Sequence[ConfigAuthorityFinding],
    *,
    baseline_fingerprint_counts: Counter[tuple[str, str, str, str]],
) -> list[ConfigAuthorityFinding]:
    remaining_counts = baseline_fingerprint_counts.copy()
    marked_findings: list[ConfigAuthorityFinding] = []
    for finding in findings:
        if finding.key == "launchplane-config-authority-binding":
            marked_findings.append(finding)
            continue
        if remaining_counts[finding.fingerprint] > 0:
            marked_findings.append(_mark_preexisting_changed_file_finding(finding))
            remaining_counts[finding.fingerprint] -= 1
            continue
        marked_findings.append(finding)
    return marked_findings


def _mark_preexisting_changed_file_finding(
    finding: ConfigAuthorityFinding,
) -> ConfigAuthorityFinding:
    if finding.classification != "needs_classification":
        return finding
    return ConfigAuthorityFinding(
        finding_id=finding.finding_id,
        path=finding.path,
        line=finding.line,
        rule_id=finding.rule_id,
        severity="info",
        key=finding.key,
        value_hash=finding.value_hash,
        evidence=finding.evidence,
        classification="allowed",
        allow_reason="preexisting_changed_file_finding",
        parser=finding.parser,
        git_status=finding.git_status,
    )


def _changed_file_baseline_fingerprint_counts(
    root: Path,
    *,
    merge_base: str,
    status_entries: Sequence[tuple[str, str]],
) -> Counter[tuple[str, str, str, str]]:
    if not merge_base:
        return Counter()
    fingerprints: Counter[tuple[str, str, str, str]] = Counter()
    baseline_paths = set(_git_branch_changed_relative_paths(root))
    baseline_paths.update(relative_path for _, relative_path in status_entries)
    for relative_path in sorted(baseline_paths):
        baseline_text = _git_output(root, "show", f"{merge_base}:{relative_path}")
        if not baseline_text:
            continue
        path = root / relative_path
        source_file = AuditSourceFile(
            path=path,
            relative_path=relative_path,
            sha256=_stable_hash(baseline_text),
            size=len(baseline_text.encode("utf-8")),
            mtime_ns=0,
            git_status="baseline",
            head_blob_sha="",
            index_blob_sha="",
            worktree_sha256="",
        )
        findings, _ = _scan_source_text(source_file=source_file, text=baseline_text)
        fingerprints.update(finding.fingerprint for finding in findings)
    return fingerprints


def _changed_files_gate_base_finding() -> ConfigAuthorityFinding:
    value_hash = _stable_hash("merge_base_unavailable")
    return ConfigAuthorityFinding(
        finding_id=_finding_id(
            path=".",
            line=0,
            rule_id="changed_files_gate_base_unavailable",
            key="changed_files_gate.merge_base",
            value_hash=value_hash,
        ),
        path=".",
        line=0,
        rule_id="changed_files_gate_base_unavailable",
        severity="medium",
        key="changed_files_gate.merge_base",
        value_hash=value_hash,
        evidence="origin/main or main merge base unavailable",
        classification="needs_classification",
        allow_reason="",
        parser="git",
        git_status="unknown",
    )


def _scan_source_text(
    *, source_file: AuditSourceFile, text: str
) -> tuple[list[ConfigAuthorityFinding], list[CoverageGap]]:
    parser = _parser_name(source_file.path)
    candidates: list[tuple[int, str, object]] = []
    coverage_gaps: list[CoverageGap] = []
    if parser == "python_ast":
        parsed_candidates, parse_error = _python_candidates(source_file.relative_path, text)
        candidates.extend(parsed_candidates)
        if parse_error:
            coverage_gaps.append(
                CoverageGap(source_file.relative_path, "parse_failure", parse_error)
            )
    elif parser == "json":
        parsed_candidates, parse_error = _json_candidates(text)
        candidates.extend(parsed_candidates)
        if parse_error:
            coverage_gaps.append(
                CoverageGap(source_file.relative_path, "parse_failure", parse_error)
            )
    elif parser == "toml":
        parsed_candidates, parse_error = _toml_candidates(text)
        candidates.extend(parsed_candidates)
        if parse_error:
            coverage_gaps.append(
                CoverageGap(source_file.relative_path, "parse_failure", parse_error)
            )
    elif parser == "yaml_line_scan":
        candidates.extend(_yaml_line_candidates(text))
        coverage_gaps.append(
            CoverageGap(
                source_file.relative_path,
                "parser_limitation",
                "YAML scanned line-by-line because no structured YAML parser is available.",
            )
        )
    elif parser == "env_line_scan":
        candidates.extend(_env_line_candidates(text))
    elif parser == "dockerfile_line_scan":
        candidates.extend(_dockerfile_line_candidates(text))
    else:
        candidates.extend(_script_line_candidates(text))

    allow_context = _allow_context_for_candidates(
        path=source_file.relative_path,
        candidates=candidates,
    )
    if (
        source_file.relative_path == LAUNCHPLANE_CONFIG_AUTHORITY_WORKFLOW_PATH
        and not allow_context.get("launchplane_config_authority_binding_valid")
    ):
        candidates.append(
            (
                0,
                "launchplane-config-authority-binding",
                allow_context.get("launchplane_config_authority_binding_evidence", "invalid"),
            )
        )
    findings = [
        _build_finding(
            source_file=source_file,
            line=line,
            key=key,
            value=value,
            parser=parser,
            allow_context=allow_context,
        )
        for line, key, value in candidates
        if _candidate_is_interesting(path=source_file.relative_path, key=key, value=value)
    ]
    return findings, coverage_gaps


def _python_candidates(relative_path: str, text: str) -> tuple[list[tuple[int, str, object]], str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return [], str(error)
    dataclass_fields = _python_dataclass_fields(tree)
    candidates: list[tuple[int, str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = _literal_value(node.value)
            for target in node.targets:
                key = _assignment_target_name(target)
                if key and value is not None:
                    candidates.append((node.lineno, key, value))
                if key:
                    candidates.extend(
                        _python_semantic_value_candidates(
                            node.value,
                            base_key=key,
                            dataclass_fields=dataclass_fields,
                        )
                    )
        elif isinstance(node, ast.AnnAssign):
            value = _literal_value(node.value) if node.value is not None else None
            key = _assignment_target_name(node.target)
            if key and value is not None:
                candidates.append((node.lineno, key, value))
            if key and node.value is not None:
                candidates.extend(
                    _python_semantic_value_candidates(
                        node.value,
                        base_key=key,
                        dataclass_fields=dataclass_fields,
                    )
                )
        elif isinstance(node, ast.Call):
            candidates.extend(_python_call_candidates(node))
    return candidates, ""


def _python_dataclass_fields(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    fields: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass_class(node):
            continue
        class_fields: list[str] = []
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                class_fields.append(statement.target.id)
        fields[node.name] = tuple(class_fields)
    return fields


def _is_dataclass_class(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if _call_name(decorator) == "dataclass":
            return True
        if isinstance(decorator, ast.Call) and _call_name(decorator.func) == "dataclass":
            return True
    return False


def _python_semantic_value_candidates(
    node: ast.AST,
    *,
    base_key: str,
    dataclass_fields: Mapping[str, tuple[str, ...]],
) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    literal_value = _literal_value(node)
    if literal_value is not None:
        for child_key, value in _flatten_python_literal(literal_value, prefix=base_key):
            if child_key != base_key or _semantic_path_has_context(base_key):
                candidates.append((getattr(node, "lineno", 0), child_key, value))
        return candidates
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        for index, item in enumerate(node.elts):
            candidates.extend(
                _python_semantic_value_candidates(
                    item,
                    base_key=f"{base_key}[{index}]",
                    dataclass_fields=dataclass_fields,
                )
            )
        return candidates
    if isinstance(node, ast.Dict):
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            key_value = _literal_value(key_node)
            child_key = (
                f"{base_key}.{key_value}" if key_value is not None else f"{base_key}.<dynamic>"
            )
            candidates.extend(
                _python_semantic_value_candidates(
                    value_node,
                    base_key=child_key,
                    dataclass_fields=dataclass_fields,
                )
            )
        return candidates
    if isinstance(node, ast.Call):
        call_name = _call_name(node.func)
        call_leaf_name = call_name.rsplit(".", 1)[-1]
        field_names = dataclass_fields.get(call_name) or dataclass_fields.get(call_leaf_name, ())
        if not field_names:
            return candidates
        for index, argument in enumerate(node.args):
            field_name = field_names[index] if index < len(field_names) else f"arg{index}"
            candidates.extend(
                _python_semantic_value_candidates(
                    argument,
                    base_key=f"{base_key}.{call_name}.{field_name}",
                    dataclass_fields=dataclass_fields,
                )
            )
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            candidates.extend(
                _python_semantic_value_candidates(
                    keyword.value,
                    base_key=f"{base_key}.{call_name}.{keyword.arg}",
                    dataclass_fields=dataclass_fields,
                )
            )
    return candidates


def _flatten_python_literal(value: object, prefix: str) -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}"
            yield from _flatten_python_literal(item, child_prefix)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            yield from _flatten_python_literal(item, child_prefix)
    else:
        yield prefix, value


def _semantic_path_has_context(key: str) -> bool:
    return "." in key or "[" in key


def _python_call_candidates(node: ast.Call) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    function_name = _call_name(node.func)
    if function_name.rsplit(".", 1)[-1] == "DriverDescriptor":
        for keyword in node.keywords:
            if keyword.arg != "context_patterns":
                continue
            value = _literal_value(keyword.value)
            if value is None:
                continue
            candidates.extend(
                (
                    getattr(keyword.value, "lineno", node.lineno),
                    key,
                    item,
                )
                for key, item in _flatten_python_literal(
                    value,
                    prefix="DriverDescriptor.context",
                )
            )
    if function_name in {"click.option", "option"}:
        option_names = [
            value
            for value in (_literal_value(argument) for argument in node.args)
            if isinstance(value, str)
        ]
        option_key = next((name for name in option_names if name.startswith("--")), "click.option")
        for keyword in node.keywords:
            if keyword.arg in {"default", "envvar", "required"}:
                value = _literal_value(keyword.value)
                if value is not None:
                    candidates.append((node.lineno, f"{option_key}.{keyword.arg}", value))
    return candidates


def _json_candidates(text: str) -> tuple[list[tuple[int, str, object]], str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return [], str(error)
    return [(1, key, value) for key, value in _flatten_mapping(parsed)], ""


def _toml_candidates(text: str) -> tuple[list[tuple[int, str, object]], str]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return [], str(error)
    return [(1, key, value) for key, value in _flatten_mapping(parsed)], ""


def _yaml_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    lines = text.splitlines()
    index = 0
    context_stack: list[tuple[int, str]] = []
    checkout_uses_indent: int | None = None
    checkout_with_count = 0
    dependency_health_uses_indent: int | None = None
    dependency_health_with_count = 0
    config_authority_uses_indent: int | None = None
    config_authority_with_count = 0
    generic_web_preview_uses_indent: int | None = None
    generic_web_preview_with_count = 0
    while index < len(lines):
        line_number = index + 1
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        indent = _leading_space_count(line)
        if checkout_uses_indent is not None and indent < checkout_uses_indent:
            checkout_uses_indent = None
        if dependency_health_uses_indent is not None and indent < dependency_health_uses_indent:
            dependency_health_uses_indent = None
        if config_authority_uses_indent is not None and indent < config_authority_uses_indent:
            config_authority_uses_indent = None
        if generic_web_preview_uses_indent is not None and indent < generic_web_preview_uses_indent:
            generic_web_preview_uses_indent = None
        context_stack = _yaml_context_for_indent(context_stack, indent=indent)
        list_match = YAML_LIST_ITEM_PATTERN.match(line)
        if list_match is not None:
            list_value = _unquote(list_match.group("value"))
            list_scalar = YAML_SCALAR_PATTERN.match(list_value)
            if list_scalar is not None:
                yaml_key = _unquote(list_scalar.group("key"))
                raw_scalar_value = _strip_inline_comment(list_scalar.group("value")).strip()
                next_index = index + 1
                if raw_scalar_value in YAML_BLOCK_SCALAR_OPENERS:
                    block_lines, next_index = _yaml_block_scalar_lines(
                        lines=lines,
                        start_index=index + 1,
                        parent_indent=indent + 2,
                    )
                    scalar_value = " ".join(block_lines).strip()
                else:
                    scalar_value = _unquote(raw_scalar_value)
                if yaml_key == "uses" and _is_github_checkout_action_reference(scalar_value):
                    checkout_uses_indent = indent + 2
                if yaml_key == "uses" and _is_launchplane_dependency_health_action_reference(
                    scalar_value
                ):
                    dependency_health_uses_indent = indent + 2
                if yaml_key == "uses" and _is_launchplane_config_authority_workflow_reference(
                    scalar_value
                ):
                    config_authority_uses_indent = indent + 2
                if yaml_key == "uses" and _is_launchplane_generic_web_preview_facade_reference(
                    scalar_value
                ):
                    generic_web_preview_uses_indent = indent + 2
                if yaml_key == "uses" and (
                    _is_yaml_reusable_workflow_reference(scalar_value)
                    or _is_launchplane_dependency_health_action_reference(scalar_value)
                ):
                    candidates.append((line_number, "uses", scalar_value))
                index = next_index
            else:
                list_key = _yaml_list_candidate_key(context_stack)
                if list_key:
                    candidates.append((line_number, list_key, list_value))
                index += 1
            continue
        empty_match = YAML_EMPTY_MAPPING_PATTERN.match(line)
        if empty_match is not None:
            yaml_key = _unquote(empty_match.group("key"))
            if (
                yaml_key == "with"
                and checkout_uses_indent is not None
                and indent == checkout_uses_indent
            ):
                checkout_with_count += 1
                yaml_key = f"checkout.with[{checkout_with_count}]"
                checkout_uses_indent = None
            elif (
                yaml_key == "with"
                and dependency_health_uses_indent is not None
                and indent == dependency_health_uses_indent
            ):
                dependency_health_with_count += 1
                yaml_key = f"dependency-health.with[{dependency_health_with_count}]"
                dependency_health_uses_indent = None
            elif (
                yaml_key == "with"
                and config_authority_uses_indent is not None
                and indent == config_authority_uses_indent
            ):
                config_authority_with_count += 1
                yaml_key = f"launchplane-config-authority.with[{config_authority_with_count}]"
                config_authority_uses_indent = None
            elif (
                yaml_key == "with"
                and generic_web_preview_uses_indent is not None
                and indent == generic_web_preview_uses_indent
            ):
                generic_web_preview_with_count += 1
                yaml_key = f"generic-web-preview.with[{generic_web_preview_with_count}]"
                generic_web_preview_uses_indent = None
            context_stack.append((indent, yaml_key))
            index += 1
            continue
        match = YAML_SCALAR_PATTERN.match(line)
        if match is None:
            index += 1
            continue
        yaml_key = _unquote(match.group("key"))
        key = _yaml_candidate_key(context_stack, yaml_key)
        value = _strip_inline_comment(match.group("value")).strip()
        if value in YAML_BLOCK_SCALAR_OPENERS:
            block_lines, next_index = _yaml_block_scalar_lines(
                lines=lines,
                start_index=index + 1,
                parent_indent=indent,
            )
            block_value = " ".join(block_lines).strip()
            if yaml_key == "uses" and _is_github_checkout_action_reference(block_value):
                checkout_uses_indent = indent
            if yaml_key == "uses" and _is_launchplane_dependency_health_action_reference(
                block_value
            ):
                dependency_health_uses_indent = indent
            if yaml_key == "uses" and _is_launchplane_config_authority_workflow_reference(
                block_value
            ):
                config_authority_uses_indent = indent
            if yaml_key == "uses" and _is_launchplane_generic_web_preview_facade_reference(
                block_value
            ):
                generic_web_preview_uses_indent = indent
            if (
                yaml_key in IGNORED_YAML_SCALAR_KEYS
                and not _is_yaml_reusable_workflow_reference(block_value)
                and not _is_launchplane_dependency_health_action_reference(block_value)
            ):
                index = next_index
                continue
            if yaml_key == "payload-fields":
                candidates.extend(
                    _yaml_block_scalar_assignment_candidates(
                        block_lines=block_lines,
                        start_line=line_number + 1,
                    )
                )
                index = next_index
                continue
            if block_value:
                candidates.append((line_number, key, block_value))
            index = next_index
            continue
        if value:
            scalar_value = _unquote(value)
            if yaml_key == "uses" and _is_github_checkout_action_reference(scalar_value):
                checkout_uses_indent = indent
            if yaml_key == "uses" and _is_launchplane_dependency_health_action_reference(
                scalar_value
            ):
                dependency_health_uses_indent = indent
            if yaml_key == "uses" and _is_launchplane_config_authority_workflow_reference(
                scalar_value
            ):
                config_authority_uses_indent = indent
            if yaml_key == "uses" and _is_launchplane_generic_web_preview_facade_reference(
                scalar_value
            ):
                generic_web_preview_uses_indent = indent
            if (
                yaml_key in IGNORED_YAML_SCALAR_KEYS
                and not _is_yaml_reusable_workflow_reference(scalar_value)
                and not _is_launchplane_dependency_health_action_reference(scalar_value)
            ):
                index += 1
                continue
            candidates.append((line_number, key, scalar_value))
        index += 1
    return candidates


def _yaml_context_for_indent(
    context_stack: Sequence[tuple[int, str]], *, indent: int
) -> list[tuple[int, str]]:
    return [
        (context_indent, key) for context_indent, key in context_stack if context_indent < indent
    ]


def _yaml_candidate_key(context_stack: Sequence[tuple[int, str]], key: str) -> str:
    if key == "default":
        input_name = _yaml_workflow_input_name(context_stack)
        if input_name:
            return f"inputs.{input_name}.default"
    checkout_block = _yaml_checkout_with_block(context_stack)
    if key == "repository" and checkout_block:
        return f"checkout.repository[{checkout_block}]"
    if key == "ref" and checkout_block:
        return f"checkout.ref[{checkout_block}]"
    dependency_health_block = _yaml_dependency_health_with_block(context_stack)
    if dependency_health_block:
        return f"dependency-health.with[{dependency_health_block}].{key}"
    config_authority_block = _yaml_config_authority_with_block(context_stack)
    if config_authority_block:
        return f"launchplane-config-authority.with[{config_authority_block}].{key}"
    generic_web_preview_block = _yaml_generic_web_preview_with_block(context_stack)
    if generic_web_preview_block:
        return f"generic-web-preview.with[{generic_web_preview_block}].{key}"
    return key


def _yaml_list_candidate_key(context_stack: Sequence[tuple[int, str]]) -> str:
    if context_stack and context_stack[-1][1] == "runs-on":
        return "runs-on"
    return ""


def _is_github_checkout_action_reference(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("actions/checkout@")
        and value != "actions/checkout@"
    )


def _is_yaml_reusable_workflow_reference(value: object) -> bool:
    return ".github/workflows/" in _string_value(value)


def _is_launchplane_dependency_health_action_reference(value: object) -> bool:
    return (
        LAUNCHPLANE_DEPENDENCY_HEALTH_ACTION_REFERENCE_PATTERN.fullmatch(
            _string_value(value).strip()
        )
        is not None
    )


def _is_immutable_launchplane_dependency_health_action_reference(value: object) -> bool:
    return (
        IMMUTABLE_LAUNCHPLANE_DEPENDENCY_HEALTH_ACTION_PATTERN.fullmatch(
            _string_value(value).strip()
        )
        is not None
    )


def _yaml_workflow_input_name(context_stack: Sequence[tuple[int, str]]) -> str:
    keys = [key for _, key in context_stack]
    for index in range(len(keys) - 2):
        if keys[index : index + 2] in (
            ["workflow_dispatch", "inputs"],
            ["workflow_call", "inputs"],
        ):
            return keys[index + 2]
    return ""


def _yaml_checkout_with_block(context_stack: Sequence[tuple[int, str]]) -> str:
    if not context_stack:
        return ""
    key = context_stack[-1][1]
    if not key.startswith("checkout.with[") or not key.endswith("]"):
        return ""
    return key.removeprefix("checkout.with[").removesuffix("]")


def _yaml_dependency_health_with_block(context_stack: Sequence[tuple[int, str]]) -> str:
    if not context_stack:
        return ""
    key = context_stack[-1][1]
    if not key.startswith("dependency-health.with[") or not key.endswith("]"):
        return ""
    return key.removeprefix("dependency-health.with[").removesuffix("]")


def _yaml_config_authority_with_block(context_stack: Sequence[tuple[int, str]]) -> str:
    if not context_stack:
        return ""
    key = context_stack[-1][1]
    if not key.startswith("launchplane-config-authority.with[") or not key.endswith("]"):
        return ""
    return key.removeprefix("launchplane-config-authority.with[").removesuffix("]")


def _yaml_generic_web_preview_with_block(context_stack: Sequence[tuple[int, str]]) -> str:
    if not context_stack:
        return ""
    key = context_stack[-1][1]
    if not key.startswith("generic-web-preview.with[") or not key.endswith("]"):
        return ""
    return key.removeprefix("generic-web-preview.with[").removesuffix("]")


def _yaml_block_scalar_lines(
    *, lines: Sequence[str], start_index: int, parent_indent: int
) -> tuple[list[str], int]:
    block_lines: list[str] = []
    index = start_index
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            block_lines.append("")
            index += 1
            continue
        if _leading_space_count(line) <= parent_indent:
            break
        block_lines.append(line.strip())
        index += 1
    return block_lines, index


def _yaml_block_scalar_assignment_candidates(
    *, block_lines: Sequence[str], start_line: int
) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for offset, line in enumerate(block_lines):
        stripped = _strip_inline_comment(line).strip()
        if not stripped:
            continue
        match = YAML_BLOCK_ASSIGNMENT_PATTERN.match(stripped)
        if match is None:
            continue
        value = _strip_inline_comment(match.group("value")).strip()
        if value:
            candidates.append(
                (start_line + offset, f"payload-fields.{match.group('key')}", _unquote(value))
            )
    return candidates


def _allow_context_for_candidates(
    *, path: str, candidates: Sequence[tuple[int, str, object]]
) -> Mapping[str, object]:
    allow_context: dict[str, object] = {}
    if path == LAUNCHPLANE_CONFIG_AUTHORITY_WORKFLOW_PATH:
        reusable_workflow_references = [
            _string_value(value).strip()
            for _, key, value in candidates
            if key == "uses" and _is_yaml_reusable_workflow_reference(value)
        ]
        workflow_revisions = [
            revision
            for _, key, value in candidates
            if key == "uses"
            and (revision := _launchplane_config_authority_workflow_revision(value))
        ]
        input_revisions = [
            _string_value(value).strip()
            for _, key, value in candidates
            if _is_launchplane_config_authority_revision_input_key(key)
        ]
        allow_context["launchplane_config_authority_binding_evidence"] = json.dumps(
            {
                "launchplane_revisions": input_revisions,
                "uses": reusable_workflow_references,
            },
            sort_keys=True,
        )
        if (
            len(reusable_workflow_references) == 1
            and len(workflow_revisions) == 1
            and len(input_revisions) == 1
            and workflow_revisions[0] == input_revisions[0]
        ):
            allow_context["launchplane_config_authority_revision"] = workflow_revisions[0]
            allow_context["launchplane_config_authority_binding_valid"] = True
    if path == ".github/workflows/cleanup-ghcr.yml":
        allow_context["cleanup_ghcr_launchplane_products"] = {
            _string_value(value).strip().rstrip(",")
            for _, key, value in candidates
            if key == "LAUNCHPLANE_PRODUCT"
        }
    return allow_context


def _checkout_candidate_block(key: str) -> str:
    match = re.search(r"\[(?P<block>\d+)\]$", key)
    if match is None:
        return ""
    return match.group("block")


def _leading_space_count(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _env_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_ASSIGNMENT_PATTERN.match(stripped)
        if match is not None:
            candidates.append((index, match.group("key"), _unquote(match.group("value"))))
    return candidates


def _dockerfile_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        match = DOCKER_ENV_PATTERN.match(line)
        if match is None:
            continue
        assignment = match.group("assignment")
        candidates.extend((index, key, value) for key, value in _assignment_tokens(assignment))
    return candidates


def _script_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidates.extend(
            (index, match.group("key"), _unquote(match.group("value")))
            for match in SHELL_ENV_PATTERN.finditer(line)
        )
        for url in URL_PATTERN.findall(line):
            candidates.append((index, "url", url))
        for match in _owner_repo_reference_matches(line):
            candidates.append((index, "repository", match.group(0)))
    return candidates


def _build_finding(
    *,
    source_file: AuditSourceFile,
    line: int,
    key: str,
    value: object,
    parser: str,
    allow_context: Mapping[str, object],
) -> ConfigAuthorityFinding:
    rule_id = _rule_id(key=key, value=value)
    allow_reason = _allow_reason(
        path=source_file.relative_path,
        key=key,
        value=value,
        allow_context=allow_context,
    )
    classification = "allowed" if allow_reason else "needs_classification"
    severity = "info" if allow_reason else _severity(rule_id=rule_id, key=key)
    evidence = _redacted_evidence(key=key, value=value)
    value_hash = _stable_hash(value)
    finding_id = _finding_id(
        path=source_file.relative_path,
        line=line,
        rule_id=rule_id,
        key=key,
        value_hash=value_hash,
    )
    return ConfigAuthorityFinding(
        finding_id=finding_id,
        path=source_file.relative_path,
        line=line,
        rule_id=rule_id,
        severity=severity,
        key=key,
        value_hash=value_hash,
        evidence=evidence,
        classification=classification,
        allow_reason=allow_reason,
        parser=parser,
        git_status=source_file.git_status,
    )


def _candidate_is_interesting(*, path: str, key: str, value: object) -> bool:
    normalized = path.replace("\\", "/")
    key_text = _semantic_full_key_text(key)
    value_text = _string_value(value)
    if normalized.startswith(".github/workflows/") and _is_empty_workflow_input_default(
        key=key,
        value=value,
    ):
        return False
    if normalized.startswith(".github/workflows/") and _dependency_health_action_input_name(key):
        return True
    if normalized.startswith(".github/workflows/") and key == "TRIVY_IMAGE":
        return True
    if normalized == LAUNCHPLANE_CONFIG_AUTHORITY_WORKFLOW_PATH and (
        key == "launchplane-revision" or _is_launchplane_config_authority_revision_input_key(key)
    ):
        return True
    if normalized == LAUNCHPLANE_CONFIG_AUTHORITY_WORKFLOW_PATH and key == (
        "launchplane-config-authority-binding"
    ):
        return True
    if _is_click_option_metadata_key(key):
        return True
    if _is_repo_metadata_ergonomics_key(key):
        return True
    if key_text in WORKFLOW_RUNTIME_AUTHORITY_KEYS:
        return True
    if normalized.startswith(".github/workflows/") and (
        key_text == "RUNS_ON"
        or _is_workflow_input_default_key(key)
        or _is_launchplane_service_route_path(key=key, value=value)
        or _is_workflow_mechanic_key_value(key=key, value=value)
        or _is_workflow_literal_branch_guard(key=key, value=value)
        or _is_workflow_operator_input_value(key=key, value=value)
        or _is_workflow_context_reference_restricted_value(value)
        or _is_workflow_operator_variable_forward(
            path=normalized,
            key=key,
            value=value,
        )
        or _is_workflow_payload_field_key(key)
    ):
        return True
    if not value_text.strip():
        return False
    if key_text in BOOTSTRAP_ENV_KEYS:
        return True
    if any(pattern.search(key_text) for pattern in RUNTIME_IDENTITY_KEY_PATTERNS):
        return True
    if CATALOG_KEY_PATTERN.search(key) and SEMANTIC_FIELD_PATTERN.search(key):
        return True
    if any(part in key_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return True
    repository_reference = (
        _contains_owner_repo_reference(value_text)
        if normalized == "package.json" and key.startswith("scripts.")
        else OWNER_REPO_PATTERN.search(value_text) is not None
    )
    return bool(
        URL_PATTERN.search(value_text)
        or repository_reference
        or PROVIDER_TARGET_PATTERN.search(value_text)
    )


def _gate_rejection_reason(finding: Mapping[str, object], *, profile: str) -> str:
    if finding.get("classification") == "needs_classification":
        return "finding_needs_classification"
    if profile != "product-repo":
        return ""
    if finding.get("allow_reason") != ALLOW_REASON_TEST_FIXTURE:
        return ""
    if _is_product_repo_lifecycle_fixture_finding(finding):
        return "launchplane_lifecycle_test_fixture"
    return ""


def _is_product_repo_lifecycle_fixture_finding(finding: Mapping[str, object]) -> bool:
    rule_id = str(finding.get("rule_id") or "")
    if rule_id in PRODUCT_REPO_REJECTED_TEST_FIXTURE_RULE_IDS:
        return True
    key_parts = _authority_key_parts(str(finding.get("key") or ""))
    if frozenset(key_parts) & PRODUCT_REPO_REJECTED_TEST_FIXTURE_KEY_PARTS:
        return True
    for phrase in PRODUCT_REPO_REJECTED_TEST_FIXTURE_KEY_PHRASES:
        if _contains_key_phrase(key_parts, phrase):
            return True
    path = str(finding.get("path") or "").lower().replace("\\", "/")
    return any(
        marker in path
        for marker in (
            "authz",
            "managed-secret",
            "provider-target",
            "route-batch",
            "runtime-environment",
            "target-id",
            "topology",
        )
    )


def _authority_key_parts(key: str) -> tuple[str, ...]:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = separated.upper().replace(".", "_").replace("-", "_")
    return tuple(part for part in re.split(r"[^A-Z0-9]+", normalized) if part)


def _contains_key_phrase(key_parts: Iterable[str], phrase: tuple[str, ...]) -> bool:
    parts = tuple(key_parts)
    if not phrase or len(phrase) > len(parts):
        return False
    return any(parts[index : index + len(phrase)] == phrase for index in range(len(parts)))


def _rule_id(*, key: str, value: object) -> str:
    leaf_text = _semantic_leaf_text(key)
    full_key_text = _semantic_full_key_text(key)
    semantic_key_text = full_key_text if _is_workflow_input_default_key(key) else leaf_text
    value_text = _string_value(value)
    if any(part in semantic_key_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return "secret_binding_identity"
    if (
        URL_PATTERN.search(value_text)
        or "DOMAIN" in semantic_key_text
        or "URL" in semantic_key_text
    ):
        return "domain_or_url_authority"
    if (
        OWNER_REPO_PATTERN.search(value_text)
        or "REPO" in semantic_key_text
        or "REPOSITORY" in semantic_key_text
    ):
        return "repository_authority"
    if (
        "AUTHZ" in semantic_key_text
        or "OPERATOR" in semantic_key_text
        or "SUBJECT" in semantic_key_text
    ):
        return "authz_or_operator_authority"
    if (
        "TARGET" in semantic_key_text
        or "PROVIDER" in semantic_key_text
        or PROVIDER_TARGET_PATTERN.search(value_text)
    ):
        return "provider_target_authority"
    return "runtime_config_authority"


def _is_workflow_input_default_key(key: str) -> bool:
    return key.startswith("inputs.") and key.endswith(".default")


def _is_empty_workflow_input_default(*, key: str, value: object) -> bool:
    value_text = _string_value(value).strip()
    return _is_workflow_input_default_key(key) and value_text in {"", "[]"}


def _severity(*, rule_id: str, key: str) -> str:
    if rule_id in {"secret_binding_identity", "authz_or_operator_authority"}:
        return "high"
    if key.startswith("--"):
        return "medium"
    return "medium"


def _allow_reason(
    *,
    path: str,
    key: str,
    value: object,
    allow_context: Mapping[str, object] | None = None,
) -> str:
    allow_context = allow_context or {}
    normalized = path.replace("\\", "/")
    key_text = _semantic_full_key_text(key)
    if _is_codeowners_path(normalized):
        return ALLOW_REASON_REPO_METADATA_ERGONOMICS
    if normalized.startswith("docs/") or normalized in {"README.md", "AGENTS.md", "handoff.md"}:
        return ALLOW_REASON_DOCS_EXAMPLE
    if normalized.startswith("tests/") or "/test" in normalized:
        return ALLOW_REASON_TEST_FIXTURE
    if normalized.startswith("addons/") or "/addons/" in normalized:
        return ALLOW_REASON_PRODUCT_OWNED_ADDON
    if normalized == ".github/github.json" and _is_repo_metadata_ergonomics_key(key):
        return ALLOW_REASON_REPO_METADATA_ERGONOMICS
    if normalized.endswith(".py") and (
        key_text.startswith("ALLOW_REASON_")
        or key_text.startswith("PRODUCT_DRIVER_REUSABLE_")
        or key_text.endswith(("FIELDS", "SCHEMA", "MODEL_CONFIG"))
        or "PATH_GLOBS" in key_text
    ):
        return ALLOW_REASON_SCHEMA_ONLY
    if normalized.endswith(".py") and _is_python_path_schema_only_value(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_SCHEMA_ONLY
    if key_text in BOOTSTRAP_ENV_KEYS:
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if normalized.startswith(".github/workflows/") and _is_launchplane_public_url_reference(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if _is_launchplane_self_management_product_reference(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if normalized.startswith(".github/workflows/") and _is_launchplane_bootstrap_context_reference(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if normalized.startswith(".github/workflows/") and _is_workflow_service_env_payload(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if normalized.startswith(".github/workflows/") and _is_workflow_mechanic_key_value(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_dependency_health_workflow_mechanic(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _dependency_health_action_input_name(key):
        return ""
    if normalized.startswith(".github/workflows/") and key == "TRIVY_IMAGE":
        return ""
    if normalized == LAUNCHPLANE_CONFIG_AUTHORITY_WORKFLOW_PATH and (
        key == "launchplane-revision"
        or _is_launchplane_config_authority_revision_input_key(key)
        or (key == "uses" and _is_yaml_reusable_workflow_reference(value))
        or key == "launchplane-config-authority-binding"
    ):
        if _is_launchplane_config_authority_connector(
            key=key,
            value=value,
            allow_context=allow_context,
        ):
            return ALLOW_REASON_THIN_CONNECTOR_INPUT
        return ""
    if normalized.startswith(".github/actions/") and _is_github_action_metadata_mechanic(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_image_artifact_mechanic(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_input_mechanic_default(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(
        ".github/workflows/"
    ) and _is_product_driver_reusable_workflow_mechanic(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if (
        normalized.startswith(PRODUCT_DRIVER_REUSABLE_WORKFLOW_PATH_PREFIX)
        and normalized.endswith(PRODUCT_DRIVER_REUSABLE_WORKFLOW_PATH_SUFFIX)
        and key.startswith("payload-fields.")
    ):
        return ""
    if normalized.startswith(".github/workflows/") and _is_workflow_thin_connector_key_value(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_generic_web_preview_facade_input(
        path=normalized,
        key=key,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_cleanup_ghcr_product_forward(
        path=normalized,
        key=key,
        value=value,
        allow_context=allow_context,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_read_model_output_forward(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_preview_feedback_url_output_forward(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_launchplane_reusable_workflow(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_response_summary_field(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_block_mechanic_field(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_input_value(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_input_reference(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_launchplane_operator_var(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_variable_forward(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_array_forward(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_jq_operator_field(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if _is_verireel_dokploy_managed_secret_binding(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_ingress_route_option_literal(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if (
        normalized.startswith(".github/workflows/")
        and not _is_workflow_runtime_authority_key(key)
        and not _is_workflow_operator_input_key(key)
        and key_text != "RUNS_ON"
        and not _is_route_path_key(key)
        and not _is_workflow_payload_field_key(key)
        and not _is_workflow_context_reference_restricted_key(key)
        and not _is_workflow_runtime_authority_key_shape(key)
        and not _is_workflow_context_reference_restricted_value(value)
        and not _is_github_direct_input_reference(value)
        and _is_github_context_reference(value)
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_payload_field_forward(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_launchplane_service_route_path(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if key_text.startswith("--") and key_text.endswith(("REQUIRED", "DEFAULT")):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if _is_click_option_metadata_key(key):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    return ""


def _is_verireel_dokploy_managed_secret_binding(*, path: str, key: str, value: object) -> bool:
    if path != "compose.dokploy.yaml":
        return False
    key_text = key.strip().upper()
    if key_text not in VERIREEL_DOKPLOY_MANAGED_SECRET_BINDINGS:
        return False
    return _string_value(value).strip() == f"${{{key_text}:?required}}"


def _is_github_context_reference(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_CONTEXT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_github_direct_input_reference(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_DIRECT_INPUT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_github_action_input_reference(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_ACTION_INPUT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_workflow_runtime_authority_key(key: str) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    return key_text in WORKFLOW_RUNTIME_AUTHORITY_KEYS


def _is_launchplane_public_url_reference(*, path: str, key: str, value: object) -> bool:
    value_text = _string_value(value).strip()
    if value_text in WORKFLOW_LAUNCHPLANE_URL_REFERENCE_PATH_VALUES.get(path, {}).get(
        key, frozenset()
    ):
        return True
    key_text = key.upper().replace(".", "_").replace("-", "_")
    if key == "LAUNCHPLANE_URL" and value_text == "${{ vars.LAUNCHPLANE_PUBLIC_URL }}":
        return True
    if key_text == "LAUNCHPLANE_URL":
        return value_text in {
            "${{ env.LAUNCHPLANE_SERVICE_URL }}",
            "${{ env.LAUNCHPLANE_URL }}",
        }
    return False


def _is_launchplane_self_management_product_reference(
    *, path: str, key: str, value: object
) -> bool:
    value_text = _string_value(value).strip().rstrip(",")
    return (
        path in LAUNCHPLANE_SELF_MANAGEMENT_WORKFLOW_PATHS
        and key == "product"
        and value_text in {'"launchplane"', "launchplane"}
    )


def _is_workflow_service_env_payload(*, path: str, key: str, value: object) -> bool:
    allowed_values = WORKFLOW_SERVICE_ENV_PAYLOAD_PATH_VALUES.get(path, {}).get(key)
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return value_text in allowed_values


def _is_launchplane_bootstrap_context_reference(*, path: str, key: str, value: object) -> bool:
    allowed_values = WORKFLOW_LAUNCHPLANE_BOOTSTRAP_CONTEXT_PATH_VALUES.get(path, {}).get(key)
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return value_text in allowed_values


def _is_workflow_operator_input_value(*, key: str, value: object) -> bool:
    if not _is_workflow_operator_input_key(key):
        return False
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    body = match.group("body").strip()
    input_match = GITHUB_INPUT_REFERENCE_PATTERN.match(body)
    if input_match is None:
        return False
    input_name = input_match.group("input_name").upper().replace(".", "_").replace("-", "_")
    key_text = key.upper().replace(".", "_").replace("-", "_")
    return input_name == key_text


def _is_workflow_operator_input_key(key: str) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    return key_text in WORKFLOW_OPERATOR_INPUT_VALUE_KEYS


def _is_workflow_operator_input_reference(*, path: str, key: str, value: object) -> bool:
    allowed_values = WORKFLOW_OPERATOR_INPUT_REFERENCE_PATH_VALUES.get(path, {}).get(key)
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return value_text in allowed_values


def _is_workflow_launchplane_operator_var(*, path: str, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    if key_text == "LAUNCHPLANE_URL":
        key_text = "LAUNCHPLANE_PUBLIC_URL"
    value_text = _string_value(value).strip().rstrip(",")
    if key_text in WORKFLOW_LAUNCHPLANE_OPERATOR_VAR_KEYS:
        return value_text == f"${{{{ vars.{key_text} }}}}"
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    body = match.group("body").strip()
    if not body.startswith("vars."):
        return False
    var_key = body.removeprefix("vars.").upper().replace(".", "_").replace("-", "_")
    return var_key in WORKFLOW_LAUNCHPLANE_OPERATOR_VAR_KEYS


def _is_workflow_context_reference_restricted_key(key: str) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    if any(part in key_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return True
    return any(part in key_text.split("_") for part in ("REPO", "REPOSITORY"))


def _is_workflow_runtime_authority_key_shape(key: str) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    return any(pattern.search(key_text) for pattern in RUNTIME_IDENTITY_KEY_PATTERNS)


def _is_workflow_context_reference_restricted_value(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    body = match.group("body").strip()
    if body.startswith("secrets."):
        return True
    if body in {"github.repository", "github.token"}:
        return True
    for segment in body.split("."):
        segment_text = segment.upper().replace("-", "_")
        segment_parts = segment_text.split("_")
        if any(part in segment_parts for part in SECRET_SHAPED_KEY_PARTS):
            return True
        if any(part in segment_parts for part in ("REPO", "REPOSITORY")):
            return True
    return False


def _is_workflow_operator_variable_forward(*, path: str, key: str, value: object) -> bool:
    if path not in WORKFLOW_OPERATOR_VARIABLE_FORWARD_PATHS:
        return False
    if not _is_workflow_operator_input_key(key):
        return False
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip().rstrip(",")
    return value_text == f"${key_text.lower()}"


def _is_workflow_jq_operator_field(*, path: str, key: str, value: object) -> bool:
    if key not in WORKFLOW_JQ_OPERATOR_FIELD_PATH_KEYS.get(path, frozenset()):
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return value_text == f"${key}"


def _is_workflow_response_summary_field(*, path: str, key: str, value: object) -> bool:
    allowed_values = _workflow_path_key_values(
        WORKFLOW_RESPONSE_SUMMARY_PATH_VALUES,
        path=path,
        key=key,
    )
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return value_text in allowed_values


def _is_workflow_block_mechanic_field(*, path: str, key: str, value: object) -> bool:
    allowed_values = _workflow_path_key_values(
        WORKFLOW_BLOCK_MECHANIC_FIELD_PATH_VALUES,
        path=path,
        key=key,
    )
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip()
    return value_text in allowed_values


def _is_workflow_operator_array_forward(*, path: str, key: str, value: object) -> bool:
    if path not in INGRESS_ROUTE_WORKFLOW_PATHS:
        return False
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip().rstrip(",").replace(" ", "")
    return key_text == "DOMAIN_NAMES" and value_text == "[$domain]"


def _is_ingress_route_option_literal(*, path: str, key: str, value: object) -> bool:
    if path not in INGRESS_ROUTE_WORKFLOW_PATHS:
        return False
    key_text = key.lower().replace("-", "_")
    value_text = _string_value(value).strip().rstrip(",")
    return key_text in {"npmplus_http3_support", "npmplus_noindex"} and value_text in {
        "false",
        "true",
    }


def _is_workflow_mechanic_key_value(*, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip()
    if key_text == "RUNS_ON":
        return value_text in WORKFLOW_RUNS_ON_MECHANIC_VALUES
    if key_text == "ID_TOKEN" and value_text == "write":
        return True
    if key_text == "GROUP" and "${{ inputs." in value_text and "${{ vars." not in value_text:
        return True
    if key_text == "PATH" and re.fullmatch(r"[A-Za-z0-9_.-]+\.json", value_text):
        return True
    if key_text == "IF" and value_text == (
        "${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}"
    ):
        return True
    return False


def _is_workflow_literal_branch_guard(*, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    if key_text != "IF":
        return False
    value_text = _string_value(value).strip()
    return any(
        pattern.search(value_text) is not None
        for pattern in (
            re.compile(r"github\.ref\s*==\s*['\"]refs/heads/[A-Za-z0-9._/-]+['\"]"),
            re.compile(r"github\.(?:base_ref|head_ref|ref_name)\s*==\s*['\"][A-Za-z0-9._/-]+['\"]"),
        )
    )


def _is_dependency_health_workflow_mechanic(
    *,
    key: str,
    value: object,
) -> bool:
    value_text = _string_value(value).strip()
    if key == "uses":
        return _is_immutable_launchplane_dependency_health_action_reference(value_text)
    if key == "TRIVY_IMAGE":
        return PINNED_TRIVY_IMAGE_PATTERN.fullmatch(value_text) is not None
    input_name = _dependency_health_action_input_name(key)
    if input_name == "baseline-report":
        return _is_step_output_path(value_text, suffix="/reports/baseline.json")
    if input_name == "candidate-report":
        return _is_step_output_path(value_text, suffix="/reports/candidate.json")
    if input_name == "repository":
        return value_text == "${{ github.repository }}"
    if input_name == "baseline-commit":
        return value_text == "${{ env.BASELINE_COMMIT }}"
    if input_name == "candidate-commit":
        return value_text == "${{ env.CANDIDATE_COMMIT }}"
    if input_name == "producer-version":
        return re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value_text) is not None
    if input_name == "advisory-source":
        return value_text == "trivy-db"
    if input_name in {"advisory-revision", "scan-configuration-sha256"}:
        return _is_github_same_job_step_output_reference(value_text)
    if input_name == "scan-scope":
        return re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value_text) is not None
    if input_name == "target-advisory-ids":
        return not value_text or _is_github_same_job_step_output_reference(value_text)
    if input_name == "target-advisory-text":
        return (
            _is_github_same_job_step_output_reference(value_text)
            or value_text == _dependabot_target_advisory_text_expression()
        )
    if input_name == "output-directory":
        return _is_step_output_path(value_text, suffix="/evaluation")
    return False


def _dependency_health_action_input_name(key: str) -> str:
    match = re.fullmatch(
        r"dependency-health\.with\[[0-9]+\]\.(?P<input_name>[A-Za-z0-9_.-]+)",
        key,
    )
    return "" if match is None else match.group("input_name")


def _dependabot_target_advisory_text_expression() -> str:
    return (
        "${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.user.login == 'dependabot[bot]' && "
        "steps.dependabot.outputs.dependency-type != 'direct:development' && "
        "github.event.pull_request.body || '' }}"
    )


def _is_step_output_path(value: str, *, suffix: str) -> bool:
    if not value.endswith(suffix):
        return False
    return _is_github_same_job_step_output_reference(value[: -len(suffix)])


def _is_github_action_metadata_mechanic(*, path: str, key: str, value: object) -> bool:
    if not path.endswith("/action.yml"):
        return False
    value_text = _string_value(value).strip()
    key_text = key.upper().replace(".", "_").replace("-", "_")
    if key_text.startswith("INPUT_") and _is_github_action_input_reference(value):
        return True
    if key_text == "MAIN":
        return re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.js", value_text) is not None
    if key_text == "DEFAULT":
        return (
            re.fullmatch(
                r"\.[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.mjs",
                value_text,
            )
            is not None
        )
    return False


def _is_workflow_image_artifact_mechanic(*, path: str, key: str, value: object) -> bool:
    if path not in {
        ".github/workflows/deploy-launchplane.yml",
        ".github/workflows/launchplane-deploy.yml",
    }:
        return False
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip()
    if key_text == "CONTEXT" and value_text == ".":
        return True
    if key_text == "FILE" and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value_text):
        return True
    if key_text in {"IMAGE_REPOSITORY", "TAGS"} and _is_github_context_or_step_output_reference(
        value_text
    ):
        return True
    if key_text == "PASSWORD" and value_text == "${{ github.token }}":
        return True
    if key_text == "IDEMPOTENCY_KEY" and _is_image_deploy_idempotency_key(value_text):
        return True
    return False


def _is_github_context_or_step_output_reference(value_text: str) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    return bool(GITHUB_CONTEXT_OR_STEP_OUTPUT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_github_step_output_reference(value_text: str) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    body = match.group("body").strip()
    return bool(
        GITHUB_STEP_OUTPUT_REFERENCE_PATTERN.match(body)
        or GITHUB_NEEDS_OUTPUT_REFERENCE_PATTERN.match(body)
    )


def _is_github_same_job_step_output_reference(value_text: str) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    return bool(GITHUB_STEP_OUTPUT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_workflow_read_model_output_forward(*, key: str, value: object) -> bool:
    key_text = _semantic_full_key_text(key)
    if key_text not in {
        "BASE_URL",
        "HEALTH_URLS_JSON",
        "HEALTHCHECK_PATH",
        "PRIMARY_BASE_URL",
    }:
        return False
    value_text = _string_value(value).strip()
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    return bool(GITHUB_NEEDS_OUTPUT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_preview_feedback_url_output_forward(*, key: str, value: object) -> bool:
    if _semantic_full_key_text(key) != "PREVIEW_URL":
        return False
    value_text = _string_value(value).strip()
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    body = match.group("body").strip()
    return body == "needs.provision_preview.outputs.preview_url"


def _is_image_deploy_idempotency_key(value_text: str) -> bool:
    if "${{ secrets." in value_text:
        return False
    if "${{ vars.LAUNCHPLANE_" not in value_text:
        return False
    if "${{ github.event.workflow_run.head_sha }}" not in value_text:
        return False
    return "${{ steps." in value_text and ".outputs.artifact_id }}" in value_text


def _is_workflow_input_mechanic_default(*, path: str, key: str, value: object) -> bool:
    if not _is_workflow_input_default_key(key):
        return False
    allowed_values = WORKFLOW_INPUT_MECHANIC_DEFAULT_PATH_VALUES.get(path, {}).get(key)
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip()
    return value_text in allowed_values


def _is_product_driver_reusable_workflow_mechanic(*, path: str, key: str, value: object) -> bool:
    if not (
        path.startswith(PRODUCT_DRIVER_REUSABLE_WORKFLOW_PATH_PREFIX)
        and path.endswith(PRODUCT_DRIVER_REUSABLE_WORKFLOW_PATH_SUFFIX)
    ):
        return False
    value_text = _string_value(value).strip().rstrip(",")
    key_text = _semantic_full_key_text(key)
    if value_text in PRODUCT_DRIVER_REUSABLE_WRAPPER_LITERAL_VALUES.get(path, {}).get(
        key_text, frozenset()
    ):
        return True
    if key == "launchplane-url":
        return value_text == "${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}"
    if key_text == "LAUNCHPLANE_URL":
        return (
            _is_github_direct_input_reference(value)
            or value_text == "${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}"
        )
    if key == "idempotency-key":
        return value_text == "${{ steps.request.outputs.idempotency_key }}"
    if key == "route-path":
        return value_text == "${{ steps.request.outputs.route_path }}"
    if _is_workflow_input_default_key(key):
        return value_text in PRODUCT_DRIVER_REUSABLE_INPUT_DEFAULT_VALUES
    if key in PRODUCT_DRIVER_REUSABLE_WITH_INPUT_KEYS:
        return (
            _is_github_step_output_reference(value_text)
            or _is_github_direct_input_reference(value)
            or _is_github_bracket_input_reference(value_text)
        )
    if key.startswith("payload-fields."):
        payload_field = key.removeprefix("payload-fields.")
        if payload_field in PRODUCT_DRIVER_TESTING_DEPLOY_PAYLOAD_FIELD_KEYS:
            if path != ".github/workflows/reusable-product-driver-testing-deploy.yml":
                return False
        elif payload_field not in PRODUCT_DRIVER_REUSABLE_PAYLOAD_FIELD_KEYS:
            return False
        return (
            _is_github_step_output_reference(value_text)
            or _is_github_direct_input_reference(value)
            or _is_github_bracket_input_reference(value_text)
        )
    if key_text in {
        "APPLICATION_ID",
        "APPLICATION_NAME",
        "BACKUP_GATE_RECORD_ID",
        "BACKUP_RECORD_ID",
        "BACKUP_STATUS",
        "BASE_URL",
        "CONTEXT",
        "DEPLOYMENT_RECORD_ID",
        "DEPLOYMENT_HEALTH_STATUS",
        "FROM_INSTANCE",
        "HEALTHCHECK_PATH",
        "HEALTH_STATUS",
        "INSTANCE",
        "MAINTENANCE_STATUS",
        "MIGRATION_STATUS",
        "OWNER_ROUTES_STATUS",
        "POST_DEPLOY_STATUS",
        "PRIMARY_BASE_URL",
        "PROMOTION_RECORD_ID",
        "ROLLBACK_HEALTH_STATUS",
        "ROLLBACK_STATUS",
        "ROLLOUT_BASE_URL",
        "ROLLOUT_STATUS",
        "SCHEDULE_NAME",
        "SNAPSHOT_NAME",
        "STATUS",
        "TARGET_CATEGORY",
        "TARGET_ID",
        "TARGET_NAME",
        "TARGET_TYPE",
        "TIMEOUT_MS",
        "POLL_INTERVAL_MS",
        "POLL_TIMEOUT_MS",
        "PRODUCT",
        "TO_INSTANCE",
        "VERIFICATION_STATUS",
    }:
        return (
            _is_github_step_output_reference(value_text)
            or _is_github_direct_input_reference(value)
            or _is_github_bracket_input_reference(value_text)
        )
    return False


def _is_github_bracket_input_reference(value_text: str) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(value_text)
    if match is None:
        return False
    body = match.group("body").strip()
    return bool(re.fullmatch(r"inputs\[['\"][A-Za-z0-9_.-]+['\"]\]", body))


def _is_launchplane_config_authority_workflow_reference(value: object) -> bool:
    return (
        LAUNCHPLANE_CONFIG_AUTHORITY_REUSABLE_WORKFLOW_PATTERN.fullmatch(
            _string_value(value).strip()
        )
        is not None
    )


def _is_launchplane_generic_web_preview_facade_reference(value: object) -> bool:
    return (
        LAUNCHPLANE_GENERIC_WEB_PREVIEW_FACADE_PATTERN.fullmatch(_string_value(value).strip())
        is not None
    )


def _launchplane_config_authority_workflow_revision(value: object) -> str:
    match = LAUNCHPLANE_CONFIG_AUTHORITY_REUSABLE_WORKFLOW_PATTERN.fullmatch(
        _string_value(value).strip()
    )
    if match is None:
        return ""
    revision = match.group("revision")
    return revision if GIT_COMMIT_SHA_PATTERN.fullmatch(revision) is not None else ""


def _is_launchplane_config_authority_connector(
    *,
    key: str,
    value: object,
    allow_context: Mapping[str, object],
) -> bool:
    expected_revision = allow_context.get("launchplane_config_authority_revision")
    if not isinstance(expected_revision, str) or not expected_revision:
        return False
    if key == "uses":
        return _launchplane_config_authority_workflow_revision(value) == expected_revision
    if not _is_launchplane_config_authority_revision_input_key(key):
        return False
    return _string_value(value).strip() == expected_revision


def _is_launchplane_config_authority_revision_input_key(key: str) -> bool:
    return key.startswith("launchplane-config-authority.with[") and key.endswith(
        "].launchplane-revision"
    )


def _is_workflow_thin_connector_key_value(*, path: str, key: str, value: object) -> bool:
    allowed_values = _workflow_path_key_values(
        WORKFLOW_THIN_CONNECTOR_PATH_VALUES,
        path=path,
        key=key,
    )
    if allowed_values is None:
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return value_text in allowed_values


def _is_generic_web_preview_facade_input(*, path: str, key: str) -> bool:
    if path != LAUNCHPLANE_GENERIC_WEB_PREVIEW_CALLER_WORKFLOW_PATH:
        return False
    match = re.fullmatch(r"generic-web-preview\.with\[\d+\]\.(?P<input_name>[A-Za-z0-9_.-]+)", key)
    return (
        match is not None
        and match.group("input_name") in LAUNCHPLANE_GENERIC_WEB_PREVIEW_FACADE_INPUTS
    )


def _is_cleanup_ghcr_product_forward(
    *, path: str, key: str, value: object, allow_context: Mapping[str, object]
) -> bool:
    if path != ".github/workflows/cleanup-ghcr.yml" or key != "product":
        return False
    value_text = _string_value(value).strip().rstrip(",")
    if value_text == "${{ env.LAUNCHPLANE_PRODUCT }}":
        return True
    existing_products = allow_context.get("cleanup_ghcr_launchplane_products")
    return isinstance(existing_products, set) and value_text in existing_products


def _is_launchplane_reusable_workflow(*, key: str, value: object) -> bool:
    if key != "uses":
        return False
    value_text = _string_value(value).strip().rstrip(",")
    return bool(LAUNCHPLANE_REUSABLE_WORKFLOW_PATTERN.match(value_text))


def _workflow_path_key_values(
    path_values: Mapping[str, Mapping[str, frozenset[str]]],
    *,
    path: str,
    key: str,
) -> frozenset[str] | None:
    keyed_values = path_values.get(path, {})
    allowed_values = keyed_values.get(key)
    if allowed_values is not None:
        return allowed_values
    # The YAML block parser emits payload-fields.<field>, while small
    # path allowlists may describe the inner field name directly.
    if key.startswith("payload-fields."):
        return keyed_values.get(key.removeprefix("payload-fields."))
    payload_field_key = f"payload-fields.{key}"
    return keyed_values.get(payload_field_key)


def _is_workflow_payload_field_forward(*, key: str, value: object) -> bool:
    if not _is_workflow_payload_field_key(key):
        return False
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    body = match.group("body").strip()
    return bool(
        GITHUB_DIRECT_INPUT_REFERENCE_PATTERN.match(body)
        or GITHUB_STEP_OUTPUT_REFERENCE_PATTERN.match(body)
        or GITHUB_ENV_REFERENCE_PATTERN.match(body)
    )


def _is_workflow_payload_field_key(key: str) -> bool:
    return key.startswith("payload-fields.")


def _is_route_path_key(key: str) -> bool:
    return key.upper().replace(".", "_").replace("-", "_") == "ROUTE_PATH"


def _is_launchplane_service_route_path(*, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip()
    if key_text != "ROUTE_PATH":
        return False
    if value_text.startswith("/v1/") and "${{" not in value_text and " " not in value_text:
        return True
    return _is_github_route_path_forwarding_reference(value)


def _is_github_route_path_forwarding_reference(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_ROUTE_PATH_FORWARDING_PATTERN.match(match.group("body").strip()))


def _is_click_option_metadata_key(key: str) -> bool:
    normalized = key.lower()
    return normalized.startswith("--") and normalized.rsplit(".", 1)[-1] in {
        "default",
        "envvar",
        "required",
    }


def _is_python_path_schema_only_value(*, path: str, key: str, value: object) -> bool:
    allowed_container_value = PYTHON_SCHEMA_ONLY_PATH_CONTAINER_VALUES.get(path, {}).get(key)
    if allowed_container_value is not None and value == allowed_container_value:
        return True
    allowed_values = PYTHON_SCHEMA_ONLY_PATH_KEY_VALUES.get(path, {}).get(key)
    if allowed_values is None:
        return False
    return _string_value(value).strip().strip("\"'") in allowed_values


def _is_repo_metadata_ergonomics_key(key: str) -> bool:
    return key.startswith(
        (
            "cleanup.",
            "defaultBranch",
            "deployLabels",
            "docs.",
            "githubSignals.",
            "githubSettings.",
            "importantWorkflows",
            "metadataFreshness.",
            "projectType",
            "pullRequests.",
            "qaLabels",
            "qualityGate.",
            "relatedRepos[",
            "reviewPolicy.",
        )
    )


def _is_codeowners_path(path: str) -> bool:
    return path in {".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"}


def _owner_repo_reference_matches(value: str) -> Iterable[re.Match[str]]:
    offset = 0
    while match := OWNER_REPO_PATTERN.search(value, offset):
        preceded_by_path_separator = match.start() > 0 and value[match.start() - 1] in "./\\"
        github_api_repository = value[: match.start()].endswith("/repos/")
        if preceded_by_path_separator and not github_api_repository:
            offset = match.start() + 1
            continue
        yield match
        offset = match.end()


def _contains_owner_repo_reference(value: str) -> bool:
    return next(iter(_owner_repo_reference_matches(value)), None) is not None


def _repo_metadata(root: Path) -> dict[str, object]:
    return {
        "branch": _git_output(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git_output(root, "rev-parse", "HEAD"),
        "dirty": bool(_git_output(root, "status", "--porcelain")),
    }


def _git_file_state(root: Path) -> dict[str, dict[str, str]]:
    state: dict[str, dict[str, str]] = {}
    for relative_path in _git_tracked_relative_paths(root):
        state[relative_path] = {
            "git_status": "tracked",
            "head_blob_sha": _git_output(root, "rev-parse", f"HEAD:{relative_path}"),
            "index_blob_sha": _git_output(root, "rev-parse", f":{relative_path}"),
        }
    for status, relative_path in _git_status_entries(root):
        entry = state.setdefault(relative_path, {})
        entry["git_status"] = status
        entry.setdefault("head_blob_sha", _git_output(root, "rev-parse", f"HEAD:{relative_path}"))
        entry.setdefault("index_blob_sha", _git_output(root, "rev-parse", f":{relative_path}"))
    return state


def _git_tracked_files(root: Path) -> list[Path]:
    return [root / relative_path for relative_path in _git_tracked_relative_paths(root)]


def _git_tracked_relative_paths(root: Path) -> list[str]:
    output = _git_output(root, "ls-files")
    return sorted(line for line in output.splitlines() if line.strip())


def _git_changed_files(root: Path) -> list[Path]:
    changed = set(_git_branch_changed_relative_paths(root))
    changed.update(relative_path for _, relative_path in _git_status_entries(root))
    return sorted(
        root / relative_path for relative_path in changed if (root / relative_path).is_file()
    )


def _git_branch_changed_relative_paths(root: Path) -> list[str]:
    merge_base = _git_merge_base(root)
    if not merge_base:
        return []
    output = _git_output(root, "diff", "--name-only", "--diff-filter=ACMRT", merge_base, "HEAD")
    if not output:
        return []
    return [line for line in output.splitlines() if line]


def _git_merge_base(root: Path) -> str:
    merge_base = _git_output(root, "merge-base", "HEAD", "origin/main")
    if merge_base:
        return merge_base
    return _git_output(root, "merge-base", "HEAD", "main")


def _git_untracked_relative_paths(root: Path) -> list[str]:
    output = _git_output(root, "ls-files", "--others", "--exclude-standard")
    return sorted(line for line in output.splitlines() if line.strip())


def _git_status_entries(root: Path) -> list[tuple[str, str]]:
    output = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip() or "tracked"
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append((status, path))
    return entries


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\n")


def _is_text_scan_candidate(path: Path) -> bool:
    return path.name in SCRIPT_NAMES or path.suffix in TEXT_SCAN_SUFFIXES


def _looks_binary(content: bytes) -> bool:
    return b"\x00" in content


def _parser_name(path: Path) -> str:
    if path.suffix == ".py":
        return "python_ast"
    if path.suffix == ".json":
        return "json"
    if path.suffix == ".toml":
        return "toml"
    if path.suffix in {".yaml", ".yml"}:
        return "yaml_line_scan"
    if path.name.startswith(".env") or path.suffix == ".env":
        return "env_line_scan"
    if path.name == "Dockerfile" or path.suffix == ".Dockerfile":
        return "dockerfile_line_scan"
    return "text_line_scan"


def _flatten_mapping(value: object, prefix: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_mapping(item, child_prefix)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from _flatten_mapping(item, child_prefix)
    else:
        yield prefix, value


def _literal_value(node: ast.AST | None) -> object | None:
    if node is None:
        return None
    try:
        return cast(object, ast.literal_eval(node))
    except (ValueError, TypeError):
        return None


def _mapping_payload(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _list_payload(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _assignment_target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        subscript_value = _literal_value(node.slice)
        if isinstance(subscript_value, str):
            return subscript_value
    return ""


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _assignment_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    parts = text.split()
    index = 0
    while index < len(parts):
        part = parts[index]
        if "=" in part:
            key, value = part.split("=", 1)
            tokens.append((key, _unquote(value)))
        elif index + 1 < len(parts):
            tokens.append((part, _unquote(parts[index + 1])))
            index += 1
        index += 1
    return tokens


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = character if quote is None else None if quote == character else quote
        if character == "#" and quote is None:
            return value[:index]
    return value


def _unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _redacted_evidence(*, key: str, value: object) -> str:
    value_text = _string_value(value)
    leaf_text = _semantic_leaf_text(key)
    full_key_text = _semantic_full_key_text(key)
    if any(part in leaf_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return "<redacted-secret-shaped-value>"
    if "REPOSITORY" in leaf_text or "REPO" in leaf_text:
        return "<redacted-repository>"
    if "DOMAIN" in leaf_text or "URL" in leaf_text:
        return "<redacted-url>"
    if any(
        marker in leaf_text
        for marker in (
            "PRODUCT",
            "TENANT",
            "CONTEXT",
            "INSTANCE",
            "BRANCH",
            "LANE",
            "TARGET",
            "PROVIDER",
            "OPERATOR",
            "SUBJECT",
            "AUTHZ",
        )
    ) or any(
        marker in full_key_text.split("_")
        for marker in ("PRODUCT", "TENANT", "CONTEXT", "INSTANCE", "LANE", "TARGET")
    ):
        return "<redacted-runtime-identity>"
    redacted = URL_PATTERN.sub("<redacted-url>", value_text)
    redacted = OWNER_REPO_PATTERN.sub("<redacted-repository>", redacted)
    if len(redacted) > MAX_EVIDENCE_VALUE_LENGTH:
        redacted = f"{redacted[:MAX_EVIDENCE_VALUE_LENGTH]}..."
    return redacted


def _semantic_leaf_text(key: str) -> str:
    leaf = key.rsplit(".", 1)[-1]
    leaf = leaf.split("[", 1)[0]
    return leaf.upper().replace("-", "_")


def _semantic_full_key_text(key: str) -> str:
    return re.sub(r"\[\d+\]", "", key).upper().replace(".", "_").replace("-", "_")


def _raw_finding_payload(finding: ConfigAuthorityFinding) -> dict[str, object]:
    payload = finding.as_payload()
    payload["raw_omitted"] = True
    return payload


def _finding_id(*, path: str, line: int, rule_id: str, key: str, value_hash: str) -> str:
    digest = _stable_hash(
        {
            "path": path,
            "line": line,
            "rule_id": rule_id,
            "key": key,
            "value_hash": value_hash,
        }
    )
    return f"caf-{digest[:12]}"


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        _json_safe_value(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(HASH_VERSION.encode() + b"\0" + encoded).hexdigest()


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(_json_safe_value(value), sort_keys=True, default=str)


def _json_safe_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_value(item) for item in value]
    return value


def _relative_path(*, root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_scan_path(*, root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path
