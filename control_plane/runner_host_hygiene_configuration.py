from typing import Literal
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.runtime_environments import RuntimeEnvironmentRecordStore
from control_plane.workflows.runner_host_hygiene_executor import (
    GeneratedRunCacheRoot,
    RunnerHostGitHubBinding,
    RunnerHostHygieneExecutorRequest,
    RunnerWorkdirRoot,
)

SETTINGS_KEY = "LAUNCHPLANE_RUNNER_HOST_HYGIENE_CONFIGURATION"
PRIVATE_KEY_BINDING = "LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_PRIVATE_KEY"
SERVICE_CONTEXT = "launchplane"


class RunnerHostHygieneConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_name: str
    execution_lane: str
    service_user: str
    repository_scope: str
    retained_warm_builders: tuple[str, ...]
    github_idle_bindings: tuple[RunnerHostGitHubBinding, ...] = Field(min_length=1, max_length=256)
    runner_workdir_roots: tuple[RunnerWorkdirRoot, ...] = Field(min_length=1, max_length=128)
    generated_run_cache_roots: tuple[GeneratedRunCacheRoot, ...] = Field(max_length=128)
    buildkit_cache_budgets: dict[str, int]
    default_cache_budget_bytes: int
    allowed_buildkit_state_volumes: tuple[str, ...] = ()
    github_app_client_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_executor_configuration(self) -> "RunnerHostHygieneConfiguration":
        if "default" in self.buildkit_cache_budgets:
            raise ValueError("The default cache budget has its own setting.")
        if not self.buildkit_cache_budgets:
            raise ValueError("Existing named builder budgets are required.")
        request = self.executor_request(audit_record_key="configuration-validation")
        self.host_name = request.host_name
        self.execution_lane = request.execution_lane
        self.service_user = request.service_user
        self.repository_scope = request.repository_scope
        self.retained_warm_builders = request.retained_warm_builders
        self.github_idle_bindings = request.github_idle_bindings
        self.runner_workdir_roots = request.runner_workdir_roots
        self.generated_run_cache_roots = request.generated_run_cache_roots
        if not any(
            binding.repository_scope == request.repository_scope.lower()
            and binding.execution_lane == request.execution_lane.lower()
            for binding in request.github_idle_bindings
        ):
            raise ValueError("Configuration must include its scheduling repository and lane.")
        repositories = {binding.repository_scope for binding in request.github_idle_bindings} | {
            root.source_repository for root in request.generated_run_cache_roots
        }
        if any(
            repository.split("/", 1)[0] != request.repository_scope.lower().split("/", 1)[0]
            for repository in repositories
        ):
            raise ValueError("Evidence repositories must share one installation owner.")
        return self

    def executor_request(
        self, *, audit_record_key: str, **intent: object
    ) -> RunnerHostHygieneExecutorRequest:
        return RunnerHostHygieneExecutorRequest.model_validate(
            {
                "host_name": self.host_name,
                "execution_lane": self.execution_lane,
                "service_user": self.service_user,
                "repository_scope": self.repository_scope,
                "retained_warm_builders": self.retained_warm_builders,
                "github_idle_bindings": self.github_idle_bindings,
                "runner_workdir_roots": self.runner_workdir_roots,
                "generated_run_cache_roots": self.generated_run_cache_roots,
                "buildkit_cache_budgets": {
                    **self.buildkit_cache_budgets,
                    "default": self.default_cache_budget_bytes,
                },
                "action": "prune_docker_cache",
                "max_used_space_bytes": self.default_cache_budget_bytes,
                **intent,
                "audit_record_key": audit_record_key,
            }
        )


class RunnerHostHygieneDispatchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["report", "maintenance", "manual"] = "report"
    audit_record_key: str = Field(default="", max_length=512)
    current_runner_name: str = Field(min_length=1, max_length=255)
    action: Literal[
        "prune_docker_cache",
        "prune_dangling_images",
        "prune_generated_run_cache",
        "remove_buildkit_state_volumes",
    ] = "prune_docker_cache"
    mutate: bool = False
    target_buildkit_builder: str = ""
    target_generated_cache_key: str = ""
    target_buildkit_state_volumes: tuple[str, ...] = ()
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    prune_until: str = "168h"
    timeout_seconds: int = Field(default=300, ge=1, le=900)
    resolve_action_started: bool = False

    @model_validator(mode="after")
    def validate_scheduled_intent(self) -> "RunnerHostHygieneDispatchIntent":
        if self.mode == "manual" and not self.audit_record_key.strip():
            raise ValueError("Manual dispatch requires an audit record key.")
        if self.mode != "manual" and self.audit_record_key:
            raise ValueError("Scheduled audit keys are derived from the authenticated run.")
        if self.mode != "manual" and any(
            getattr(self, key) != field.default
            for key, field in type(self).model_fields.items()
            if key not in {"mode", "audit_record_key", "current_runner_name"}
        ):
            raise ValueError("Scheduled dispatch does not accept manual overrides.")
        return self


class RunnerHostHygieneDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_sha256: str
    execution_lane: str
    requests: tuple[RunnerHostHygieneExecutorRequest, ...]


def load_runner_host_hygiene_configuration(
    store: RuntimeEnvironmentRecordStore,
) -> tuple[RunnerHostHygieneConfiguration, str]:
    records = tuple(
        record
        for record in store.list_runtime_environment_records(context_name=SERVICE_CONTEXT)
        if record.scope == "context"
        and record.context == SERVICE_CONTEXT
        and not record.instance
        and SETTINGS_KEY in record.env
    )
    if len(records) != 1:
        raise ValueError("An exact service hygiene configuration record is required.")
    value = records[0].env[SETTINGS_KEY]
    if not isinstance(value, str):
        raise ValueError("The service hygiene configuration must be JSON.")
    configuration = RunnerHostHygieneConfiguration.model_validate_json(value)
    digest = nonvalidating_compact_sorted_json_sha256(configuration.model_dump(mode="json"))
    return configuration, digest


def build_runner_host_hygiene_dispatch(
    *,
    configuration: RunnerHostHygieneConfiguration,
    configuration_sha256: str,
    intent: RunnerHostHygieneDispatchIntent,
    current_run_id: int | None = None,
) -> RunnerHostHygieneDispatch:
    if intent.mode != "manual" and (current_run_id is None or current_run_id <= 0):
        raise ValueError("Scheduled dispatch requires its authenticated run ID.")
    date = datetime.now(timezone.utc).date().isoformat()
    prefix = f"runner-host-hygiene/{date}/{configuration.host_name}-scheduled"
    common: dict[str, object] = {"current_runner_name": intent.current_runner_name}
    requests: tuple[RunnerHostHygieneExecutorRequest, ...]
    if intent.mode == "manual":
        options = intent.model_dump(exclude={"mode", "audit_record_key", "current_runner_name"})
        builder = intent.target_buildkit_builder.strip().lower()
        options["target_buildkit_builder"] = builder
        if builder:
            if builder not in configuration.buildkit_cache_budgets:
                raise ValueError("The target builder is not configured.")
            options.update(
                allowed_buildkit_builders=tuple(configuration.buildkit_cache_budgets),
                max_used_space_bytes=configuration.buildkit_cache_budgets[builder],
            )
        elif intent.action != "prune_docker_cache":
            options["max_used_space_bytes"] = 0
        if intent.action == "remove_buildkit_state_volumes":
            options["allowed_buildkit_state_volumes"] = configuration.allowed_buildkit_state_volumes
        requests = (
            configuration.executor_request(
                audit_record_key=intent.audit_record_key,
                **common,
                **options,
            ),
        )
    elif intent.mode == "report":
        requests = (
            configuration.executor_request(
                audit_record_key=f"{prefix}-report-{current_run_id}",
                prune_until="720h",
                timeout_seconds=900,
                **common,
            ),
        )
    else:
        requests = (
            configuration.executor_request(
                audit_record_key=f"{prefix}-default-cache-{current_run_id}",
                mutate=True,
                prune_until="720h",
                timeout_seconds=900,
                **common,
            ),
            configuration.executor_request(
                audit_record_key=f"{prefix}-dangling-images-{current_run_id}",
                action="prune_dangling_images",
                max_used_space_bytes=0,
                mutate=True,
                timeout_seconds=900,
                **common,
            ),
            *(
                configuration.executor_request(
                    audit_record_key=f"{prefix}-builder-{builder}-{current_run_id}",
                    target_buildkit_builder=builder,
                    allowed_buildkit_builders=tuple(configuration.buildkit_cache_budgets),
                    max_used_space_bytes=budget,
                    mutate=True,
                    timeout_seconds=900,
                    **common,
                )
                for builder, budget in configuration.buildkit_cache_budgets.items()
            ),
            *(
                configuration.executor_request(
                    audit_record_key=f"{prefix}-generated-{root.key}-{current_run_id}",
                    action="prune_generated_run_cache",
                    target_generated_cache_key=root.key,
                    max_used_space_bytes=0,
                    mutate=True,
                    timeout_seconds=900,
                    **common,
                )
                for root in configuration.generated_run_cache_roots
            ),
        )
    return RunnerHostHygieneDispatch(
        configuration_sha256=configuration_sha256,
        execution_lane=configuration.execution_lane,
        requests=requests,
    )


def require_current_hygiene_configuration(
    store: RuntimeEnvironmentRecordStore,
    expected_sha256: str,
) -> RunnerHostHygieneConfiguration:
    configuration, digest = load_runner_host_hygiene_configuration(store)
    if digest != expected_sha256:
        raise ValueError("Hygiene configuration changed; obtain a fresh dispatch.")
    return configuration
