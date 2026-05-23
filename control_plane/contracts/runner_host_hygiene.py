from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerHostHygieneStatus = Literal["healthy", "attention"]
RunnerHostHygieneApplyAction = Literal[
    "prune_docker_cache",
    "prune_runner_workdir",
    "restart_runner_service",
]
RunnerHostHygieneApplyPlanStatus = Literal["ready", "blocked"]
RunnerHostHygieneApplyAuditStatus = Literal["planned", "completed", "failed"]
RunnerHostHygieneApplyBlockerCode = Literal[
    "action_not_enabled",
    "approved_host_mismatch",
    "audit_record_required",
    "host_needs_attention",
    "mutate_not_requested",
    "warm_builder_not_retained",
]
RunnerHostHygieneFindingCode = Literal[
    "docker_reclaimable_above_limit",
    "free_disk_below_minimum",
    "orphan_buildkit_present",
    "required_warm_builder_missing",
    "runner_workdir_above_limit",
]


class RunnerHostHygienePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    maximum_docker_reclaimable_bytes: int | None = Field(default=None, ge=0)
    maximum_runner_workdir_bytes: int | None = Field(default=None, ge=0)
    required_warm_builders: tuple[str, ...] = ()
    allow_orphan_buildkit: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygienePolicy":
        self.required_warm_builders = _normalized_tokens(self.required_warm_builders)
        return self


class RunnerHostHygieneObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_name: str
    observed_at: str
    free_disk_bytes: int = Field(ge=0)
    docker_reclaimable_bytes: int = Field(default=0, ge=0)
    runner_workdir_bytes: int = Field(default=0, ge=0)
    warm_builders: tuple[str, ...] = ()
    orphan_buildkit_containers: int = Field(default=0, ge=0)
    orphan_buildkit_volumes: int = Field(default=0, ge=0)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_observation(self) -> "RunnerHostHygieneObservation":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene observation requires host_name"
        )
        self.observed_at = _required_text(
            self.observed_at, "runner host hygiene observation requires observed_at"
        )
        self.warm_builders = _normalized_tokens(self.warm_builders)
        self.notes = tuple(note.strip() for note in self.notes if note.strip())
        return self


class RunnerHostHygieneFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerHostHygieneFindingCode
    message: str

    @model_validator(mode="after")
    def _normalize_finding(self) -> "RunnerHostHygieneFinding":
        self.message = _required_text(self.message, "runner host hygiene finding requires message")
        return self


class RunnerHostHygieneReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerHostHygieneStatus
    host_name: str
    findings: tuple[RunnerHostHygieneFinding, ...] = ()
    summary: str
    next_steps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_report(self) -> "RunnerHostHygieneReport":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene report requires host_name"
        )
        self.findings = tuple(sorted(self.findings, key=lambda finding: finding.code))
        self.summary = _required_text(self.summary, "runner host hygiene report requires summary")
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        if self.status == "healthy" and self.findings:
            raise ValueError("healthy runner host hygiene report cannot include findings")
        if self.status == "attention" and not self.findings:
            raise ValueError("attention runner host hygiene report requires findings")
        return self


class RunnerHostHygieneApplyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    approved_hosts: tuple[str, ...] = ()
    required_retained_warm_builders: tuple[str, ...] = ()
    require_healthy_report: bool = True
    require_audit_record: bool = True
    allow_docker_cache_prune: bool = False
    allow_runner_workdir_prune: bool = False
    allow_runner_service_restart: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygieneApplyPolicy":
        self.approved_hosts = _normalized_host_names(self.approved_hosts)
        self.required_retained_warm_builders = _normalized_tokens(
            self.required_retained_warm_builders
        )
        return self


class RunnerHostHygieneApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: RunnerHostHygieneApplyAction
    host_name: str
    mutate: bool = False
    retained_warm_builders: tuple[str, ...] = ()
    audit_record_key: str = ""

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerHostHygieneApplyRequest":
        self.host_name = _normalized_host_name(self.host_name)
        if not self.host_name:
            raise ValueError("runner host hygiene apply requires host_name")
        self.retained_warm_builders = _normalized_tokens(self.retained_warm_builders)
        self.audit_record_key = self.audit_record_key.strip()
        return self


class RunnerHostHygieneApplyBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerHostHygieneApplyBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "RunnerHostHygieneApplyBlocker":
        self.message = _required_text(
            self.message, "runner host hygiene apply blocker requires message"
        )
        return self


class RunnerHostHygieneApplyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerHostHygieneApplyPlanStatus
    action: RunnerHostHygieneApplyAction
    host_name: str
    mutate: bool
    audit_record_key: str = ""
    blockers: tuple[RunnerHostHygieneApplyBlocker, ...] = ()
    next_steps: tuple[str, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "RunnerHostHygieneApplyPlan":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene apply plan requires host_name"
        )
        self.audit_record_key = self.audit_record_key.strip()
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        self.summary = _required_text(
            self.summary, "runner host hygiene apply plan requires summary"
        )
        if self.status == "ready" and self.blockers:
            raise ValueError("ready runner host hygiene apply plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked runner host hygiene apply plan requires blockers")
        return self


class RunnerHostHygieneApplyAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    audit_record_key: str
    status: RunnerHostHygieneApplyAuditStatus
    request: RunnerHostHygieneApplyRequest
    plan: RunnerHostHygieneApplyPlan
    pre_apply_report: RunnerHostHygieneReport
    post_apply_report: RunnerHostHygieneReport | None = None
    message: str = ""

    @model_validator(mode="after")
    def _normalize_record(self) -> "RunnerHostHygieneApplyAuditRecord":
        self.audit_record_key = _required_text(
            self.audit_record_key, "runner host hygiene apply audit record requires key"
        )
        self.message = self.message.strip()
        if self.audit_record_key != self.request.audit_record_key:
            raise ValueError("runner host hygiene audit record key must match request")
        if self.status == "planned" and self.post_apply_report is not None:
            raise ValueError("planned runner host hygiene audit record cannot include post report")
        return self


def evaluate_runner_host_hygiene(
    *,
    policy: RunnerHostHygienePolicy,
    observation: RunnerHostHygieneObservation,
) -> RunnerHostHygieneReport:
    findings: list[RunnerHostHygieneFinding] = []
    if observation.free_disk_bytes < policy.minimum_free_disk_bytes:
        findings.append(
            _finding(
                "free_disk_below_minimum",
                (
                    "runner host free disk is below the configured minimum: "
                    f"{observation.free_disk_bytes} < {policy.minimum_free_disk_bytes}"
                ),
            )
        )
    if (
        policy.maximum_docker_reclaimable_bytes is not None
        and observation.docker_reclaimable_bytes > policy.maximum_docker_reclaimable_bytes
    ):
        findings.append(
            _finding(
                "docker_reclaimable_above_limit",
                (
                    "runner host Docker reclaimable bytes exceed the configured limit: "
                    f"{observation.docker_reclaimable_bytes} > "
                    f"{policy.maximum_docker_reclaimable_bytes}"
                ),
            )
        )
    if (
        policy.maximum_runner_workdir_bytes is not None
        and observation.runner_workdir_bytes > policy.maximum_runner_workdir_bytes
    ):
        findings.append(
            _finding(
                "runner_workdir_above_limit",
                (
                    "runner host work directory bytes exceed the configured limit: "
                    f"{observation.runner_workdir_bytes} > "
                    f"{policy.maximum_runner_workdir_bytes}"
                ),
            )
        )
    missing_builders = tuple(
        builder
        for builder in policy.required_warm_builders
        if builder not in observation.warm_builders
    )
    if missing_builders:
        findings.append(
            _finding(
                "required_warm_builder_missing",
                "runner host is missing required warm builders: " + ", ".join(missing_builders),
            )
        )
    if not policy.allow_orphan_buildkit and (
        observation.orphan_buildkit_containers > 0 or observation.orphan_buildkit_volumes > 0
    ):
        findings.append(
            _finding(
                "orphan_buildkit_present",
                (
                    "runner host has orphan BuildKit artifacts: "
                    f"{observation.orphan_buildkit_containers} containers, "
                    f"{observation.orphan_buildkit_volumes} volumes"
                ),
            )
        )

    status: RunnerHostHygieneStatus = "attention" if findings else "healthy"
    return RunnerHostHygieneReport(
        status=status,
        host_name=observation.host_name,
        findings=tuple(findings),
        summary=(
            "runner host hygiene satisfies report-only policy"
            if status == "healthy"
            else "runner host hygiene needs operator attention"
        ),
        next_steps=_next_steps(status),
    )


def plan_runner_host_hygiene_apply(
    *,
    policy: RunnerHostHygieneApplyPolicy,
    request: RunnerHostHygieneApplyRequest,
    report: RunnerHostHygieneReport,
) -> RunnerHostHygieneApplyPlan:
    blockers: list[RunnerHostHygieneApplyBlocker] = []
    if not policy.approved_hosts or request.host_name not in policy.approved_hosts:
        blockers.append(
            _apply_blocker(
                "approved_host_mismatch",
                f"runner host is not approved for hygiene apply: {request.host_name}",
            )
        )
    if not _apply_action_allowed(policy=policy, action=request.action):
        blockers.append(
            _apply_blocker(
                "action_not_enabled",
                f"runner host hygiene apply action is not enabled: {request.action}",
            )
        )
    if not request.mutate:
        blockers.append(
            _apply_blocker(
                "mutate_not_requested",
                "runner host hygiene apply is dry-run only until mutate is explicitly requested",
            )
        )
    if policy.require_audit_record and not request.audit_record_key:
        blockers.append(
            _apply_blocker(
                "audit_record_required",
                "runner host hygiene apply requires an audit record key",
            )
        )
    if policy.require_healthy_report and report.status != "healthy":
        blockers.append(
            _apply_blocker(
                "host_needs_attention",
                f"runner host hygiene report is not healthy: {report.summary}",
            )
        )
    missing_retained_builders = tuple(
        builder
        for builder in policy.required_retained_warm_builders
        if builder not in request.retained_warm_builders
    )
    if missing_retained_builders:
        blockers.append(
            _apply_blocker(
                "warm_builder_not_retained",
                (
                    "runner host hygiene apply request does not retain required warm builders: "
                    + ", ".join(missing_retained_builders)
                ),
            )
        )

    status: RunnerHostHygieneApplyPlanStatus = "blocked" if blockers else "ready"
    return RunnerHostHygieneApplyPlan(
        status=status,
        action=request.action,
        host_name=request.host_name,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
        blockers=tuple(blockers),
        next_steps=_apply_next_steps(action=request.action, status=status),
        summary=(
            "runner host hygiene apply plan is ready"
            if status == "ready"
            else "runner host hygiene apply plan is blocked"
        ),
    )


def _apply_action_allowed(
    *, policy: RunnerHostHygieneApplyPolicy, action: RunnerHostHygieneApplyAction
) -> bool:
    if action == "prune_docker_cache":
        return policy.allow_docker_cache_prune
    if action == "prune_runner_workdir":
        return policy.allow_runner_workdir_prune
    return policy.allow_runner_service_restart


def _apply_next_steps(
    *, action: RunnerHostHygieneApplyAction, status: RunnerHostHygieneApplyPlanStatus
) -> tuple[str, ...]:
    if status == "blocked":
        return ("resolve blockers before invoking any host adapter",)
    if action == "restart_runner_service":
        return (
            "capture pre-apply host hygiene evidence",
            "restart only the approved runner service through the host adapter",
            "capture post-apply host hygiene evidence and write the audit record",
        )
    if action == "prune_runner_workdir":
        return (
            "capture pre-apply host hygiene evidence",
            "prune only approved runner work directories through the host adapter",
            "capture post-apply host hygiene evidence and write the audit record",
        )
    return (
        "capture pre-apply host hygiene evidence",
        "prune Docker cache without removing retained warm builders",
        "capture post-apply host hygiene evidence and write the audit record",
    )


def _next_steps(status: RunnerHostHygieneStatus) -> tuple[str, ...]:
    if status == "healthy":
        return ("keep collecting read-only host hygiene evidence before enabling apply",)
    return (
        "review findings before scheduling any host cleanup",
        "use a Launchplane-owned apply workflow before mutating runner hosts",
    )


def _finding(code: RunnerHostHygieneFindingCode, message: str) -> RunnerHostHygieneFinding:
    return RunnerHostHygieneFinding(code=code, message=message)


def _apply_blocker(
    code: RunnerHostHygieneApplyBlockerCode, message: str
) -> RunnerHostHygieneApplyBlocker:
    return RunnerHostHygieneApplyBlocker(code=code, message=message)


def _normalized_host_names(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_host_name(value))})
    )


def _normalized_host_name(value: str) -> str:
    return value.strip().lower()


def _normalized_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_token(value))})
    )


def _normalized_token(value: str) -> str:
    return value.strip().lower()


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
