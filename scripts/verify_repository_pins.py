from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from collections.abc import Iterator, Mapping

import yaml

from control_plane.first_party_action_pins import ActionPinViolation, build_action_pin_report

WRAPPER_PATHS = (
    ".github/workflows/product-retirement.yml",
    ".github/workflows/detached-application-retirement.yml",
)


class WorkflowLoader(yaml.SafeLoader):
    """Keep the workflow 'on' key and YAML 1.2 boolean semantics."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in values
        if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
    ]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _read_workflow(root: Path, revision: str, path: str) -> dict[str, object]:
    result = subprocess.run(
        ("git", "show", f"{revision}:{path}"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise ValueError(f"Cannot read workflow {path} at {revision}.")
    try:
        data = yaml.load(result.stdout, Loader=WorkflowLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"Cannot parse workflow {path} at {revision}.") from error
    if not isinstance(data, dict):
        raise ValueError(f"Workflow {path} must be a mapping.")
    return data


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Workflow inputs and jobs must be mappings.")
    return value


def verify_worker_pins(root: Path, revision: str, repository: str) -> list[str]:
    violations: list[str] = []
    for path in WRAPPER_PATHS:
        wrapper = _read_workflow(root, revision, path)
        job = _mapping(_mapping(wrapper["jobs"])["retire"])
        reference = job["uses"]
        if not isinstance(reference, str):
            raise ValueError(f"Worker reference in {path} must be a string.")
        source, separator, pin = reference.partition("@")
        prefix = f"{repository}/.github/workflows/"
        if (
            not separator
            or not source.startswith(prefix)
            or len(pin) != 40
            or any(c not in "0123456789abcdef" for c in pin)
        ):
            raise ValueError(f"Invalid pinned worker reference in {path}.")
        worker_path = ".github/workflows/" + source.removeprefix(prefix)
        if "/" in source.removeprefix(prefix):
            raise ValueError(f"Invalid worker path in {path}.")
        worker = _read_workflow(root, pin, worker_path)
        expected = _mapping(_mapping(_mapping(worker["on"])["workflow_call"])["inputs"])
        actual = _mapping(_mapping(_mapping(wrapper["on"])["workflow_dispatch"])["inputs"])
        forwarded = _mapping(job["with"])
        if set(actual) != set(expected) or set(forwarded) != set(expected):
            violations.append(f"{path}: wrapper and pinned worker input names disagree")
            continue
        for name, worker_input in expected.items():
            caller_input = _mapping(actual[name])
            worker_input = _mapping(worker_input)
            if caller_input.get("required") != worker_input.get("required"):
                violations.append(f"{path}: {name} required flag disagrees")
            if path.endswith("detached-application-retirement.yml"):
                keys = ("type", "default")
            else:
                keys = () if name == "mode" else ("default",)
            for key in keys:
                if (key in worker_input or key in caller_input) and caller_input.get(
                    key
                ) != worker_input.get(key):
                    violations.append(f"{path}: {name} {key} disagrees")
    return violations


VERSION_PROVENANCE_PATTERN = re.compile(r"^v\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")


CONTAINER_TAG_PATTERN = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")


LOCAL_REFERENCE_PREFIXES = ("./.github/actions/", "./.github/workflows/")


SELF_REUSABLE_WORKFLOW_PREFIX = "cbusillo/launchplane/.github/workflows/"


FIRST_PARTY_CROSS_REPOSITORY_ACTION_PREFIX = "cbusillo/launchplane/.github/actions/"


PINNED_SELF_REUSABLE_WORKFLOWS: Mapping[Path, frozenset[str]] = {
    Path(".github/workflows/product-onboarding.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-generic-web-onboarding-apply.yml"}
    ),
    Path(".github/workflows/generic-web-preview-authorization.yml"): frozenset(
        {"cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-authz-apply.yml"}
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


APPROVED_REMOTE_ACTIONS = frozenset(
    (
        "actions/cache",
        "actions/cache/restore",
        "actions/cache/save",
        "actions/checkout",
        "actions/create-github-app-token",
        "actions/download-artifact",
        "actions/github-script",
        "actions/setup-node",
        "actions/upload-artifact",
        "astral-sh/setup-uv",
        "cbusillo/launchplane/.github/actions/launchplane-request",
        "cbusillo/launchplane/.github/actions/generic-web-deploy-recovery-dry-run",
        "docker/build-push-action",
        "docker/login-action",
        "docker/setup-buildx-action",
        "github/codeql-action/analyze",
        "github/codeql-action/init",
    )
) | frozenset(source for sources in PINNED_SELF_REUSABLE_WORKFLOWS.values() for source in sources)


APPROVED_CONTAINER_IMAGES = frozenset(
    (
        "mcr.microsoft.com/playwright",
        "ghcr.io/aquasecurity/trivy",
        "ghcr.io/gitleaks/gitleaks",
        "postgres",
        "rhysd/actionlint",
    )
)


UNTRUSTED_RUN_EXPRESSION_PATTERN = re.compile(
    r"\$\{\{[^}]*\b(?:inputs\s*[.\[]|github\s*(?:\.\s*event\b|\[\s*'event'|\.\s*head_ref\b"
    r"|\[\s*'head_ref'))"
)


def _normalized_expression(expression: str) -> str:
    return " ".join(expression.split())


def _expression_tokens(expression: str) -> Iterator[tuple[int, str]]:
    quote: str | None = None
    index = 0
    while index < len(expression):
        character = expression[index]
        if quote is not None:
            if character == quote:
                if expression[index : index + 2] == quote * 2:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
        else:
            yield index, character
        index += 1


def _strip_enclosing_parentheses(expression: str) -> str:
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        for index, character in _expression_tokens(expression):
            depth += {"(": 1, ")": -1}.get(character, 0)
            if depth == 0 and index < len(expression) - 1:
                return expression
        expression = expression[1:-1].strip()
    return expression


def _requires_conjunct(expression: str, required: str) -> bool:
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
    for index, character in _expression_tokens(expression):
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth == 0 and expression.startswith("||", index):
            return False
        if depth == 0 and expression.startswith("&&", index):
            conjuncts.append(expression[start:index].strip())
            start = index + 2
    conjuncts.append(expression[start:].strip())
    return any(
        conjunct != expression and _requires_conjunct(conjunct, required) for conjunct in conjuncts
    )


def _runner_expression_excludes_forks(expression: str) -> bool:
    match = re.fullmatch(
        r"\$\{\{\s*fromJSON\((?P<condition>.*?)\s*&&\s*"
        r"'(?P<reserved>\[.*?])'\s*\|\|\s*'(?P<hosted>\[.*?])'\s*\)\s*}}",
        expression,
        flags=re.DOTALL,
    )
    if match is None:
        return False
    try:
        reserved = json.loads(match["reserved"])
        hosted = json.loads(match["hosted"])
    except json.JSONDecodeError:
        return False
    return (
        isinstance(reserved, list)
        and "self-hosted" in reserved
        and isinstance(hosted, list)
        and bool(hosted)
        and all(isinstance(label, str) and label != "self-hosted" for label in hosted)
        and _requires_conjunct(match["condition"], SAME_REPO_PULL_REQUEST_IF)
    )


SAME_REPO_PULL_REQUEST_IF = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)
STATIC_CONTAINER_REFERENCE_PATTERN = re.compile(
    r"(?<![/\w.-])(?P<source>(?:[a-z0-9.-]+/)+[a-z0-9._/-]+|postgres):"
    r"(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]*)"
    r"(?:@sha256:(?P<digest>[0-9a-f]{64}))?"
)
REAL_TOPOLOGY_PATTERNS = (
    re.compile(r"^\s*default:\s*(?:sellyouroutboard|odoo-tenant-\S*|cm|opw)\s*$", re.MULTILINE),
    re.compile(r"sellyouroutboard", re.IGNORECASE),
    re.compile(r"shinycomputers\.com"),
    re.compile(r"odoo-tenant-(?:cm|opw)\b"),
    re.compile(r"allowed_targets="),
)


def workflow_violations(
    text: str, *, path: Path, playwright_version: str | None = None
) -> list[ActionPinViolation]:
    loader = WorkflowLoader(text)
    try:
        node = loader.get_single_node()
        data = loader.construct_document(node) if node is not None else None
    finally:
        loader.dispose()
    if not isinstance(data, dict):
        raise ValueError(f"Workflow {path} must be a mapping.")
    reference_lines: dict[int, int] = {}
    visited: set[int] = set()

    def locate_references(value: object, source: yaml.Node) -> None:
        if not isinstance(value, (dict, list)) or id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, dict) and isinstance(source, yaml.MappingNode):
            for key, child in source.value:
                if key.value == "uses":
                    reference_lines[id(value)] = child.start_mark.line
                locate_references(value.get(key.value), child)
        elif isinstance(value, list) and isinstance(source, yaml.SequenceNode):
            for child_value, child in zip(value, source.value, strict=True):
                locate_references(child_value, child)

    assert node is not None
    locate_references(data, node)
    lines = text.splitlines()
    violations: list[ActionPinViolation] = []

    def reject(code: str, message: str, *, line_number: int | None = None) -> None:
        violations.append(
            ActionPinViolation(code=code, message=message, path=path, line_number=line_number)
        )

    def check_reference(site: Mapping[str, object]) -> None:
        reference = site["uses"]
        if not isinstance(reference, str):
            reject("action_reference_invalid", "Action references must be strings.")
            return
        if reference.startswith("./"):
            if not reference.startswith(LOCAL_REFERENCE_PREFIXES) or "@" in reference:
                reject(
                    "local_action_reference_invalid",
                    "Local actions must use a repository-relative path without a ref.",
                )
            return
        source, separator, revision = reference.rpartition("@")
        if not separator or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            reject(
                "action_reference_not_immutable",
                "Remote actions require a full lowercase commit SHA.",
            )
        if source not in APPROVED_REMOTE_ACTIONS:
            reject("action_source_unclassified", f"Unclassified remote action source: {source}.")
        if source.startswith(
            SELF_REUSABLE_WORKFLOW_PREFIX
        ) and source not in PINNED_SELF_REUSABLE_WORKFLOWS.get(path, frozenset()):
            reject(
                "worker_identity_unapproved",
                "Repository-qualified workers must be approved for their caller.",
            )
        line = reference_lines[id(site)]
        provenance = lines[line].partition("#")[2].strip()
        if source == "cbusillo/launchplane/.github/actions/launchplane-request":
            valid_provenance = provenance == "launchplane-request"
        elif source.startswith(
            (FIRST_PARTY_CROSS_REPOSITORY_ACTION_PREFIX, SELF_REUSABLE_WORKFLOW_PREFIX)
        ):
            valid_provenance = provenance == "main"
        else:
            valid_provenance = VERSION_PROVENANCE_PATTERN.fullmatch(provenance) is not None
        if not valid_provenance:
            reject(
                "action_provenance_invalid",
                "Remote pins must retain their release or first-party provenance.",
                line_number=line + 1,
            )

    def check_image(image: object) -> None:
        match = (
            STATIC_CONTAINER_REFERENCE_PATTERN.fullmatch(image) if isinstance(image, str) else None
        )
        if match is None:
            reject(
                "container_reference_invalid",
                "Container images require a static classified source, release tag and digest.",
            )
            return
        if match["source"] not in APPROVED_CONTAINER_IMAGES:
            reject("container_source_unclassified", "Container image source is unclassified.")
        if match["digest"] is None:
            reject("container_digest_missing", "Container images require a sha256 manifest digest.")
        if CONTAINER_TAG_PATTERN.fullmatch(match["tag"]) is None:
            reject("container_tag_invalid", "Container image tags must name a reviewable release.")
        if match["source"] == "mcr.microsoft.com/playwright":
            image_version = match["tag"].removeprefix("v").split("-", 1)[0]
            if playwright_version is None or image_version != playwright_version:
                reject(
                    "browser_image_version_mismatch",
                    "Browser images must match the committed frontend Playwright dependency.",
                )

    def check_values(value: object) -> None:
        if isinstance(value, dict):
            for child in value.values():
                check_values(child)
        elif isinstance(value, list):
            for child in value:
                check_values(child)
        elif isinstance(value, str):
            for match in STATIC_CONTAINER_REFERENCE_PATTERN.finditer(value):
                check_image(match.group())

    def check_steps(
        value: object,
        *,
        group: str,
        inherited_context: bool,
        direct_target: bool = False,
        condition: str = "",
    ) -> None:
        if not isinstance(value, list):
            raise ValueError(f"Workflow {path} steps must be a list.")
        steps = [_mapping(step) for step in value]
        for step in steps:
            if "uses" in step:
                check_reference(step)
            script = step.get("run")
            if isinstance(script, str) and UNTRUSTED_RUN_EXPRESSION_PATTERN.search(script):
                reject(
                    "untrusted_run_expression",
                    "Pass untrusted inputs and event values through the environment.",
                )
        checkouts = [
            (index, step)
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        for _, checkout in checkouts:
            inputs = _mapping(checkout.get("with", {}))
            ref = inputs.get("ref", "")
            checkout_context = re.sub(
                r"\[\s*'(\w+)'\s*]",
                r".\1",
                f"{ref} {inputs.get('repository', '')}",
            )
            checkout_context = re.sub(r"\s*\.\s*", ".", checkout_context).lower()
            inherited_untrusted_ref = inherited_context and (
                "github.event.pull_request" in checkout_context
                or "github.event.number" in checkout_context
                or "github.head_ref" in checkout_context
                or "refs/pull/" in checkout_context
            )
            excludes_target = any(
                _requires_conjunct(value, required)
                for value in (condition, str(checkout.get("if", "")))
                for required in (
                    "github.event_name != 'pull_request_target'",
                    "github.event_name == 'pull_request'",
                )
            )
            if (direct_target or inherited_untrusted_ref) and not excludes_target:
                trusted_ref = (
                    ref
                    in (
                        "",
                        "${{ github.sha }}",
                        "${{ github.ref }}",
                        "${{ github.event.pull_request.base.sha }}",
                        "${{ github.event.pull_request.base.ref }}",
                        "${{ github.event.repository.default_branch }}",
                    )
                    or isinstance(ref, str)
                    and re.fullmatch(r"[0-9a-f]{40}|refs/heads/[A-Za-z0-9_./-]+", ref) is not None
                )
                if (
                    not trusted_ref
                    or inputs.get("repository", "${{ github.repository }}")
                    != "${{ github.repository }}"
                ):
                    reject(
                        "pull_request_target_checkout_untrusted",
                        f"{group} must check out a trusted repository ref.",
                    )
        for index, step in enumerate(steps):
            if step.get("uses") != "./.github/actions/launchplane-request":
                continue
            if inherited_context or len(checkouts) != 1:
                reject(
                    "local_request_checkout_untrusted",
                    f"{group} requires one trusted same-repository checkout.",
                )
                continue
            checkout_index, checkout = checkouts[0]
            inputs = _mapping(checkout.get("with", {}))
            if checkout_index >= index or "ref" in inputs or "repository" in inputs:
                reject(
                    "local_request_checkout_untrusted",
                    f"{group} must check out its exact workflow revision before the local request action.",
                )

    check_values(data)
    runs = _mapping(data.get("runs", {}))
    check_steps(runs.get("steps", []), group="Composite action", inherited_context=True)
    trigger = data.get("on")
    events = set(trigger) if isinstance(trigger, (dict, list)) else {trigger}
    jobs = _mapping(data.get("jobs", {}))
    for job_id, raw_job in jobs.items():
        job = _mapping(raw_job)
        if "uses" in job:
            check_reference(job)
        check_steps(
            job.get("steps", []),
            group=f"Job {job_id}",
            inherited_context="workflow_call" in events,
            direct_target="pull_request_target" in events,
            condition=str(job.get("if", "")),
        )
        if "pull_request" in events:
            runner = str(job.get("runs-on", ""))
            if "self-hosted" in runner or "${{" in runner:
                condition = str(job.get("if", ""))
                if "always()" in condition and not _requires_conjunct(condition, "!cancelled()"):
                    reject(
                        "runner_cancellation_missing",
                        f"Job {job_id} must release its self-hosted runner on cancellation.",
                    )
                if not (
                    _requires_conjunct(condition, SAME_REPO_PULL_REQUEST_IF)
                    or _requires_conjunct(
                        condition,
                        "github.event.pull_request.head.repo.full_name == github.repository",
                    )
                    or _runner_expression_excludes_forks(runner)
                ):
                    reject(
                        "runner_fork_isolation_missing",
                        f"Job {job_id} must exclude fork pull requests before using a self-hosted runner.",
                    )
        container = job.get("container")
        if container is not None:
            check_image(container.get("image") if isinstance(container, dict) else container)
        for service in _mapping(job.get("services", {})).values():
            check_image(_mapping(service).get("image"))
    if path.parent == Path(".github/workflows") and any(
        pattern.search(text) for pattern in REAL_TOPOLOGY_PATTERNS
    ):
        reject("product_topology_embedded", "Workflow product topology belongs in runtime records.")
    return list(dict.fromkeys(violations))


def verify_workflow_pins(root: Path, revision: str) -> list[ActionPinViolation]:
    result = subprocess.run(
        (
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            revision,
            "--",
            ".github/workflows",
            ".github/actions",
        ),
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    package = subprocess.run(
        ("git", "show", f"{revision}:frontend/package.json"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    playwright_version = None
    if package.returncode == 0:
        dependencies = _mapping(_mapping(json.loads(package.stdout)).get("devDependencies", {}))
        version = dependencies.get("@playwright/test")
        if isinstance(version, str):
            playwright_version = version
    violations: list[ActionPinViolation] = []
    for name in result.stdout.splitlines():
        path = Path(name)
        if path.suffix not in {".yml", ".yaml"} or not (
            path.parent == Path(".github/workflows") or path.name in {"action.yml", "action.yaml"}
        ):
            continue
        text = subprocess.run(
            ("git", "show", f"{revision}:{name}"),
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        violations.extend(
            workflow_violations(text, path=path, playwright_version=playwright_version)
        )
    return violations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify committed workflow safety, action content and retirement worker interfaces."
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        report = build_action_pin_report(
            args.repo_root,
            target_revision=args.revision,
            repository=args.repository,
        )
        violations = verify_worker_pins(args.repo_root, report.head_sha, args.repository)
        workflow_checks = verify_workflow_pins(args.repo_root, report.head_sha)
    except (
        ValueError,
        OSError,
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        yaml.YAMLError,
        KeyError,
    ) as error:
        parser.exit(1, f"Pin verification failed: {error}\n")
    print(
        json.dumps(
            {
                "action_pins": report.as_summary_dict(),
                "worker_violations": violations,
                "workflow_violations": [item.as_dict() for item in workflow_checks],
            },
            indent=2,
        )
    )
    if report.violations or violations or workflow_checks:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
