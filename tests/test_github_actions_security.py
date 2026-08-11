import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from unittest import TestCase

from tests.support.workflows import WorkflowInvariantViolation
from tests.support.workflows import check_security_policy_runs_for_all_pull_requests
from tests.support.workflows import load_workflow


def _assert_no_workflow_violations(
    test_case: TestCase,
    violations: tuple[WorkflowInvariantViolation, ...],
) -> None:
    test_case.assertEqual([], [str(violation) for violation in violations])


USES_LINE_PATTERN = re.compile(
    r"^\s*(?:-\s+)?uses:\s*(?P<reference>[^#\s]+)(?:\s+#\s*(?P<provenance>.+?))?\s*$"
)
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PROVENANCE_PATTERN = re.compile(r"^v\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
CONTAINER_TAG_PATTERN = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
STATIC_CONTAINER_REFERENCE_PATTERN = re.compile(
    r"(?P<source>(?:[a-z0-9.-]+/)+[a-z0-9._/-]+|postgres):"
    r"(?P<tag>v?[0-9][A-Za-z0-9._-]*)"
    r"(?:@sha256:(?P<digest>[0-9a-f]{64}))?"
)
LOCAL_REFERENCE_PREFIXES = ("./.github/actions/", "./.github/workflows/")
SELF_REUSABLE_WORKFLOW_PREFIX = "cbusillo/launchplane/.github/workflows/"
FIRST_PARTY_CROSS_REPOSITORY_ACTION_PREFIX = "cbusillo/launchplane/.github/actions/"
MUTABLE_REFERENCE_ALLOWLIST: Mapping[Path, frozenset[str]] = {}
PINNED_SELF_REUSABLE_WORKFLOWS: Mapping[Path, frozenset[str]] = {
    Path(".github/workflows/product-onboarding.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-generic-web-onboarding-apply.yml"}
    ),
    Path(".github/workflows/generic-web-preview-authorization.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-authz-apply.yml"}
    ),
    Path(".github/workflows/authz-policy-reconcile.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml"}
    ),
    Path(".github/workflows/deploy-launchplane.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml"}
    ),
    Path(".github/workflows/route-binding-reconcile.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-route-binding-reconcile.yml"}
    ),
    Path(".github/workflows/external-route-binding-reconcile.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-external-route-binding-reconcile.yml"}
    ),
    Path(".github/workflows/ingress-route-apply.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-ingress-route-apply.yml"}
    ),
    Path(".github/workflows/ingress-route-dry-run.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-ingress-route-dry-run.yml"}
    ),
    Path(".github/workflows/product-health-monitoring.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-product-health-monitoring.yml"}
    ),
    Path(".github/workflows/product-prelaunch-rebuild-policy.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-product-prelaunch-rebuild-policy.yml"}
    ),
    Path(".github/workflows/odoo-artifact-publish.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-artifact-publish.yml"}
    ),
    Path(".github/workflows/odoo-testing-route-binding-refresh.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-testing-route-binding-refresh.yml"}
    ),
    Path(".github/workflows/odoo-target-replacement-plan.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-target-replacement-plan.yml"}
    ),
    Path(".github/workflows/odoo-target-replacement-apply.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-target-replacement-apply.yml"}
    ),
    Path(".github/workflows/odoo-website-bootstrap-override.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-website-bootstrap-override.yml"}
    ),
    Path(".github/workflows/odoo-prod-backup-verification.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-prod-backup-verification.yml"}
    ),
    Path(".github/workflows/odoo-prod-backup-restore-plan.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-prod-backup-restore-plan.yml"}
    ),
    Path(".github/workflows/odoo-prod-backup-restore-apply.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-odoo-prod-backup-restore-apply.yml"}
    ),
    Path(".github/workflows/odoo-prod-retained-volume-backup-import-plan.yml"): frozenset(
        {
            "cbusillo/launchplane/.github/workflows/"
            "reusable-odoo-prod-retained-volume-backup-import-plan.yml"
        }
    ),
    Path(".github/workflows/odoo-prod-retained-volume-backup-import-apply.yml"): frozenset(
        {
            "cbusillo/launchplane/.github/workflows/"
            "reusable-odoo-prod-retained-volume-backup-import-apply.yml"
        }
    ),
    Path(".github/workflows/tracked-target-logs.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-tracked-target-logs.yml"}
    ),
    Path(".github/workflows/product-retirement.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-product-retirement.yml"}
    ),
}


@dataclass(frozen=True)
class ActionClassification:
    trust: str
    privilege: str


@dataclass(frozen=True)
class ActionReference:
    path: Path
    line_number: int
    reference: str
    provenance: str | None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line_number}"


@dataclass(frozen=True)
class ContainerReference:
    path: Path
    line_number: int
    source: str
    tag: str
    digest: str | None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line_number}"


APPROVED_REMOTE_ACTIONS: Mapping[str, ActionClassification] = {
    "actions/cache": ActionClassification("GitHub-maintained", "cache transport"),
    "actions/cache/restore": ActionClassification("GitHub-maintained", "cache restore"),
    "actions/cache/save": ActionClassification("GitHub-maintained", "cache persistence"),
    "actions/checkout": ActionClassification("GitHub-maintained", "repository checkout"),
    "actions/create-github-app-token": ActionClassification(
        "GitHub-maintained", "short-lived GitHub App token minting"
    ),
    "actions/download-artifact": ActionClassification("GitHub-maintained", "artifact download"),
    "actions/github-script": ActionClassification("GitHub-maintained", "GitHub API interaction"),
    "actions/setup-node": ActionClassification("GitHub-maintained", "Node runtime bootstrap"),
    "actions/setup-python": ActionClassification("GitHub-maintained", "Python runtime bootstrap"),
    "actions/upload-artifact": ActionClassification("GitHub-maintained", "artifact upload"),
    "astral-sh/setup-uv": ActionClassification("Third-party publisher", "Python tool bootstrap"),
    "cbusillo/launchplane/.github/actions/launchplane-request": ActionClassification(
        "First-party cross-repository", "OIDC-authenticated Launchplane API requests"
    ),
    "cbusillo/launchplane/.github/actions/setup-odoo-preview-request-client": (
        ActionClassification("First-party cross-repository", "preview request client setup")
    ),
    "cbusillo/launchplane/.github/workflows/reusable-authz-policy-reconcile.yml": (
        ActionClassification("First-party same-repository", "authorization policy administration")
    ),
    "cbusillo/launchplane/.github/workflows/reusable-generic-web-onboarding-apply.yml": (
        ActionClassification(
            "First-party same-repository",
            "protected generic-web target, record, and authorization apply",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-authz-apply.yml": (
        ActionClassification(
            "First-party same-repository",
            "protected generic-web preview authorization rotation and retirement",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-route-binding-reconcile.yml": (
        ActionClassification("First-party same-repository", "route authority reconciliation")
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-testing-route-binding-refresh.yml": (
        ActionClassification(
            "First-party same-repository",
            "testing route-binding evidence refresh",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-target-replacement-plan.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo target replacement planning",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-target-replacement-apply.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo target replacement apply",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-website-bootstrap-override.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo website-bootstrap repair",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-prod-backup-verification.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo production backup verification",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-prod-backup-restore-plan.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo production backup restore planning",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-prod-backup-restore-apply.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo production backup restore apply",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-prod-retained-volume-backup-import-plan.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo retained-volume backup import planning",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-prod-retained-volume-backup-import-apply.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance Odoo retained-volume backup import apply",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-external-route-binding-reconcile.yml": (
        ActionClassification(
            "First-party same-repository", "external route authority reconciliation"
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-ingress-route-apply.yml": (
        ActionClassification(
            "First-party same-repository", "exact-instance reviewed ingress evidence apply"
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-ingress-route-dry-run.yml": (
        ActionClassification(
            "First-party same-repository", "exact-instance ingress route inspection"
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-product-health-monitoring.yml": (
        ActionClassification(
            "First-party same-repository", "exact-instance product health policy mutation"
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-product-prelaunch-rebuild-policy.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance product prelaunch rebuild policy mutation",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-odoo-artifact-publish.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance immutable Odoo artifact publication",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-tracked-target-logs.yml": (
        ActionClassification(
            "First-party same-repository",
            "exact-instance redacted target-log diagnostics",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-product-retirement.yml": (
        ActionClassification(
            "First-party same-repository",
            "protected immutable product retirement",
        )
    ),
    "docker/build-push-action": ActionClassification(
        "Third-party publisher", "container build and publication"
    ),
    "docker/login-action": ActionClassification(
        "Third-party publisher", "container registry authentication"
    ),
    "docker/setup-buildx-action": ActionClassification(
        "Third-party publisher", "container build bootstrap"
    ),
    "github/codeql-action/analyze": ActionClassification(
        "GitHub-maintained", "code scanning analysis"
    ),
    "github/codeql-action/init": ActionClassification(
        "GitHub-maintained", "code scanning initialization"
    ),
}
APPROVED_CONTAINER_IMAGES: Mapping[str, ActionClassification] = {
    "ghcr.io/aquasecurity/trivy": ActionClassification(
        "Third-party publisher", "runtime image vulnerability scanning"
    ),
    "ghcr.io/gitleaks/gitleaks": ActionClassification(
        "Third-party publisher", "repository secret scanning"
    ),
    "postgres": ActionClassification("Official image", "integration-test database"),
    "rhysd/actionlint": ActionClassification("Third-party publisher", "workflow linting"),
}


def _action_reference_files() -> tuple[Path, ...]:
    workflow_files = sorted(Path(".github/workflows").glob("*.yml"))
    composite_action_files = sorted(Path(".github/actions").rglob("action.y*ml"))
    return tuple(workflow_files + composite_action_files)


def _action_references() -> Iterator[ActionReference]:
    for path in _action_reference_files():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES_LINE_PATTERN.match(line)
            if match is None:
                continue
            yield ActionReference(
                path=path,
                line_number=line_number,
                reference=match.group("reference"),
                provenance=match.group("provenance"),
            )


def _container_references() -> Iterator[ContainerReference]:
    for path in sorted(Path(".github/workflows").glob("*.yml")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in STATIC_CONTAINER_REFERENCE_PATTERN.finditer(line):
                yield ContainerReference(
                    path=path,
                    line_number=line_number,
                    source=match.group("source"),
                    tag=match.group("tag"),
                    digest=match.group("digest"),
                )


class GitHubActionsSecurityTests(TestCase):
    def test_product_repo_config_authority_uses_called_workflow_revision(self) -> None:
        workflow = Path(".github/workflows/reusable-product-repo-config-authority.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("launchplane-revision:", workflow)
        self.assertIn("launchplane-revision must be a 40-character commit SHA", workflow)
        self.assertIn("JOB_CONTEXT_JSON: ${{ toJSON(job) }}", workflow)
        self.assertIn(".workflow_sha", workflow)
        self.assertIn("launchplane-revision must match the called workflow commit SHA", workflow)
        self.assertIn("ref: ${{ inputs.launchplane-revision }}", workflow)
        self.assertNotIn("ref: main", workflow)

    def test_product_repo_config_authority_revision_validation_fails_closed(self) -> None:
        workflow = load_workflow(".github/workflows/reusable-product-repo-config-authority.yml")
        step = workflow.step_named(
            "launchplane-config-authority",
            "Validate Launchplane audit tool revision",
        )
        self.assertIsNotNone(step)
        assert step is not None

        revision = "a" * 40
        cases: tuple[tuple[str, str, dict[str, object], int], ...] = (
            ("valid", revision, {"workflow_sha": revision}, 0),
            ("missing", revision, {}, 1),
            ("non-string", revision, {"workflow_sha": 123}, 1),
            ("malformed", revision, {"workflow_sha": "a" * 39}, 1),
            ("uppercase", revision, {"workflow_sha": "A" * 40}, 1),
            ("non-hex", revision, {"workflow_sha": "g" * 40}, 1),
            ("trailing-newline", revision, {"workflow_sha": f"{revision}\n"}, 1),
            ("mismatch", revision, {"workflow_sha": "b" * 40}, 1),
        )
        for case_name, input_revision, job_context, expected_exit_code in cases:
            with self.subTest(case_name=case_name):
                result = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-e",
                        "-o",
                        "pipefail",
                        "-c",
                        step.run,
                    ],
                    check=False,
                    capture_output=True,
                    env=os.environ
                    | {
                        "JOB_CONTEXT_JSON": json.dumps(job_context),
                        "LAUNCHPLANE_REVISION": input_revision,
                    },
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    expected_exit_code,
                    f"stdout={result.stdout}\nstderr={result.stderr}",
                )

    def test_action_reference_parser_covers_inline_step_syntax(self) -> None:
        match = USES_LINE_PATTERN.match(
            "      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0"
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(
            match.group("reference"),
            "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        )
        self.assertEqual(match.group("provenance"), "v7.0.0")

    def test_remote_action_references_are_classified_and_immutably_pinned(self) -> None:
        observed_sources: set[str] = set()
        violations: list[str] = []

        for action in _action_references():
            if action.reference.startswith("./"):
                if not action.reference.startswith(LOCAL_REFERENCE_PREFIXES):
                    violations.append(
                        f"{action.location}: unsupported local action reference {action.reference!r}."
                    )
                if "@" in action.reference:
                    violations.append(
                        f"{action.location}: local action reference must not include a ref."
                    )
                continue

            if action.reference in MUTABLE_REFERENCE_ALLOWLIST.get(action.path, frozenset()):
                continue

            source, separator, revision = action.reference.rpartition("@")
            if not separator:
                violations.append(
                    f"{action.location}: remote action reference must include a full commit SHA."
                )
                continue
            if source.startswith(SELF_REUSABLE_WORKFLOW_PREFIX):
                allowed_pinned_sources = PINNED_SELF_REUSABLE_WORKFLOWS.get(
                    action.path, frozenset()
                )
                if source not in allowed_pinned_sources:
                    violations.append(
                        f"{action.location}: same-repository reusable workflows must use a "
                        "relative path unless the exact pinned identity is an approved trust anchor."
                    )
            classification = APPROVED_REMOTE_ACTIONS.get(source)
            if classification is None:
                violations.append(
                    f"{action.location}: unclassified remote action source {source!r}."
                )
            else:
                observed_sources.add(source)
            if FULL_SHA_PATTERN.fullmatch(revision) is None:
                violations.append(
                    f"{action.location}: remote action {source!r} must use a 40-character SHA."
                )
            if action.provenance is None:
                violations.append(
                    f"{action.location}: remote action {source!r} must document its provenance."
                )
            elif source.startswith(FIRST_PARTY_CROSS_REPOSITORY_ACTION_PREFIX):
                if action.provenance != "main":
                    violations.append(
                        f"{action.location}: first-party cross-repository action provenance must be 'main'."
                    )
            elif source.startswith(SELF_REUSABLE_WORKFLOW_PREFIX):
                if action.provenance != "main":
                    violations.append(
                        f"{action.location}: pinned same-repository workflow provenance must be 'main'."
                    )
            elif VERSION_PROVENANCE_PATTERN.fullmatch(action.provenance) is None:
                violations.append(
                    f"{action.location}: release provenance must use a version tag, not {action.provenance!r}."
                )

        self.assertFalse(violations, "\n".join(violations))
        self.assertSetEqual(set(APPROVED_REMOTE_ACTIONS), observed_sources)

    def test_static_container_references_are_classified_and_digest_pinned(self) -> None:
        observed_sources: set[str] = set()
        violations: list[str] = []

        for image in _container_references():
            classification = APPROVED_CONTAINER_IMAGES.get(image.source)
            if classification is None:
                violations.append(
                    f"{image.location}: unclassified container image source {image.source!r}."
                )
            else:
                observed_sources.add(image.source)
            if image.digest is None:
                violations.append(
                    f"{image.location}: container image {image.source!r} must use a sha256 digest."
                )
            if CONTAINER_TAG_PATTERN.fullmatch(image.tag) is None:
                violations.append(
                    f"{image.location}: container image tag {image.tag!r} is not reviewable."
                )

        self.assertFalse(violations, "\n".join(violations))
        self.assertSetEqual(set(APPROVED_CONTAINER_IMAGES), observed_sources)

    def test_security_gate_runs_action_pinning_policy_for_all_pull_requests(self) -> None:
        security_workflow = load_workflow(".github/workflows/security.yml")

        _assert_no_workflow_violations(
            self,
            check_security_policy_runs_for_all_pull_requests(security_workflow),
        )

    def test_retirement_skips_repository_app_token_and_github_metadata_calls(self) -> None:
        workflow = load_workflow(".github/workflows/generic-web-preview-authorization.yml")

        for step_name in (
            "Mint repository metadata token",
            "Resolve immutable repository identity",
        ):
            step = workflow.step_named("plan", step_name)
            self.assertIsNotNone(step)
            assert step is not None
            self.assertEqual(step.data.get("if"), "${{ inputs.operation != 'retire' }}")
        token_step = workflow.step_named("plan", "Mint repository metadata token")
        assert token_step is not None
        self.assertEqual(
            token_step.uses.split("@", maxsplit=1)[0], "actions/create-github-app-token"
        )

    def test_documentation_and_dependabot_preserve_reviewable_pin_updates(self) -> None:
        docs_index = Path("docs/README.md").read_text(encoding="utf-8")
        policy = Path("docs/github-actions-security.md").read_text(encoding="utf-8")
        dependabot = Path(".github/dependabot.yml").read_text(encoding="utf-8")

        self.assertIn("github-actions-security.md", docs_index)
        self.assertIn("GitHub-maintained", policy)
        self.assertIn("Third-party publisher", policy)
        self.assertIn("First-party cross-repository", policy)
        self.assertIn("High-privilege", policy)
        self.assertIn("protected immutable product retirement", policy)
        self.assertIn("container image", policy)
        self.assertIn("MUTABLE_REFERENCE_ALLOWLIST", policy)
        self.assertIn("Dependabot", policy)
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertIn("interval: weekly", dependabot)
