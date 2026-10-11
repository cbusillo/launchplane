"""Reviewed reconciliation of one legacy preview after complete provider absence proof."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.dokploy import api as dokploy_api, source as dokploy_source
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import (
    effective_preview_app_name_prefix,
    preview_application_name,
    resolve_generic_web_preview_slug,
)

LEGACY_PREVIEW_RECONCILIATION_ROUTE = "/v1/previews/legacy-generic-web/reconciliation"


class LegacyPreviewReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=256)
    preview_id: str = Field(min_length=1, max_length=256)
    mode: Literal["inspect", "plan", "apply"] = "inspect"
    reason: str = Field(min_length=1, max_length=2048)
    plan_idempotency_key: str = ""
    expected_plan_digest: str = ""
    reviewed_plan: bool = False

    @model_validator(mode="after")
    def validate_review(self) -> "LegacyPreviewReconciliationRequest":
        for name in ("product", "preview_id", "reason"):
            value = getattr(self, name).strip()
            if not value:
                raise ValueError(f"{name} must be nonempty")
            setattr(self, name, value)
        if self.mode == "apply":
            if (
                not self.reviewed_plan
                or not self.plan_idempotency_key.strip()
                or len(self.expected_plan_digest) != 64
                or any(c not in "0123456789abcdef" for c in self.expected_plan_digest)
            ):
                raise ValueError("Apply requires the reviewed saved plan and its digest.")
        elif self.reviewed_plan or self.plan_idempotency_key or self.expected_plan_digest:
            raise ValueError("Review fields belong only to apply.")
        return self


@dataclass(frozen=True)
class LegacyPreviewAuthority:
    profile: LaunchplaneProductProfileRecord
    preview: PreviewRecord
    slug: str
    application_name: str
    generations: tuple[PreviewGenerationRecord, ...]
    digest: str
    known_other_target_ids: frozenset[str]
    preview_target_ids: frozenset[str]


def bind_legacy_preview(
    store: PostgresRecordStore, request: LegacyPreviewReconciliationRequest
) -> LegacyPreviewAuthority:
    profile = store.read_product_profile_record(request.product)
    preview = store.read_preview_record(request.preview_id)
    if profile.driver_id != "generic-web" or not profile.preview.context:
        raise ValueError("Reconciliation requires a generic-web preview profile.")
    if preview.context != profile.preview.context or preview.anchor_repo not in {
        profile.repository,
        profile.repository.partition("/")[2],
    }:
        raise ValueError("Preview does not belong to the requested product.")
    profiles = store.list_product_profile_records()
    if any(
        other.product != profile.product
        and preview.context
        in {
            other.preview.context,
            *other.historical_contexts,
            *(lane.context for lane in other.lanes),
        }
        for other in profiles
    ):
        raise ValueError("Preview context has shared or uncertain ownership.")
    previews = store.list_preview_records(context_name=preview.context)
    anchors = [p for p in previews if p.anchor_pr_number == preview.anchor_pr_number]
    if len(anchors) != 1 or anchors[0] != preview:
        raise ValueError("Preview anchor is ambiguous.")
    slug = resolve_generic_web_preview_slug(
        profile=profile,
        preview_slug="",
        anchor_pr_number=preview.anchor_pr_number,
        label="Reconciliation",
    )
    prefix = effective_preview_app_name_prefix(profile=profile)
    application_name = preview_application_name(app_name_prefix=prefix, preview_slug=slug)
    generations = tuple(
        sorted(
            store.list_preview_generation_records(preview_id=preview.preview_id),
            key=lambda g: g.generation_id,
        )
    )
    generation_ids = {g.generation_id for g in generations}
    deployment_evidence: list[dict[str, object]] = []
    preview_target_ids: set[str] = set()
    for generation in generations:
        runtime = generation.runtime_identity
        if runtime is None:
            continue
        if (
            runtime.product
            and runtime.product != profile.product
            or runtime.context != preview.context
            or runtime.instance != slug
            or runtime.preview_id
            and runtime.preview_id != preview.preview_id
        ):
            raise ValueError("Historical runtime evidence belongs to another preview.")
        try:
            deployment = store.read_deployment_record(runtime.deployment_record_id)
        except FileNotFoundError:
            deployment_evidence.append({"missing_deployment": runtime.deployment_record_id})
            continue
        deployment_evidence.append(deployment.model_dump(mode="json"))
        if deployment.deployed_target is not None:
            if (
                deployment.deployed_target.provider_id != "dokploy"
                or deployment.deployed_target.target_category != "application"
            ):
                raise ValueError("Historical deployment has unsupported provider evidence.")
            preview_target_ids.add(deployment.deployed_target.target_id)
    if any(
        pointer and pointer not in generation_ids
        for pointer in (
            preview.active_generation_id,
            preview.serving_generation_id,
            preview.latest_generation_id,
        )
    ) or any(g.state in {"resolving", "building", "deploying", "verifying"} for g in generations):
        raise ValueError("Preview generation evidence is incomplete or still running.")
    if store.list_product_reconcile_requests(product=profile.product, state="running", limit=None):
        raise ValueError("Product reconciliation is still running.")
    targets = store.list_provider_target_records()
    target_ids = store.list_dokploy_target_id_records()
    target_configs = store.list_dokploy_target_records()
    # Any authority for this preview must first be resolved through its tracked teardown.
    scoped_targets: tuple[
        ProviderTargetRecord | DokployTargetIdRecord | DokployTargetRecord, ...
    ] = (*targets, *target_ids, *target_configs)
    physical_targets: tuple[ProviderTargetRecord | DokployTargetIdRecord, ...] = (
        *targets,
        *target_ids,
    )
    if any(t.context == preview.context and t.instance == slug for t in scoped_targets):
        raise ValueError("Preview has tracked target authority; use tracked teardown first.")
    lane_scopes = {(lane.context, lane.instance) for lane in profile.lanes}
    sibling_scopes = {
        (
            other.context,
            resolve_generic_web_preview_slug(
                profile=profile,
                preview_slug="",
                anchor_pr_number=other.anchor_pr_number,
                label="Reconciliation",
            ),
        )
        for other in previews
        if other.preview_id != preview.preview_id
        and other.state != "destroyed"
        and other.anchor_repo in {profile.repository, profile.repository.partition("/")[2]}
        and sum(p.anchor_pr_number == other.anchor_pr_number for p in previews) == 1
    }
    other_scopes = lane_scopes | sibling_scopes
    other_target_ids = frozenset(
        t.target_id
        for t in targets
        if (t.context, t.instance) in other_scopes and t.provider_id == "dokploy"
    ) | frozenset(t.target_id for t in target_ids if (t.context, t.instance) in other_scopes)
    for target_id in other_target_ids:
        owners = {(t.context, t.instance) for t in physical_targets if t.target_id == target_id}
        if len(owners) != 1 or not owners <= other_scopes:
            raise ValueError("Provider target ownership is shared.")
    digest = canonical_sha256(
        {
            "profile": profile.model_dump(mode="json"),
            "preview": preview.model_dump(mode="json"),
            "generations": [g.model_dump(mode="json") for g in generations],
            "deployments": deployment_evidence,
            "known_other_target_ids": sorted(other_target_ids),
            "targets": [
                t.model_dump(mode="json")
                for t in targets
                if t.context == preview.context or (t.context, t.instance) in lane_scopes
            ],
            "target_ids": [
                t.model_dump(mode="json")
                for t in target_ids
                if t.context == preview.context or (t.context, t.instance) in lane_scopes
            ],
            "target_configs": [
                t.model_dump(mode="json")
                for t in target_configs
                if t.context == preview.context or (t.context, t.instance) in lane_scopes
            ],
        }
    )
    return LegacyPreviewAuthority(
        profile,
        preview,
        slug,
        application_name,
        generations,
        digest,
        other_target_ids,
        frozenset(preview_target_ids),
    )


def observe_legacy_preview(
    *, control_plane_root: Path, bound: LegacyPreviewAuthority
) -> dict[str, object]:
    """Enumerate twice; inspect payloads and domains, including renamed candidates."""
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    applications = dokploy_api.search_dokploy_applications(host=host, token=token)
    seen: dict[str, dict[str, object]] = {}
    present = False
    domain_host = urlsplit(bound.preview.canonical_url).hostname
    if not domain_host:
        raise ValueError("Preview has no usable domain evidence.")
    for item in applications:
        target_id = str(item.get("applicationId") or item.get("id") or "").strip()
        if not target_id:
            raise ValueError("Provider inventory is incomplete.")
        app = dokploy_api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=target_id
        )
        if str(app.get("applicationId") or app.get("id") or "").strip() != target_id:
            raise ValueError("Provider identity changed during inspection.")
        name = str(app.get("name") or "").strip()
        app_name = str(app.get("appName") or "").strip()
        if not name or not app_name:
            raise ValueError("Provider application naming evidence is incomplete.")
        domains = dokploy_api.fetch_dokploy_application_domains(
            host=host, token=token, application_id=target_id, strict=True
        )
        domain_names = sorted(
            str(d.get("host") or d.get("domain") or "").strip().lower() for d in domains
        )
        if any(not d for d in domain_names):
            raise ValueError("Provider domain evidence is incomplete.")
        exact_candidate = (
            target_id in bound.preview_target_ids
            or name.lower() == bound.application_name.lower()
            or app_name.lower() == f"{bound.profile.product}-{bound.slug}".lower()
            or app_name.lower().startswith(f"{bound.profile.product}-{bound.slug}-".lower())
            or domain_host.lower() in domain_names
        )
        repository = bound.profile.repository.lower()
        image_repository = bound.profile.image.repository
        repository_values = [
            str(app.get(key) or "").strip().lower().rstrip("/").removesuffix(".git")
            for key in (
                "repository",
                "customGitUrl",
                "githubRepository",
                "gitlabRepository",
                "giteaRepository",
                "bitbucketRepository",
            )
        ]
        image = str(app.get("dockerImage") or "").strip()
        potential_product = (
            name.lower().startswith(
                effective_preview_app_name_prefix(profile=bound.profile).lower() + "-"
            )
            or app_name.lower().startswith(bound.profile.product.lower() + "-")
            or any(
                repository in value or value == repository.partition("/")[2]
                for value in repository_values
            )
            or image == image_repository
            or image.startswith((image_repository + ":", image_repository + "@"))
        )
        if exact_candidate:
            present = True
        elif potential_product and target_id not in bound.known_other_target_ids:
            raise ValueError("Provider has an unbound or renamed product candidate.")
        seen[target_id] = {
            "id": target_id,
            "name": name,
            "app_name": app_name,
            "domains": domain_names,
            "image": image,
            "repositories": repository_values,
        }
    final = dokploy_api.search_dokploy_applications(host=host, token=token)
    if sorted(str(a.get("applicationId") or a.get("id")) for a in applications) != sorted(
        str(a.get("applicationId") or a.get("id")) for a in final
    ):
        raise ValueError("Provider inventory changed during inspection.")
    return {
        "provider_state": "present" if present else "absent",
        "provider_absence_verified": not present,
        "inventory_digest": canonical_sha256(seen),
        "provider_writes": False,
    }


def plan_legacy_preview(
    *,
    store: PostgresRecordStore,
    control_plane_root: Path,
    request: LegacyPreviewReconciliationRequest,
    caller_scope: str,
) -> tuple[LegacyPreviewAuthority, dict[str, object]]:
    bound = bind_legacy_preview(store, request)
    observation = observe_legacy_preview(control_plane_root=control_plane_root, bound=bound)
    if bind_legacy_preview(store, request).digest != bound.digest:
        raise ValueError("Preview authority changed during inspection.")
    result: dict[str, object] = {
        "product": bound.profile.product,
        "preview_id": bound.preview.preview_id,
        "context": bound.preview.context,
        "preview_slug": bound.slug,
        "preview_state": bound.preview.state,
        "destroyed_at": bound.preview.destroyed_at,
        "action": "reconcile_absent"
        if observation["provider_absence_verified"]
        else "blocked_provider_present",
        "apply_eligible": observation["provider_absence_verified"]
        and bound.preview.state != "destroyed",
        "authority_digest": bound.digest,
        **observation,
    }
    result["plan_digest"] = canonical_sha256(
        {
            "caller_scope": caller_scope,
            "reason": request.reason,
            "result": {key: value for key, value in result.items() if key != "inventory_digest"},
        }
    )
    return bound, result
