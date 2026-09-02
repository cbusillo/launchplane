from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from control_plane.first_party_action_pins import (
    discover_action_pin_sites,
    launchplane_request_action_source,
)


YamlScalar: TypeAlias = str | int | bool | None
YamlValue: TypeAlias = YamlScalar | list["YamlValue"] | dict[str, "YamlValue"]
YamlMapping: TypeAlias = dict[str, YamlValue]

SAME_REPO_PULL_REQUEST_IF = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)
FORK_PULL_REQUEST_IF = (
    "github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name != github.repository"
)
SELF_HOSTED_RUNNER = ("self-hosted", "${{ vars.LAUNCHPLANE_RUNNER_LABEL }}")
UBUNTU_HOSTED_RUNNER = ("ubuntu-latest",)
LAUNCHPLANE_REQUEST_USES = (
    "./.github/actions/launchplane-request",
    "cbusillo/launchplane/.github/actions/launchplane-request",
)
TIMING_SNAPSHOT_ARTIFACT = "unittest-timing-snapshot"
TIMING_SNAPSHOT_PATH = "${{ runner.temp }}/unittest-timing-snapshot"
TIMING_SNAPSHOT_HISTORY = '"${RUNNER_TEMP}/unittest-timing-snapshot/history.json"'


def launchplane_request_action_reference(repo_root: Path = Path(".")) -> str:
    revisions = {
        site.revision for site in discover_action_pin_sites(repo_root) if len(site.revision) == 40
    }
    if len(revisions) != 1:
        raise AssertionError(
            "Launchplane request action consumers must share one immutable revision; "
            f"found {sorted(revisions)}"
        )
    return f"{launchplane_request_action_source(repo_root)}@{next(iter(revisions))}"


@dataclass(frozen=True)
class WorkflowInvariantViolation:
    workflow: str
    invariant: str
    message: str

    def __str__(self) -> str:
        return f"{self.workflow} [{self.invariant}]: {self.message}"


@dataclass(frozen=True)
class WorkflowStep:
    workflow: "Workflow"
    job_id: str
    index: int
    data: Mapping[str, YamlValue]

    @property
    def name(self) -> str:
        return _string_value(self.data.get("name"), default=f"step {self.index + 1}")

    @property
    def uses(self) -> str:
        return _string_value(self.data.get("uses"))

    @property
    def run(self) -> str:
        return _string_value(self.data.get("run"))

    @property
    def with_values(self) -> Mapping[str, YamlValue]:
        return _mapping_value(self.data.get("with"))


@dataclass(frozen=True)
class Workflow:
    path: Path
    data: Mapping[str, YamlValue]

    @property
    def label(self) -> str:
        return self.path.name

    @property
    def name(self) -> str:
        return _string_value(self.data.get("name"), default=self.path.stem)

    @property
    def jobs(self) -> Mapping[str, YamlValue]:
        return _mapping_value(self.data.get("jobs"))

    @property
    def permissions(self) -> Mapping[str, YamlValue]:
        return _mapping_value(self.data.get("permissions"))

    def job(self, job_id: str) -> Mapping[str, YamlValue]:
        return _mapping_value(self.jobs.get(job_id))

    def job_permissions(self, job_id: str) -> Mapping[str, YamlValue]:
        job_permissions = _mapping_value(self.job(job_id).get("permissions"))
        if job_permissions:
            return job_permissions
        return self.permissions

    def job_uses(self, job_id: str) -> str:
        return _string_value(self.job(job_id).get("uses"))

    def steps(self, job_id: str) -> tuple[WorkflowStep, ...]:
        raw_steps = _sequence_value(self.job(job_id).get("steps"))
        steps: list[WorkflowStep] = []
        for index, raw_step in enumerate(raw_steps):
            step = _mapping_value(raw_step)
            if step:
                steps.append(WorkflowStep(self, job_id, index, step))
        return tuple(steps)

    def step_named(self, job_id: str, name: str) -> WorkflowStep | None:
        for step in self.steps(job_id):
            if step.name == name:
                return step
        return None


class WorkflowInvariantChecker:
    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow
        self._violations: list[WorkflowInvariantViolation] = []

    @property
    def violations(self) -> tuple[WorkflowInvariantViolation, ...]:
        return tuple(self._violations)

    def require(self, condition: bool, invariant: str, message: str) -> None:
        if not condition:
            self._violations.append(
                WorkflowInvariantViolation(
                    workflow=self.workflow.label,
                    invariant=invariant,
                    message=message,
                )
            )


class _YamlParser:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines()
        self._index = 0

    def parse(self) -> YamlValue:
        next_line = self._peek_content()
        if next_line is None:
            return {}
        _, indent, text = next_line
        value = self._parse_node(indent)
        trailing = self._peek_content()
        if trailing is not None:
            line_number, _, trailing_text = trailing
            raise ValueError(
                f"unexpected trailing YAML content on line {line_number}: {trailing_text}"
            )
        return value

    def _parse_node(self, indent: int) -> YamlValue:
        next_line = self._peek_content()
        if next_line is None:
            return {}
        _, line_indent, text = next_line
        if line_indent < indent:
            return {}
        if text.startswith("- "):
            return self._parse_sequence(line_indent)
        return self._parse_mapping(line_indent)

    def _parse_mapping(self, indent: int) -> YamlMapping:
        mapping: YamlMapping = {}
        while True:
            next_line = self._peek_content()
            if next_line is None:
                break
            line_number, line_indent, text = next_line
            if line_indent < indent or text.startswith("- "):
                break
            if line_indent > indent:
                raise ValueError(f"unexpected nested YAML content on line {line_number}: {text}")
            key, value_text = _split_key_value(text, line_number)
            self._index = line_number
            mapping[key] = self._parse_value_after_key(indent, value_text)
        return mapping

    def _parse_sequence(self, indent: int) -> list[YamlValue]:
        values: list[YamlValue] = []
        while True:
            next_line = self._peek_content()
            if next_line is None:
                break
            line_number, line_indent, text = next_line
            if line_indent < indent or not text.startswith("- "):
                break
            if line_indent > indent:
                raise ValueError(f"unexpected nested YAML content on line {line_number}: {text}")
            item_text = text[2:].strip()
            self._index = line_number
            if not item_text:
                nested_line = self._peek_content()
                if nested_line is None or nested_line[1] <= indent:
                    values.append({})
                else:
                    values.append(self._parse_node(nested_line[1]))
                continue
            if _looks_like_mapping_item(item_text):
                values.append(self._parse_sequence_mapping_item(indent, item_text, line_number))
            else:
                values.append(_parse_scalar(item_text))
        return values

    def _parse_sequence_mapping_item(
        self,
        sequence_indent: int,
        item_text: str,
        line_number: int,
    ) -> YamlMapping:
        key, value_text = _split_key_value(item_text, line_number)
        mapping: YamlMapping = {key: self._parse_value_after_key(sequence_indent, value_text)}
        next_line = self._peek_content()
        if next_line is not None and next_line[1] > sequence_indent:
            nested = self._parse_mapping(next_line[1])
            mapping.update(nested)
        return mapping

    def _parse_value_after_key(self, indent: int, value_text: str) -> YamlValue:
        value_text = value_text.strip()
        if not value_text:
            next_line = self._peek_content()
            if next_line is None or next_line[1] <= indent:
                return {}
            return self._parse_node(next_line[1])
        if value_text in {"|", "|-", "|+", ">", ">-", ">+"}:
            return self._parse_block_scalar(indent)
        return _parse_scalar(value_text)

    def _parse_block_scalar(self, parent_indent: int) -> str:
        start = self._index
        end = start
        block_indents: list[int] = []
        for raw_index in range(start, len(self._lines)):
            raw_line = self._lines[raw_index]
            if not raw_line.strip():
                end = raw_index + 1
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent <= parent_indent:
                break
            block_indents.append(indent)
            end = raw_index + 1
        if not block_indents:
            self._index = end
            return ""
        trim_indent = min(block_indents)
        block_lines = [
            line[trim_indent:] if len(line) >= trim_indent else ""
            for line in self._lines[start:end]
        ]
        self._index = end
        return "\n".join(block_lines).rstrip("\n")

    def _peek_content(self) -> tuple[int, int, str] | None:
        for raw_index in range(self._index, len(self._lines)):
            raw_line = self._lines[raw_index]
            stripped = raw_line.strip()
            if not stripped or stripped == "---" or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            return raw_index + 1, indent, raw_line[indent:]
        return None


def load_workflow(path: str | Path) -> Workflow:
    workflow_path = Path(path)
    data = _YamlParser(workflow_path.read_text(encoding="utf-8")).parse()
    if not isinstance(data, dict):
        raise ValueError(f"{workflow_path} did not parse to a YAML mapping")
    return Workflow(path=workflow_path, data=data)


def workflow_call_inputs(workflow: Workflow) -> set[str]:
    workflow_on = _mapping_value(workflow.data.get("on"))
    workflow_call = _mapping_value(workflow_on.get("workflow_call"))
    return set(_mapping_value(workflow_call.get("inputs")))


def launchplane_request_steps(workflow: Workflow) -> tuple[WorkflowStep, ...]:
    request_steps: list[WorkflowStep] = []
    for job_id in workflow.jobs:
        for step in workflow.steps(job_id):
            if any(step.uses.startswith(prefix) for prefix in LAUNCHPLANE_REQUEST_USES):
                request_steps.append(step)
    return tuple(request_steps)


def workflow_scalar_values(value: YamlValue, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            yield from workflow_scalar_values(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from workflow_scalar_values(child, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value
    elif value is not None:
        yield path, str(value)


def check_fork_runner_isolation(
    workflow: Workflow,
    *,
    same_repo_jobs: Sequence[str],
    fork_jobs: Sequence[str],
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "fork-runner-isolation"
    for job_id in same_repo_jobs:
        job = workflow.job(job_id)
        checker.require(bool(job), invariant, f"missing same-repository job {job_id!r}")
        if not job:
            continue
        checker.require(
            _if_contains(job, SAME_REPO_PULL_REQUEST_IF),
            invariant,
            f"job {job_id!r} must be gated to same-repository pull requests",
        )
        runs_on = _runner_labels(job.get("runs-on"))
        checker.require(
            tuple(runs_on) == SELF_HOSTED_RUNNER,
            invariant,
            f"job {job_id!r} must use the self-hosted Launchplane runner lane",
        )
    for job_id in fork_jobs:
        job = workflow.job(job_id)
        checker.require(bool(job), invariant, f"missing fork job {job_id!r}")
        if not job:
            continue
        checker.require(
            _if_contains(job, FORK_PULL_REQUEST_IF),
            invariant,
            f"job {job_id!r} must be gated to fork pull requests",
        )
        runs_on = _runner_labels(job.get("runs-on"))
        checker.require(
            tuple(runs_on) == UBUNTU_HOSTED_RUNNER,
            invariant,
            f"job {job_id!r} must use GitHub-hosted ubuntu-latest",
        )
    return checker.violations


def check_required_permissions(
    workflow: Workflow,
    *,
    job_ids: Sequence[str],
    permissions: Mapping[str, str],
    invariant: str,
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    for job_id in job_ids:
        actual_permissions = workflow.job_permissions(job_id)
        for permission, expected in permissions.items():
            checker.require(
                _string_value(actual_permissions.get(permission)) == expected,
                invariant,
                f"job {job_id!r} must grant {permission}: {expected}",
            )
    return checker.violations


def check_launchplane_oidc_permissions(
    workflow: Workflow,
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "launchplane-request-oidc-permissions"
    for step in launchplane_request_steps(workflow):
        permissions = workflow.job_permissions(step.job_id)
        checker.require(
            _string_value(permissions.get("contents")) == "read",
            invariant,
            f"job {step.job_id!r} step {step.name!r} must grant contents: read",
        )
        checker.require(
            _string_value(permissions.get("id-token")) == "write",
            invariant,
            f"job {step.job_id!r} step {step.name!r} must grant id-token: write",
        )
    return checker.violations


def check_forbidden_scalar_values(
    workflow: Workflow,
    *,
    forbidden_values: Sequence[str],
    invariant: str,
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    for scalar_path, scalar in workflow_scalar_values(cast("YamlValue", workflow.data)):
        for forbidden_value in forbidden_values:
            checker.require(
                forbidden_value not in scalar,
                invariant,
                f"forbidden value {forbidden_value!r} appears at {scalar_path}",
            )
    return checker.violations


def check_security_policy_runs_for_all_pull_requests(
    workflow: Workflow,
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "action-pinning-policy-gate"
    for job_id in ("workflow_lint", "workflow_lint_fork"):
        step = workflow.step_named(job_id, "Enforce immutable GitHub Action references")
        checker.require(step is not None, invariant, f"job {job_id!r} must run action pinning")
        if step is None:
            continue
        checker.require(
            "tests.test_github_actions_security" in step.run,
            invariant,
            f"job {job_id!r} must execute tests.test_github_actions_security",
        )
    return checker.violations


def check_frontend_browser_smoke(
    workflow: Workflow,
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "frontend-browser-smoke"
    job_id = "frontend_browser_smoke"
    job = workflow.job(job_id)
    checker.require(bool(job), invariant, f"missing {job_id} job")
    checker.require(
        tuple(_runner_labels(job.get("runs-on"))) == UBUNTU_HOSTED_RUNNER,
        invariant,
        f"{job_id} must run on ubuntu-latest",
    )
    checker.require(
        not _string_value(job.get("if")),
        invariant,
        f"{job_id} must run for same-repository and fork pull requests",
    )
    install_step = workflow.step_named(job_id, "Install Chromium")
    checker.require(install_step is not None, invariant, "missing Install Chromium step")
    if install_step is not None:
        checker.require(
            "playwright install --with-deps chromium" in install_step.run,
            invariant,
            "Chromium install must include hosted-runner dependencies",
        )
    run_step = workflow.step_named(job_id, "Run browser smoke")
    checker.require(run_step is not None, invariant, "missing Run browser smoke step")
    if run_step is not None:
        checker.require(
            run_step.run == "pnpm --dir frontend test:browser",
            invariant,
            "browser smoke command drifted",
        )
    upload_step = workflow.step_named(job_id, "Upload browser evidence")
    checker.require(upload_step is not None, invariant, "missing browser evidence upload")
    if upload_step is not None:
        checker.require(
            upload_step.uses.startswith("actions/upload-artifact@"),
            invariant,
            "browser evidence must use upload-artifact",
        )
        checker.require(
            _normalize_expression(_string_value(upload_step.data.get("if"))) == "always()",
            invariant,
            "browser evidence must upload after failures",
        )
        _require_step_with(
            checker,
            upload_step,
            invariant,
            {
                "path": "tmp/browser-smoke",
                "if-no-files-found": "error",
                "retention-days": "14",
            },
        )
    return checker.violations


def check_ci_aggregate_gate(workflow: Workflow) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "aggregate-ci-gate"
    expected_needs = {
        "static_checks",
        "static_checks_fork",
        "container_scan",
        "container_scan_fork",
        "frontend_validate",
        "frontend_validate_fork",
        "frontend_browser_smoke",
        "test",
        "test_fork",
        "postgres_integration",
    }
    expected_env = {
        "BASE_REPOSITORY",
        "HEAD_REPOSITORY",
        "STATIC_CHECKS_RESULT",
        "STATIC_CHECKS_FORK_RESULT",
        "CONTAINER_SCAN_RESULT",
        "CONTAINER_SCAN_FORK_RESULT",
        "FRONTEND_VALIDATE_RESULT",
        "FRONTEND_VALIDATE_FORK_RESULT",
        "FRONTEND_BROWSER_SMOKE_RESULT",
        "TEST_RESULT",
        "TEST_FORK_RESULT",
        "POSTGRES_INTEGRATION_RESULT",
    }
    gate = workflow.job("ci_gate")
    checker.require(bool(gate), invariant, "missing ci_gate job")
    checker.require(_string_value(gate.get("name")) == "ci-gate", invariant, "ci_gate name drifted")
    checker.require(
        tuple(_runner_labels(gate.get("runs-on"))) == UBUNTU_HOSTED_RUNNER,
        invariant,
        "ci_gate must run on ubuntu-latest",
    )
    checker.require(
        _normalize_expression(_string_value(gate.get("if")))
        == _normalize_expression(
            "always() && (github.event_name == 'pull_request' || github.event_name == 'push')"
        ),
        invariant,
        "ci_gate must always evaluate pull-request and merge-train push results",
    )
    checker.require(
        _string_set(gate.get("needs")) == expected_needs, invariant, "ci_gate needs drifted"
    )
    step = workflow.step_named("ci_gate", "Require successful CI path")
    checker.require(step is not None, invariant, "ci_gate must require successful CI path")
    if step is not None:
        checker.require(
            set(_mapping_value(step.data.get("env"))) == expected_env,
            invariant,
            "ci_gate env result mirror drifted",
        )
        for required in (
            "HEAD_REPOSITORY",
            "BASE_REPOSITORY",
            "require_success static_checks",
            "require_success container_scan",
            "require_success frontend_validate",
            "require_success test",
            "require_success postgres_integration",
            "require_success static_checks_fork",
            "require_success container_scan_fork",
            "require_success frontend_validate_fork",
            "require_success test_fork",
        ):
            checker.require(required in step.run, invariant, f"ci_gate script missing {required!r}")
        browser_gate = 'require_success frontend_browser_smoke "${FRONTEND_BROWSER_SMOKE_RESULT}"'
        branch_marker = 'if [ "${HEAD_REPOSITORY}" = "${BASE_REPOSITORY}" ]; then'
        _, branch_separator, branch_tail = step.run.partition(branch_marker)
        same_repo_block, else_separator, fork_tail = branch_tail.partition("\nelse\n")
        fork_block, fi_separator, _ = fork_tail.partition("\nfi")
        checker.require(
            bool(branch_separator and else_separator and fi_separator),
            invariant,
            "ci_gate same-repository and fork branch structure drifted",
        )
        checker.require(
            browser_gate in same_repo_block,
            invariant,
            "ci_gate same-repository path must require browser smoke",
        )
        checker.require(
            browser_gate in fork_block,
            invariant,
            "ci_gate fork path must require browser smoke",
        )
    return checker.violations


def check_security_aggregate_gate(workflow: Workflow) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "aggregate-security-gate"
    expected_needs = {"workflow_lint", "workflow_lint_fork", "secret_scan", "secret_scan_fork"}
    expected_env = {
        "BASE_REPOSITORY",
        "HEAD_REPOSITORY",
        "WORKFLOW_LINT_RESULT",
        "WORKFLOW_LINT_FORK_RESULT",
        "SECRET_SCAN_RESULT",
        "SECRET_SCAN_FORK_RESULT",
    }
    gate = workflow.job("security_gate")
    checker.require(bool(gate), invariant, "missing security_gate job")
    checker.require(
        _string_value(gate.get("name")) == "security-gate",
        invariant,
        "security_gate name drifted",
    )
    checker.require(
        tuple(_runner_labels(gate.get("runs-on"))) == UBUNTU_HOSTED_RUNNER,
        invariant,
        "security_gate must run on ubuntu-latest",
    )
    checker.require(
        _normalize_expression(_string_value(gate.get("if")))
        == _normalize_expression(
            "always() && (github.event_name == 'pull_request' || github.event_name == 'push')"
        ),
        invariant,
        "security_gate must always evaluate pull-request and merge-train push results",
    )
    checker.require(
        _string_set(gate.get("needs")) == expected_needs,
        invariant,
        "security_gate needs drifted",
    )
    step = workflow.step_named("security_gate", "Require successful security path")
    checker.require(
        step is not None, invariant, "security_gate must require successful security path"
    )
    if step is not None:
        checker.require(
            set(_mapping_value(step.data.get("env"))) == expected_env,
            invariant,
            "security_gate env result mirror drifted",
        )
        for required in (
            "HEAD_REPOSITORY",
            "BASE_REPOSITORY",
            "require_success workflow_lint",
            "require_success secret_scan",
            "require_success workflow_lint_fork",
            "require_success secret_scan_fork",
        ):
            checker.require(
                required in step.run, invariant, f"security_gate script missing {required!r}"
            )
    return checker.violations


def check_unittest_timing_snapshot(workflow: Workflow) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    invariant = "immutable-unittest-timing-snapshot"
    snapshot_job = workflow.job("test_timing_snapshot")
    checker.require(bool(snapshot_job), invariant, "missing test_timing_snapshot job")
    checker.require(
        workflow.step_named("test_timing_snapshot", "Restore unittest timings") is not None,
        invariant,
        "test_timing_snapshot must restore timing cache before freezing it",
    )
    freeze_step = workflow.step_named("test_timing_snapshot", "Freeze unittest timing snapshot")
    checker.require(
        freeze_step is not None, invariant, "missing Freeze unittest timing snapshot step"
    )
    if freeze_step is not None:
        checker.require(
            "${GITHUB_RUN_ID}" in freeze_step.run and "${GITHUB_RUN_ATTEMPT}" in freeze_step.run,
            invariant,
            "snapshot id must include run id and attempt",
        )
    upload_snapshot = workflow.step_named("test_timing_snapshot", "Upload unittest timing snapshot")
    checker.require(
        upload_snapshot is not None, invariant, "missing Upload unittest timing snapshot step"
    )
    if upload_snapshot is not None:
        _require_step_with(
            checker,
            upload_snapshot,
            invariant,
            {
                "name": TIMING_SNAPSHOT_ARTIFACT,
                "path": TIMING_SNAPSHOT_PATH,
                "if-no-files-found": "error",
                "overwrite": "true",
                "retention-days": "30",
            },
        )
    shards_job = workflow.job("test_shards")
    expected_unittest_env = {
        "UNITTEST_SHARD_COUNT": "12",
        "UNITTEST_MAX_TESTS_PER_TARGET": "20",
        "UNITTEST_MAX_SECONDS_PER_TARGET": "30",
    }
    for job_id in ("test_shards", "test"):
        job_env = _mapping_value(workflow.job(job_id).get("env"))
        for key, expected_value in expected_unittest_env.items():
            checker.require(
                _string_value(job_env.get(key)) == expected_value,
                invariant,
                f"job {job_id!r} must set {key}: {expected_value}",
            )
    matrix = _mapping_value(_mapping_value(shards_job.get("strategy")).get("matrix"))
    checker.require(
        tuple(_string_value(item) for item in _sequence_value(matrix.get("shard_index")))
        == tuple(str(index) for index in range(12)),
        invariant,
        "test_shards matrix must cover shard indexes 0 through 11",
    )
    checker.require(
        _string_set(shards_job.get("needs")) == {"test_timing_snapshot"},
        invariant,
        "test_shards must depend only on the frozen timing snapshot",
    )
    for step_name in (
        "Download unittest timing snapshot",
        "Show unit test shard plan",
        "Run unit test shard",
    ):
        step = workflow.step_named("test_shards", step_name)
        checker.require(step is not None, invariant, f"test_shards missing {step_name!r}")
        if step is not None and step_name == "Download unittest timing snapshot":
            _require_step_with(
                checker,
                step,
                invariant,
                {"name": TIMING_SNAPSHOT_ARTIFACT, "path": TIMING_SNAPSHOT_PATH},
            )
        if step is not None and step_name != "Download unittest timing snapshot":
            checker.require(
                TIMING_SNAPSHOT_HISTORY in step.run,
                invariant,
                f"{step_name!r} must read the frozen timing snapshot",
            )
    upload_shard = workflow.step_named("test_shards", "Upload unittest timings")
    checker.require(upload_shard is not None, invariant, "missing Upload unittest timings step")
    if upload_shard is not None:
        _require_step_with(
            checker,
            upload_shard,
            invariant,
            {"overwrite": "true", "retention-days": "30", "if-no-files-found": "error"},
        )
    aggregate_job = workflow.job("test")
    checker.require(
        _string_set(aggregate_job.get("needs")) == {"test_timing_snapshot", "test_shards"},
        invariant,
        "test aggregate job must wait for snapshot and shards",
    )
    aggregate_step = workflow.step_named("test", "Aggregate unittest timings")
    checker.require(
        aggregate_step is not None, invariant, "missing Aggregate unittest timings step"
    )
    if aggregate_step is not None:
        checker.require(
            TIMING_SNAPSHOT_HISTORY in aggregate_step.run,
            invariant,
            "aggregate must read the same frozen timing snapshot",
        )
    save_step = workflow.step_named("test", "Save unittest timings")
    checker.require(save_step is not None, invariant, "missing Save unittest timings step")
    if save_step is not None:
        checker.require(
            _normalize_expression(_string_value(save_step.data.get("if")))
            == _normalize_expression(
                "github.event_name == 'push' && github.ref == 'refs/heads/main'"
            ),
            invariant,
            "timing cache save must remain limited to pushes to main",
        )
    return checker.violations


def check_launchplane_request_contract(
    workflow: Workflow,
    *,
    expected_steps: Mapping[str, Mapping[str, str]],
    invariant: str,
) -> tuple[WorkflowInvariantViolation, ...]:
    checker = WorkflowInvariantChecker(workflow)
    observed_steps = {step.name: step for step in launchplane_request_steps(workflow)}
    for step_name, expected_with in expected_steps.items():
        step = observed_steps.get(step_name)
        checker.require(step is not None, invariant, f"missing request step {step_name!r}")
        if step is None:
            continue
        for key, expected_value in expected_with.items():
            checker.require(
                _string_value(step.with_values.get(key)) == expected_value,
                invariant,
                f"step {step_name!r} must set {key}: {expected_value}",
            )
    extra_steps = set(observed_steps) - set(expected_steps)
    checker.require(
        not extra_steps,
        invariant,
        f"unexpected request steps: {sorted(extra_steps)}",
    )
    return checker.violations


def _require_step_with(
    checker: WorkflowInvariantChecker,
    step: WorkflowStep,
    invariant: str,
    expected_values: Mapping[str, str],
) -> None:
    for key, expected_value in expected_values.items():
        checker.require(
            _string_value(step.with_values.get(key)) == expected_value,
            invariant,
            f"step {step.name!r} must set {key}: {expected_value}",
        )


def _mapping_value(value: YamlValue | None) -> Mapping[str, YamlValue]:
    if isinstance(value, dict):
        return value
    return {}


def _sequence_value(value: YamlValue | None) -> Sequence[YamlValue]:
    if isinstance(value, list):
        return value
    return ()


def _string_value(value: YamlValue | None, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return default


def _string_set(value: YamlValue | None) -> set[str]:
    if isinstance(value, list):
        return {_string_value(item) for item in value}
    scalar = _string_value(value)
    if scalar:
        return {scalar}
    return set()


def _runner_labels(value: YamlValue | None) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(_string_value(item) for item in value)
    scalar = _string_value(value)
    if scalar:
        return (scalar,)
    return ()


def _if_contains(job: Mapping[str, YamlValue], expected_expression: str) -> bool:
    return _normalize_expression(expected_expression) in _normalize_expression(
        _string_value(job.get("if"))
    )


def _normalize_expression(value: str) -> str:
    return " ".join(value.split())


def _split_key_value(text: str, line_number: int) -> tuple[str, str]:
    quote: str | None = None
    for index, character in enumerate(text):
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        if character == ":" and quote is None:
            key = _parse_key(text[:index].strip())
            return key, text[index + 1 :]
    raise ValueError(f"expected YAML key/value pair on line {line_number}: {text}")


def _parse_key(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _looks_like_mapping_item(value: str) -> bool:
    try:
        _split_key_value(value, 0)
    except ValueError:
        return False
    return True


def _parse_scalar(value: str) -> YamlScalar | list[YamlValue]:
    value = _strip_inline_comment(value).strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline_list(inner)]
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {"null", "~"}:
        return None
    if value.isdecimal():
        return int(value)
    return value


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        if character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _split_inline_list(value: str) -> list[str]:
    parts: list[str] = []
    quote: str | None = None
    start = 0
    for index, character in enumerate(value):
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        if character == "," and quote is None:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts
