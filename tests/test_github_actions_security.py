import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from unittest import TestCase

from tests.support.workflows import SAME_REPO_PULL_REQUEST_IF
from tests.support.workflows import load_workflow


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
    Path(".github/workflows/detached-application-retirement.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-detached-application-retirement.yml"}
    ),
    Path(".github/workflows/product-onboarding-manifest.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-stable-lane-repair.yml"}
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
    "actions/upload-artifact": ActionClassification("GitHub-maintained", "artifact upload"),
    "astral-sh/setup-uv": ActionClassification("Third-party publisher", "Python tool bootstrap"),
    "cbusillo/launchplane/.github/actions/launchplane-request": ActionClassification(
        "First-party cross-repository", "OIDC-authenticated Launchplane API requests"
    ),
    "cbusillo/launchplane/.github/actions/generic-web-deploy-recovery-dry-run": (
        ActionClassification(
            "First-party cross-repository",
            "bounded legacy generic-web deploy reservation inspection",
        )
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
    "cbusillo/launchplane/.github/workflows/reusable-detached-application-retirement.yml": (
        ActionClassification(
            "First-party same-repository",
            "protected immutable detached application retirement",
        )
    ),
    "cbusillo/launchplane/.github/workflows/reusable-stable-lane-repair.yml": (
        ActionClassification(
            "First-party same-repository",
            "protected immutable stable-lane profile repair",
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
    "docker/setup-compose-action": ActionClassification(
        "Third-party publisher", "CI Compose bootstrap"
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


UNTRUSTED_RUN_EXPRESSION_PATTERN = re.compile(
    r"\$\{\{[^}]*\b(?:inputs\s*[.\[]|github\s*(?:\.\s*event\b|\[\s*'event'|\.\s*head_ref\b"
    r"|\[\s*'head_ref'))"
)


def _normalized_expression(expression: str) -> str:
    return " ".join(expression.split())


def _strip_enclosing_parentheses(expression: str) -> str:
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        for index, character in enumerate(expression):
            depth += {"(": 1, ")": -1}.get(character, 0)
            if depth == 0 and index < len(expression) - 1:
                return expression
        expression = expression[1:-1].strip()
    return expression


def _requires_conjunct(expression: str, required: str) -> bool:
    """Whether `required` must hold for the whole expression to be true."""
    expression = _normalized_expression(expression)
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    expression = _strip_enclosing_parentheses(expression)
    required = _strip_enclosing_parentheses(_normalized_expression(required))
    if expression == required:
        return True
    conjuncts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(expression):
        character = expression[index]
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth == 0 and expression.startswith("||", index):
            return False
        if depth == 0 and expression.startswith("&&", index):
            conjuncts.append(expression[start:index].strip())
            start = index + 2
            index += 1
        index += 1
    conjuncts.append(expression[start:].strip())
    return any(
        conjunct != expression and _requires_conjunct(conjunct, required) for conjunct in conjuncts
    )


def _run_scripts() -> Iterator[tuple[Path, str, str]]:
    for path in _action_reference_files():
        data = load_workflow(path).data
        jobs = data.get("jobs")
        step_groups: list[tuple[str, object]] = []
        if isinstance(jobs, dict):
            step_groups.extend(
                (str(job_id), job.get("steps"))
                for job_id, job in jobs.items()
                if isinstance(job, dict)
            )
        runs = data.get("runs")
        if isinstance(runs, dict):
            step_groups.append(("runs", runs.get("steps")))
        for group, steps in step_groups:
            if not isinstance(steps, list):
                continue
            for step in steps:
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    yield path, f"{group}:{step.get('name') or step.get('id') or '?'}", step["run"]


def _action_reference_files() -> tuple[Path, ...]:
    workflow_root = Path(".github/workflows")
    workflow_files = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
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
    def test_local_launchplane_request_actions_use_trusted_same_repo_checkout(self) -> None:
        violations: list[str] = []
        workflow_root = Path(".github/workflows")
        workflow_paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
        for path in workflow_paths:
            workflow = load_workflow(path)
            trigger = workflow.data.get("on")
            if not isinstance(trigger, dict):
                trigger = {}
            for job_id in workflow.jobs:
                local_steps = [
                    step
                    for step in workflow.steps(job_id)
                    if step.uses == "./.github/actions/launchplane-request"
                ]
                if not local_steps:
                    continue
                checkout_steps = [
                    step
                    for step in workflow.steps(job_id)
                    if step.uses.startswith("actions/checkout@")
                ]
                if "workflow_call" in trigger:
                    violations.append(
                        f"{path}:{job_id}: workflow_call jobs must use the immutable remote "
                        "launchplane-request action because local action code can come from the caller."
                    )
                if len(checkout_steps) != 1:
                    violations.append(
                        f"{path}:{job_id}: local launchplane-request use requires exactly one "
                        "trusted checkout step."
                    )
                    continue
                checkout = checkout_steps[0]
                if "repository" in checkout.with_values or "ref" in checkout.with_values:
                    violations.append(
                        f"{path}:{job_id}: local launchplane-request checkout must use the "
                        "workflow's exact same-repository revision without repository/ref overrides."
                    )

        self.assertEqual([], violations)

    def test_pull_request_jobs_on_self_hosted_runners_exclude_forks(self) -> None:
        same_repo_guard = f"({SAME_REPO_PULL_REQUEST_IF})"
        violations: list[str] = []
        workflow_root = Path(".github/workflows")
        workflow_paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
        for path in workflow_paths:
            workflow = load_workflow(path)
            trigger = workflow.data.get("on")
            if isinstance(trigger, dict):
                events = set(trigger)
            elif isinstance(trigger, list):
                events = {str(event) for event in trigger}
            else:
                events = {str(trigger)}
            if "pull_request" not in events:
                continue
            for job_id in workflow.jobs:
                job = workflow.job(job_id)
                runs_on = job.get("runs-on")
                labels = runs_on if isinstance(runs_on, list) else [runs_on]
                if not any("self-hosted" in str(label) for label in labels):
                    continue
                if not _requires_conjunct(str(job.get("if", "")), same_repo_guard):
                    violations.append(
                        f"{path}:{job_id}: self-hosted pull_request jobs must be gated to "
                        "same-repository pull requests."
                    )

        self.assertEqual([], violations)

    def test_same_repository_guard_must_hold_for_the_whole_condition(self) -> None:
        guard = f"({SAME_REPO_PULL_REQUEST_IF})"
        for expression, required in (
            (f"needs.a.outputs.verified != 'true' && {guard}", True),
            (f"{guard} && always()", True),
            (f"({guard})", True),
            (f"{guard} || needs.a.outputs.verified != 'true'", False),
            (f"(needs.a.outputs.verified != 'true' || {guard}) && always()", False),
            ("needs.a.outputs.verified != 'true'", False),
            (f"${{{{ needs.a.outputs.verified != 'true' && {guard} }}}}", True),
            (f"(always() && {guard}) && needs.a.result == 'success'", True),
            (f"(always() || {guard}) && needs.a.result == 'success'", False),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(_requires_conjunct(expression, guard), required)

    def test_untrusted_expression_pattern_covers_index_and_multiline_forms(self) -> None:
        for script, matches in (
            ('echo "${{ inputs.command }}"', True),
            ("echo \"${{ inputs['command'] }}\"", True),
            ("echo \"${{ github['event']['pull_request']['title'] }}\"", True),
            ('echo "${{\n  inputs.command }}"', True),
            ('echo "${{ github.head_ref }}"', True),
            ('echo "${{ github.event_name }}"', False),
            ('echo "${{ steps.request.outputs.url }}"', False),
            ('echo "$INPUT_COMMAND"', False),
        ):
            with self.subTest(script=script):
                self.assertEqual(
                    UNTRUSTED_RUN_EXPRESSION_PATTERN.search(script) is not None, matches
                )

    def test_run_scripts_read_untrusted_values_through_the_environment(self) -> None:
        # An expression in a run script is pasted into the shell before it runs,
        # so a crafted input or event field becomes code. Pass it through env.
        violations = [
            f"{path}:{step}: {match.group(0)}"
            for path, step, script in _run_scripts()
            for match in UNTRUSTED_RUN_EXPRESSION_PATTERN.finditer(script)
        ]

        self.assertEqual([], violations)

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
            if source not in APPROVED_REMOTE_ACTIONS:
                violations.append(
                    f"{action.location}: unclassified remote action source {source!r}."
                )
            if FULL_SHA_PATTERN.fullmatch(revision) is None:
                violations.append(
                    f"{action.location}: remote action {source!r} must use a 40-character SHA."
                )
            if action.provenance is None:
                violations.append(
                    f"{action.location}: remote action {source!r} must document its provenance."
                )
            elif source == ("cbusillo/launchplane/.github/actions/launchplane-request"):
                if action.provenance != "launchplane-request":
                    violations.append(
                        f"{action.location}: first-party cross-repository action provenance "
                        "must be 'launchplane-request'."
                    )
            elif source.startswith(FIRST_PARTY_CROSS_REPOSITORY_ACTION_PREFIX):
                if action.provenance != "main":
                    violations.append(
                        f"{action.location}: first-party cross-repository action provenance "
                        "must be 'main'."
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

    def test_static_container_references_are_classified_and_digest_pinned(self) -> None:
        violations: list[str] = []

        for image in _container_references():
            if image.source not in APPROVED_CONTAINER_IMAGES:
                violations.append(
                    f"{image.location}: unclassified container image source {image.source!r}."
                )
            if image.digest is None:
                violations.append(
                    f"{image.location}: container image {image.source!r} must use a sha256 digest."
                )
            if CONTAINER_TAG_PATTERN.fullmatch(image.tag) is None:
                violations.append(
                    f"{image.location}: container image tag {image.tag!r} is not reviewable."
                )

        self.assertFalse(violations, "\n".join(violations))
