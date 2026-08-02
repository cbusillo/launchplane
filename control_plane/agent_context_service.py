from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.every_code_summary_read_model import (
    build_every_code_summary_read_model,
)
from control_plane.contracts.preview_readiness_read_model import (
    build_preview_readiness_read_model,
)
from control_plane.contracts.product_environment_read_model import ActionAllowed
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
)
from control_plane.work_graph_service import (
    WorkGraphPlanningFactsProvider,
    WorkGraphWorkRequestStore,
    build_repo_product_mapping_service_payload,
    build_work_graph_snapshot_service_payload,
)


LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
AgentContextSectionStatus = Literal["available", "unauthorized", "unavailable"]


class AgentContextSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentContextSectionStatus
    payload: dict[str, object] = Field(default_factory=dict)
    reason_code: str = ""


class AgentContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    repository: str = ""
    source: dict[str, object]
    sections: dict[str, AgentContextSection]

    @model_validator(mode="after")
    def _validate_payload(self) -> "AgentContextPayload":
        if not self.generated_at.strip():
            raise ValueError("agent context payload requires generated_at")
        return self


UtcNow = Callable[[], str]


def build_agent_context_service_payload(
    *,
    generated_at: str,
    repository: str,
    product_store: object,
    work_request_store: WorkGraphWorkRequestStore,
    preview_readiness_store: object,
    action_allowed: ActionAllowed,
    planning_facts_provider: WorkGraphPlanningFactsProvider | None,
    tenant_admission_section: AgentContextSection | None = None,
) -> AgentContextPayload:
    normalized_repository = repository.strip()
    sections = {
        "repo_product_mapping": _available_section(
            build_repo_product_mapping_service_payload(
                generated_at=generated_at,
                product_store=product_store,  # type: ignore[arg-type]
                work_request_store=work_request_store,
            )
        ),
        "work_graph_snapshot": _build_work_graph_snapshot_section(
            generated_at=generated_at,
            product_store=product_store,
            work_request_store=work_request_store,
            action_allowed=action_allowed,
            planning_facts_provider=planning_facts_provider,
        ),
        "every_code_summary": _available_section(
            {
                "summary": build_every_code_summary_read_model(
                    generated_at=generated_at,
                    record_store=work_request_store,
                    repository=normalized_repository,
                    limit=25,
                    offset=0,
                ).model_dump(mode="json")
            }
        ),
        "preview_readiness": _available_section(
            {
                "readiness": build_preview_readiness_read_model(
                    generated_at=generated_at,
                    record_store=preview_readiness_store,  # type: ignore[arg-type]
                    repository=normalized_repository,
                    limit=25,
                    offset=0,
                ).model_dump(mode="json")
            }
        ),
    }
    if tenant_admission_section is not None:
        sections["tenant_admission"] = tenant_admission_section
    return AgentContextPayload(
        generated_at=generated_at,
        repository=normalized_repository,
        source={
            "section_count": len(sections),
            "available_section_count": sum(
                1 for section in sections.values() if section.status == "available"
            ),
        },
        sections=sections,
    )


def agent_context_allowed(
    *, authz_policy: LaunchplaneAuthzPolicy, identity: LaunchplaneIdentity
) -> bool:
    return authz_policy.allows(
        identity=identity,
        action="product_environment.read",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
        target=AuthorizationTarget(scope="context"),
    )


def agent_context_action_allowed(
    *, authz_policy: LaunchplaneAuthzPolicy, identity: LaunchplaneIdentity
) -> ActionAllowed:
    def action_allowed(
        requested_action: str,
        requested_product: str,
        requested_context: str,
        requested_instances: tuple[str, ...],
    ) -> bool:
        return authz_policy.allows(
            identity=identity,
            action=requested_action,
            product=requested_product,
            context=requested_context,
            target=(
                AuthorizationTarget(scope="instance", instances=requested_instances)
                if requested_instances
                else AuthorizationTarget(scope="context")
            ),
        )

    return action_allowed


def _build_work_graph_snapshot_section(
    *,
    generated_at: str,
    product_store: object,
    work_request_store: WorkGraphWorkRequestStore,
    action_allowed: ActionAllowed,
    planning_facts_provider: WorkGraphPlanningFactsProvider | None,
) -> AgentContextSection:
    try:
        return _available_section(
            build_work_graph_snapshot_service_payload(
                generated_at=generated_at,
                product_store=product_store,  # type: ignore[arg-type]
                work_request_store=work_request_store,
                action_allowed=action_allowed,
                planning_facts_provider=planning_facts_provider,
            )
        )
    except RuntimeError:
        return AgentContextSection(status="unavailable", reason_code="work_graph_unavailable")


def _available_section(payload: dict[str, object]) -> AgentContextSection:
    return AgentContextSection(status="available", payload=payload)
