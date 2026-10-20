"""Reviewed reconciliation of one legacy preview after complete provider absence proof."""

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from collections.abc import Iterator
from collections.abc import Mapping
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
from control_plane.drivers.registry import product_profile_uses_odoo_driver
from control_plane.contracts.preview_execution import (
    odoo_preview_reconciliation_key,
    odoo_preview_target_key,
    preview_execution_lock_name,
)
from control_plane.legacy_preview_completion import (
    LEGACY_PREVIEW_RECONCILIATION_ROUTE as LEGACY_PREVIEW_RECONCILIATION_ROUTE,
)
from control_plane.workflows.launchplane import generate_preview_id
from control_plane.workflows.generic_web_preview import (
    effective_preview_app_name_prefix,
    preview_application_name,
    resolve_generic_web_preview_slug,
    serialize_generic_web_preview_operation,
)


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
    known_other_application_ids: frozenset[str] = frozenset()


def bind_legacy_preview(
    store: PostgresRecordStore,
    request: LegacyPreviewReconciliationRequest,
    *,
    allow_tracked: bool = False,
) -> LegacyPreviewAuthority:
    if allow_tracked and product_profile_uses_odoo_driver(
        store.read_product_profile_record(request.product)
    ):
        raise ValueError("Integrated product retirement requires generic-web previews.")
    with serialize_legacy_preview_operation(store, request):
        return _bind_legacy_preview(store, request, allow_tracked=allow_tracked)


def _preview_name(profile: LaunchplaneProductProfileRecord, slug: str) -> str:
    return preview_application_name(
        app_name_prefix=effective_preview_app_name_prefix(profile=profile), preview_slug=slug
    )


def _odoo_lock_name(profile: LaunchplaneProductProfileRecord, slug: str) -> str:
    return preview_execution_lock_name(
        odoo_preview_target_key(
            context=profile.preview.context, compose_name=_preview_name(profile, slug)
        )
    )


@contextmanager
def serialize_legacy_preview_operation(
    store: PostgresRecordStore,
    request: LegacyPreviewReconciliationRequest,
    *,
    product_mutation: bool = False,
) -> Iterator[None]:
    profile = store.read_product_profile_record(request.product)
    preview = store.read_preview_record(request.preview_id)
    slug = resolve_generic_web_preview_slug(
        profile=profile,
        preview_slug="",
        anchor_pr_number=preview.anchor_pr_number,
        label="Reconciliation",
    )
    if product_profile_uses_odoo_driver(profile):
        name = _odoo_lock_name(profile, slug)
        if not store.testing_replacement_execution_is_excluded(name):
            # Take the actual Odoo executor BEFORE generic preview/authority locks.
            with store.testing_replacement_execution_lock(name) as check:
                if check is None:
                    raise ValueError(
                        "Preview executor is active; resume original preview recovery."
                    )
                check()
                with serialize_legacy_preview_operation(
                    store, request, product_mutation=product_mutation
                ):
                    yield
                check()
            return
    logical_id = generate_preview_id(
        context_name=profile.preview.context,
        anchor_repo=profile.repository.partition("/")[2],
        anchor_pr_number=preview.anchor_pr_number,
    )
    with ExitStack() as locks:
        if product_profile_uses_odoo_driver(profile) and not store.preview_refresh_is_excluded(
            preview_id=logical_id
        ):
            locks.enter_context(
                store.serialize_preview_refresh(preview_id=logical_id, dedicated=True)
            )
        if not _generic_execution_excluded(store, profile, slug):
            locks.enter_context(
                serialize_generic_web_preview_operation(
                    record_store=store,
                    profile=profile,
                    preview_slug=slug,
                    product_mutation=product_mutation,
                )
            )
        yield


def _generic_execution_excluded(
    store: PostgresRecordStore, profile: LaunchplaneProductProfileRecord, slug: str
) -> bool:
    return store.preview_refresh_is_excluded(
        preview_id=f"product-retirement:{profile.product}"
    ) or store.preview_refresh_is_excluded(
        preview_id=f"{profile.product}:{profile.preview.context}:{slug}"
    )


def _execution_excluded(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    slug: str,
    anchor_pr_number: int,
) -> bool:
    if product_profile_uses_odoo_driver(profile):
        logical_id = generate_preview_id(
            context_name=profile.preview.context,
            anchor_repo=profile.repository.partition("/")[2],
            anchor_pr_number=anchor_pr_number,
        )
        return store.preview_refresh_is_excluded(
            preview_id=logical_id
        ) and store.testing_replacement_execution_is_excluded(
            _odoo_lock_name(profile, slug), require_postgres=True
        )
    return _generic_execution_excluded(store, profile, slug)


def _physical_dokploy_targets(
    targets: tuple[ProviderTargetRecord, ...],
    ids: tuple[DokployTargetIdRecord, ...],
    configs: tuple[DokployTargetRecord, ...],
) -> tuple[ProviderTargetRecord, ...]:
    physical = [t for t in targets if t.provider_id == "dokploy"]
    for legacy in ids:
        config = next(
            (c for c in configs if (c.context, c.instance) == (legacy.context, legacy.instance)),
            None,
        )
        if config is None:
            # An untyped ID without its config cannot exclude any provider category.
            continue
        joined = ProviderTargetRecord.from_dokploy_records(
            target_record=config, target_id_record=legacy
        )
        physical.append(joined)
    return tuple(physical)


def _require_no_held_odoo_work(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    slug: str,
    previews: tuple[PreviewRecord, ...],
) -> None:
    key = odoo_preview_reconciliation_key(
        context=profile.preview.context, compose_name=_preview_name(profile, slug)
    )
    target_key = odoo_preview_target_key(
        context=profile.preview.context, compose_name=_preview_name(profile, slug)
    )
    siblings: set[str] = set()
    for preview in previews:
        other_slug = resolve_generic_web_preview_slug(
            profile=profile,
            preview_slug="",
            anchor_pr_number=preview.anchor_pr_number,
            label="Reconciliation",
        )
        if (
            other_slug != slug
            and preview.state != "destroyed"
            and preview.anchor_repo in {profile.repository, profile.repository.partition("/")[2]}
            and sum(p.anchor_pr_number == preview.anchor_pr_number for p in previews) == 1
        ):
            siblings.add(
                odoo_preview_reconciliation_key(
                    context=profile.preview.context, compose_name=_preview_name(profile, other_slug)
                )
            )
    prefix = f"dokploy:compose:{profile.preview.context.strip()}:"
    for held in store.list_held_provider_target_reservations():
        if (
            held.provider_target_key in {key, target_key}
            or held.reconciliation_key == key
            or (
                held.reconciliation_key.startswith(prefix)
                and held.reconciliation_key not in siblings
            )
        ):
            raise ValueError(
                "Held preview work needs original-operation recovery before reconciliation."
            )


def _bind_legacy_preview(
    store: PostgresRecordStore,
    request: LegacyPreviewReconciliationRequest,
    *,
    allow_tracked: bool,
) -> LegacyPreviewAuthority:
    profile = store.read_product_profile_record(request.product)
    preview = store.read_preview_record(request.preview_id)
    odoo = product_profile_uses_odoo_driver(profile)
    if (profile.driver_id != "generic-web" and not odoo) or not profile.preview.context:
        raise ValueError("Reconciliation requires a supported preview profile.")
    if allow_tracked and odoo:
        raise ValueError("Integrated product retirement requires generic-web previews.")
    if preview.context != profile.preview.context or preview.anchor_repo not in {
        profile.repository,
        profile.repository.partition("/")[2],
    }:
        raise ValueError("Preview does not belong to the requested product.")
    if odoo and (
        preview.anchor_repo != profile.repository.partition("/")[2]
        or preview.preview_id
        != generate_preview_id(
            context_name=profile.preview.context,
            anchor_repo=profile.repository.partition("/")[2],
            anchor_pr_number=preview.anchor_pr_number,
        )
    ):
        raise ValueError("Odoo preview identity does not match its lifecycle producer.")
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
    application_name = _preview_name(profile, slug)
    category = "compose" if odoo else "application"
    if odoo:
        if not store.testing_replacement_execution_is_excluded(_odoo_lock_name(profile, slug)):
            raise ValueError("Odoo preview executor exclusion was lost.")
        _require_no_held_odoo_work(store, profile, slug, previews)
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
            or runtime.instance != (application_name if odoo else slug)
            or runtime.preview_id
            and runtime.preview_id not in {preview.preview_id, slug}
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
                or deployment.deployed_target.target_category != category
            ):
                raise ValueError("Historical deployment has unsupported provider evidence.")
            if odoo and deployment.deployed_target.display_name != application_name:
                raise ValueError("Historical compose naming evidence changed.")
            preview_target_ids.add(deployment.deployed_target.target_id)
    pointers = {
        preview.active_generation_id,
        preview.serving_generation_id,
        preview.latest_generation_id,
    } - {""}
    nonterminal = tuple(
        g for g in generations if g.state in {"resolving", "building", "deploying", "verifying"}
    )
    qualified_legacy_odoo = (
        odoo
        and bool(pointers)
        and all(g.runtime_identity is None for g in nonterminal)
        and _execution_excluded(store, profile, slug, preview.anchor_pr_number)
    )
    if any(
        pointer and pointer not in generation_ids
        for pointer in (
            preview.active_generation_id,
            preview.serving_generation_id,
            preview.latest_generation_id,
        )
    ) or (
        nonterminal
        and not qualified_legacy_odoo
        and (
            not pointers
            or any(g.generation_id in pointers for g in nonterminal)
            or not _execution_excluded(store, profile, slug, preview.anchor_pr_number)
        )
    ):
        raise ValueError("Preview generation evidence is incomplete or still running.")
    if store.list_product_reconcile_requests(product=profile.product, state="running", limit=None):
        raise ValueError("Product reconciliation is still running.")
    targets = store.list_provider_target_records()
    target_ids = store.list_dokploy_target_id_records()
    target_configs = store.list_dokploy_target_records()
    own_scopes = {(lane.context, lane.instance) for lane in profile.lanes} | {
        (
            preview.context,
            resolve_generic_web_preview_slug(
                profile=profile,
                preview_slug="",
                anchor_pr_number=p.anchor_pr_number,
                label="Reconciliation",
            ),
        )
        for p in previews
    }
    for legacy in target_ids:
        if (
            (legacy.context, legacy.instance) in own_scopes
            and any(
                other.target_id == legacy.target_id
                and (other.context, other.instance) != (legacy.context, legacy.instance)
                for other in target_ids
            )
            and not any(
                (c.context, c.instance) == (legacy.context, legacy.instance) for c in target_configs
            )
        ):
            raise ValueError("Untyped provider target ownership is ambiguous.")
    # Any authority for this preview must first be resolved through its tracked teardown.
    scoped_targets: tuple[
        ProviderTargetRecord | DokployTargetIdRecord | DokployTargetRecord, ...
    ] = (*targets, *target_ids, *target_configs)
    all_physical_targets = _physical_dokploy_targets(targets, target_ids, target_configs)
    for context, instance in own_scopes:
        identities = {
            (t.target_category, t.target_id)
            for t in all_physical_targets
            if (t.context, t.instance) == (context, instance)
        }
        if len(identities) > 1:
            raise ValueError("Provider target category or identity is ambiguous.")
    physical_targets = tuple(t for t in all_physical_targets if t.target_category == category)
    if not allow_tracked and any(
        t.context == preview.context and t.instance == slug for t in scoped_targets
    ):
        raise ValueError("Preview has tracked target authority; use tracked teardown first.")
    if allow_tracked:
        preview_target_ids.update(
            t.target_id
            for t in physical_targets
            if t.context == preview.context and t.instance == slug
        )
        for target_id in preview_target_ids:
            if any(
                t.target_id == target_id and (t.context, t.instance) != (preview.context, slug)
                for t in physical_targets
            ):
                raise ValueError("Preview provider target has another owner.")
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

    def other_ids(target_category: str) -> frozenset[str]:
        category_targets = tuple(
            t for t in all_physical_targets if t.target_category == target_category
        )
        identifiers = frozenset(
            t.target_id for t in category_targets if (t.context, t.instance) in other_scopes
        )
        for identifier in identifiers:
            owners = {
                (t.context, t.instance) for t in category_targets if t.target_id == identifier
            }
            if len(owners) != 1 or not owners <= other_scopes:
                raise ValueError("Provider target ownership is shared.")
        return identifiers

    other_target_ids = other_ids(category)
    other_application_ids = other_ids("application") if odoo else other_target_ids
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
        other_application_ids,
    )


def observe_legacy_preview(
    *,
    control_plane_root: Path,
    bound: LegacyPreviewAuthority,
    retirement_peers: tuple[LegacyPreviewAuthority, ...] = (),
) -> dict[str, object]:
    """Enumerate twice; inspect payloads and domains, including renamed candidates."""
    if product_profile_uses_odoo_driver(bound.profile):
        from control_plane.legacy_odoo_preview import observe_legacy_odoo_preview

        return observe_legacy_odoo_preview(control_plane_root=control_plane_root, bound=bound)
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
    applications = dokploy_api.search_dokploy_applications(host=host, token=token)
    seen: dict[str, dict[str, object]] = {}
    present = False
    candidates: list[str] = []
    domain_host = urlsplit(bound.preview.canonical_url).hostname
    if not domain_host:
        raise ValueError("Preview has no usable domain evidence.")
    for item in applications:
        target_id = str(item.get("applicationId") or item.get("id") or "").strip()
        if not target_id:
            raise ValueError("Provider inventory is incomplete.")
        if target_id in seen:
            raise ValueError("Provider inventory has duplicate identifiers.")
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
        exact_candidate = legacy_preview_candidate(bound, app, tuple(domains))
        peer_matches = [
            peer
            for peer in retirement_peers
            if peer.preview.preview_id != bound.preview.preview_id
            and legacy_preview_candidate(peer, app, tuple(domains))
        ]
        if exact_candidate and peer_matches:
            raise ValueError("Preview provider identity is shared.")
        if len(peer_matches) > 1:
            raise ValueError("Sibling preview provider identity is ambiguous.")
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
            if (
                retirement_peers
                and target_id not in bound.preview_target_ids
                and not any(
                    value == repository
                    or value.endswith("/" + repository)
                    or value == repository.partition("/")[2]
                    for value in repository_values
                )
                and not (
                    image == image_repository
                    or image.startswith((image_repository + ":", image_repository + "@"))
                )
            ):
                raise ValueError("Preview candidate lacks product source ownership proof.")
            if target_id in bound.known_other_target_ids:
                raise ValueError("Preview candidate belongs to a stable lane or sibling.")
            present = True
            candidates.append(target_id)
        elif (
            potential_product and target_id not in bound.known_other_target_ids and not peer_matches
        ):
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
    result: dict[str, object] = {
        "provider_state": "present" if present else "absent",
        "provider_absence_verified": not present,
        "inventory_digest": canonical_sha256(seen),
        "provider_writes": False,
    }
    if retirement_peers:
        result["candidate_target_ids"] = tuple(sorted(candidates))
    return result


def legacy_preview_candidate(
    bound: LegacyPreviewAuthority,
    app: Mapping[str, object],
    domains: tuple[Mapping[str, object], ...],
) -> bool:
    target_id = str(app.get("applicationId") or app.get("id") or "").strip()
    name = str(app.get("name") or "").strip().lower()
    app_name = str(app.get("appName") or "").strip().lower()
    domain = urlsplit(bound.preview.canonical_url).hostname
    hosts = {str(d.get("host") or d.get("domain") or "").strip().lower() for d in domains}
    return (
        target_id in bound.preview_target_ids
        or name == bound.application_name.lower()
        or app_name == f"{bound.profile.product}-{bound.slug}".lower()
        or app_name.startswith(f"{bound.profile.product}-{bound.slug}-".lower())
        or bool(domain and domain.lower() in hosts)
    )


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
        and bound.preview.state != "destroyed"
        and bound.profile.is_active,
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
