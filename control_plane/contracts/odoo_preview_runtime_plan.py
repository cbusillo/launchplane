from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


OdooPreviewRuntimeOperation = Literal["refresh", "destroy"]
OdooPreviewRuntimeStrategy = Literal[
    "isolated_dokploy_compose",
    "staged_compose_mvp",
    "unknown",
]
OdooPreviewRuntimePlanStatus = Literal["ready", "blocked"]
OdooPreviewRuntimeTargetKind = Literal["preview", "testing", "stable", "prod", "unknown"]
OdooPreviewRuntimeBindingSource = Literal[
    "runtime_environment",
    "managed_secret",
    "generated",
    "artifact_manifest",
    "default",
]
OdooPreviewRuntimeBindingScope = Literal["preview", "non_prod", "prod"]
OdooPreviewRuntimeBlockerCode = Literal[
    "default_credential_binding",
    "destroy_target_missing",
    "provider_capability_missing",
    "runtime_env_missing",
    "runtime_strategy_not_isolated",
    "runtime_target_not_preview",
    "runtime_target_slug_mismatch",
    "preview_url_missing",
    "prod_secret_binding",
    "refresh_artifact_missing",
    "refresh_source_missing",
]


_SENSITIVE_DEFAULT_KEYS = frozenset(
    {
        "ADMIN_PASSWD",
        "ODOO_ADMIN_PASSWORD",
        "ODOO_DB_PASSWORD",
        "ODOO_MASTER_PASSWORD",
        "POSTGRES_PASSWORD",
    }
)


class OdooPreviewProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_create_compose: bool = False
    can_update_compose_env: bool = False
    can_deploy_compose: bool = False
    can_bind_domain: bool = False
    can_delete_compose: bool = False
    can_delete_domain: bool = False


class OdooPreviewRuntimeBindingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    source: OdooPreviewRuntimeBindingSource
    scope: OdooPreviewRuntimeBindingScope = "preview"
    present: bool = True

    @model_validator(mode="after")
    def _normalize_binding(self) -> "OdooPreviewRuntimeBindingEvidence":
        self.key = _required_text(self.key, "Odoo preview runtime binding requires key")
        return self


class OdooPreviewRuntimeTargetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["dokploy_compose"] = "dokploy_compose"
    target_id: str
    target_name: str
    context: str
    instance: str
    environment_kind: OdooPreviewRuntimeTargetKind = "unknown"
    domain: str = ""

    @model_validator(mode="after")
    def _normalize_target(self) -> "OdooPreviewRuntimeTargetEvidence":
        self.target_id = _required_text(
            self.target_id, "Odoo preview runtime target requires target_id"
        )
        self.target_name = _required_text(
            self.target_name, "Odoo preview runtime target requires target_name"
        )
        self.context = _required_text(self.context, "Odoo preview runtime target requires context")
        self.instance = _required_text(
            self.instance, "Odoo preview runtime target requires instance"
        )
        self.domain = self.domain.strip().lower()
        return self


class OdooPreviewRuntimePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation: OdooPreviewRuntimeOperation
    product: str
    repository: str
    pr_number: int = Field(ge=1)
    preview_slug: str
    preview_url: str = ""
    strategy: OdooPreviewRuntimeStrategy = "unknown"
    image_reference: str = ""
    source_git_ref: str = ""
    target: OdooPreviewRuntimeTargetEvidence | None = None
    provider_capabilities: OdooPreviewProviderCapabilities = Field(
        default_factory=OdooPreviewProviderCapabilities
    )
    runtime_bindings: tuple[OdooPreviewRuntimeBindingEvidence, ...] = ()
    required_runtime_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_request(self) -> "OdooPreviewRuntimePlanRequest":
        self.product = _required_text(self.product, "Odoo preview runtime plan requires product")
        self.repository = _required_text(
            self.repository, "Odoo preview runtime plan requires repository"
        )
        self.preview_slug = _required_text(
            self.preview_slug, "Odoo preview runtime plan requires preview_slug"
        )
        self.preview_url = self.preview_url.strip()
        self.image_reference = self.image_reference.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.required_runtime_keys = _normalized_unique_texts(self.required_runtime_keys)
        return self


class OdooPreviewRuntimeBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: OdooPreviewRuntimeBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "OdooPreviewRuntimeBlocker":
        self.message = _required_text(self.message, "Odoo preview runtime blocker requires message")
        return self


class OdooPreviewRuntimeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    target: str

    @model_validator(mode="after")
    def _normalize_action(self) -> "OdooPreviewRuntimeAction":
        self.action = _required_text(self.action, "Odoo preview runtime action requires action")
        self.target = _required_text(self.target, "Odoo preview runtime action requires target")
        return self


class OdooPreviewRuntimePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: OdooPreviewRuntimePlanStatus
    operation: OdooPreviewRuntimeOperation
    product: str
    repository: str
    pr_number: int = Field(ge=1)
    preview_slug: str
    preview_url: str
    strategy: OdooPreviewRuntimeStrategy
    target: OdooPreviewRuntimeTargetEvidence | None = None
    blockers: tuple[OdooPreviewRuntimeBlocker, ...] = ()
    planned_actions: tuple[OdooPreviewRuntimeAction, ...] = ()
    rollback_actions: tuple[OdooPreviewRuntimeAction, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "OdooPreviewRuntimePlan":
        self.product = _required_text(self.product, "Odoo preview runtime plan requires product")
        self.repository = _required_text(
            self.repository, "Odoo preview runtime plan requires repository"
        )
        self.preview_slug = _required_text(
            self.preview_slug, "Odoo preview runtime plan requires preview_slug"
        )
        self.preview_url = self.preview_url.strip()
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.summary = _required_text(self.summary, "Odoo preview runtime plan requires summary")
        if self.status == "ready" and self.blockers:
            raise ValueError("ready Odoo preview runtime plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked Odoo preview runtime plan requires blockers")
        return self


def plan_odoo_preview_runtime(*, request: OdooPreviewRuntimePlanRequest) -> OdooPreviewRuntimePlan:
    blockers: list[OdooPreviewRuntimeBlocker] = []

    if request.strategy != "isolated_dokploy_compose":
        blockers.append(
            _blocker(
                "runtime_strategy_not_isolated",
                "Odoo preview runtime requires an isolated per-PR Dokploy compose strategy.",
            )
        )
    if not request.preview_url:
        blockers.append(
            _blocker(
                "preview_url_missing",
                "Odoo preview runtime requires a resolved public preview URL before provider apply.",
            )
        )

    _validate_provider_capabilities(request=request, blockers=blockers)
    _validate_runtime_bindings(request=request, blockers=blockers)
    if request.target is not None:
        _validate_preview_target(request=request, blockers=blockers)
    elif request.operation == "destroy":
        blockers.append(
            _blocker(
                "destroy_target_missing",
                "Odoo preview destroy requires matching preview runtime target evidence.",
            )
        )

    if request.operation == "refresh":
        if not request.image_reference:
            blockers.append(
                _blocker(
                    "refresh_artifact_missing",
                    "Odoo preview refresh requires an immutable image reference.",
                )
            )
        if not request.source_git_ref:
            blockers.append(
                _blocker(
                    "refresh_source_missing",
                    "Odoo preview refresh requires source revision evidence.",
                )
            )

    status: OdooPreviewRuntimePlanStatus = "blocked" if blockers else "ready"
    planned_actions, rollback_actions = _actions_for_request(request=request, status=status)
    return OdooPreviewRuntimePlan(
        status=status,
        operation=request.operation,
        product=request.product,
        repository=request.repository,
        pr_number=request.pr_number,
        preview_slug=request.preview_slug,
        preview_url=request.preview_url,
        strategy=request.strategy,
        target=request.target,
        blockers=tuple(blockers),
        planned_actions=planned_actions,
        rollback_actions=rollback_actions,
        summary=(
            "Odoo preview runtime plan is ready"
            if status == "ready"
            else "Odoo preview runtime plan is blocked"
        ),
    )


def _validate_provider_capabilities(
    *, request: OdooPreviewRuntimePlanRequest, blockers: list[OdooPreviewRuntimeBlocker]
) -> None:
    capabilities = request.provider_capabilities
    required = {
        "refresh": (
            ("can_create_compose", capabilities.can_create_compose),
            ("can_update_compose_env", capabilities.can_update_compose_env),
            ("can_deploy_compose", capabilities.can_deploy_compose),
            ("can_bind_domain", capabilities.can_bind_domain),
            ("can_delete_compose", capabilities.can_delete_compose),
            ("can_delete_domain", capabilities.can_delete_domain),
        ),
        "destroy": (
            ("can_delete_compose", capabilities.can_delete_compose),
            ("can_delete_domain", capabilities.can_delete_domain),
        ),
    }[request.operation]
    missing = tuple(name for name, enabled in required if not enabled)
    if missing:
        blockers.append(
            _blocker(
                "provider_capability_missing",
                "Odoo preview runtime provider capabilities are missing: " + ", ".join(missing),
            )
        )


def _validate_runtime_bindings(
    *, request: OdooPreviewRuntimePlanRequest, blockers: list[OdooPreviewRuntimeBlocker]
) -> None:
    present_bindings = {binding.key for binding in request.runtime_bindings if binding.present}
    missing = tuple(key for key in request.required_runtime_keys if key not in present_bindings)
    if missing:
        blockers.append(
            _blocker(
                "runtime_env_missing",
                "Odoo preview runtime is missing required runtime bindings: " + ", ".join(missing),
            )
        )
    prod_bindings = tuple(
        binding.key
        for binding in request.runtime_bindings
        if binding.present and binding.scope == "prod"
    )
    if prod_bindings:
        blockers.append(
            _blocker(
                "prod_secret_binding",
                "Odoo preview runtime cannot use prod-scoped bindings: " + ", ".join(prod_bindings),
            )
        )
    default_sensitive = tuple(
        binding.key
        for binding in request.runtime_bindings
        if binding.present
        and binding.source == "default"
        and binding.key.upper() in _SENSITIVE_DEFAULT_KEYS
    )
    if default_sensitive:
        blockers.append(
            _blocker(
                "default_credential_binding",
                "Odoo preview runtime cannot use default sensitive credentials: "
                + ", ".join(default_sensitive),
            )
        )


def _validate_preview_target(
    *, request: OdooPreviewRuntimePlanRequest, blockers: list[OdooPreviewRuntimeBlocker]
) -> None:
    assert request.target is not None
    target = request.target
    if target.environment_kind != "preview":
        blockers.append(
            _blocker(
                "runtime_target_not_preview",
                f"Odoo preview runtime target is not preview-scoped: {target.environment_kind}",
            )
        )
    preview_token = f"pr-{request.pr_number}"
    haystack = " ".join(
        (
            target.target_id.lower(),
            target.target_name.lower(),
            target.instance.lower(),
            target.domain.lower(),
        )
    )
    if request.preview_slug.lower() not in haystack and preview_token not in haystack:
        blockers.append(
            _blocker(
                "runtime_target_slug_mismatch",
                "Odoo preview runtime target does not match the requested preview slug or PR number.",
            )
        )


def _actions_for_request(
    *, request: OdooPreviewRuntimePlanRequest, status: OdooPreviewRuntimePlanStatus
) -> tuple[tuple[OdooPreviewRuntimeAction, ...], tuple[OdooPreviewRuntimeAction, ...]]:
    if status != "ready":
        return (), ()
    if request.operation == "destroy":
        target = _target_label(request=request)
        return (
            (
                OdooPreviewRuntimeAction(action="delete_preview_domain", target=target),
                OdooPreviewRuntimeAction(action="delete_preview_compose", target=target),
            ),
            (),
        )
    target = _target_label(request=request)
    return (
        (
            OdooPreviewRuntimeAction(action="create_or_reuse_preview_compose", target=target),
            OdooPreviewRuntimeAction(action="render_preview_runtime_env", target=target),
            OdooPreviewRuntimeAction(action="bind_preview_domain", target=request.preview_url),
            OdooPreviewRuntimeAction(action="deploy_preview_compose", target=target),
            OdooPreviewRuntimeAction(action="run_preview_smoke", target=request.preview_url),
        ),
        (
            OdooPreviewRuntimeAction(
                action="delete_created_preview_domain", target=request.preview_url
            ),
            OdooPreviewRuntimeAction(action="delete_created_preview_compose", target=target),
        ),
    )


def _target_label(*, request: OdooPreviewRuntimePlanRequest) -> str:
    if request.target is None:
        return f"{request.product}-{request.preview_slug}"
    return request.target.target_id


def _blocker(code: OdooPreviewRuntimeBlockerCode, message: str) -> OdooPreviewRuntimeBlocker:
    return OdooPreviewRuntimeBlocker(code=code, message=message)


def _required_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _normalized_unique_texts(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError("Odoo preview runtime keys must be non-empty")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)
