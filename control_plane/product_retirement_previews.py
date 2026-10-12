"""Owned-preview stages of the existing product retirement executor."""

from pathlib import Path
from typing import cast

from control_plane.contracts.product_retirement import (
    ProductRetirementPreviewPlan,
    canonical_sha256,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.legacy_preview_reconciliation import (
    LegacyPreviewAuthority,
    LegacyPreviewReconciliationRequest,
    bind_legacy_preview,
    observe_legacy_preview,
)
from control_plane.storage.postgres import PostgresRecordStore


def owned_previews(
    store: PostgresRecordStore, profile: LaunchplaneProductProfileRecord
) -> tuple[LegacyPreviewAuthority, ...]:
    if not profile.preview.context:
        return ()
    previews = store.list_preview_records(context_name=profile.preview.context)
    owned = []
    for preview in sorted(previews, key=lambda p: p.preview_id):
        if preview.state == "destroyed":
            continue
        owned.append(
            bind_legacy_preview(
                store,
                LegacyPreviewReconciliationRequest(
                    product=profile.product,
                    preview_id=preview.preview_id,
                    reason="Product retirement",
                ),
                allow_tracked=True,
            )
        )
    return tuple(owned)


def preview_authority(
    store: PostgresRecordStore, bound: LegacyPreviewAuthority
) -> dict[str, object]:
    scope = (bound.preview.context, bound.slug)
    targets: list[dict[str, object]] = []
    for kind, records in (
        ("provider_target", store.list_provider_target_records()),
        ("dokploy_target_id", store.list_dokploy_target_id_records()),
        ("dokploy_target", store.list_dokploy_target_records()),
    ):
        targets.extend(
            {"kind": kind, "record": t.model_dump(mode="json")}
            for t in records
            if (t.context, t.instance) == scope
        )
    deployments = []
    for generation in bound.generations:
        if generation.runtime_identity:
            record_id = generation.runtime_identity.deployment_record_id
            try:
                deployments.append(store.read_deployment_record(record_id).model_dump(mode="json"))
            except FileNotFoundError:
                deployments.append({"missing_deployment": record_id})
    return {
        "preview": bound.preview.model_dump(mode="json"),
        "generations": [g.model_dump(mode="json") for g in bound.generations],
        "deployments": deployments,
        "targets": targets,
    }


def validate_product_scope(
    store: PostgresRecordStore, profile: LaunchplaneProductProfileRecord, instance: str
) -> None:
    if (
        len(profile.lanes) != 1
        or profile.lanes[0].instance != instance
        or instance != "testing"
        or profile.production_use == "live"
    ):
        raise ValueError("Integrated retirement requires one non-live generic-web testing lane.")
    contexts = {profile.lanes[0].context, profile.preview.context, *profile.historical_contexts} - {
        ""
    }
    if any(
        other.product != profile.product
        and contexts.intersection(
            {
                other.preview.context,
                *other.historical_contexts,
                *(lane.context for lane in other.lanes),
            }
        )
        for other in store.list_product_profile_records()
    ):
        raise ValueError("Product retirement contexts have shared ownership.")
    for context in profile.historical_contexts:
        if context != profile.preview.context and any(
            preview.state != "destroyed"
            for preview in store.list_preview_records(context_name=context)
        ):
            raise ValueError(
                "Active historical previews have unqualified ownership; product retirement is blocked."
            )
    scopes = {(profile.lanes[0].context, instance)} | {
        (p.preview.context, p.slug) for p in owned_previews(store, profile)
    }
    for records in (
        store.list_provider_target_records(),
        store.list_dokploy_target_id_records(),
        store.list_dokploy_target_records(),
    ):
        if any(t.context in contexts and (t.context, t.instance) not in scopes for t in records):
            raise ValueError("Product has unplanned target authority.")
    from control_plane.contracts.deploy_target import ProviderTargetRecord
    from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

    physical: tuple[ProviderTargetRecord | DokployTargetIdRecord, ...] = (
        *store.list_provider_target_records(),
        *store.list_dokploy_target_id_records(),
    )
    for target in physical:
        if (target.context, target.instance) in scopes and any(
            other.target_id == target.target_id
            and (other.context, other.instance) != (target.context, target.instance)
            for other in physical
        ):
            raise ValueError("Product provider target has shared ownership.")


def plan_product_previews(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    root: Path,
    observed_at: str,
) -> tuple[ProductRetirementPreviewPlan, ...]:
    from control_plane.product_retirement import observe_tracked_dokploy_application

    peers = owned_previews(store, profile)
    plans = []
    for bound in peers:
        observation = observe_legacy_preview(
            control_plane_root=root, bound=bound, retirement_peers=peers
        )
        ids = cast(tuple[str, ...], observation["candidate_target_ids"])
        if len(ids) > 1:
            raise ValueError("Preview has multiple provider candidates.")
        provider = (
            observe_tracked_dokploy_application(
                control_plane_root=root, target_id=ids[0], observed_at=observed_at
            )
            if ids
            else None
        )
        if provider is not None and (provider.state != "present" or not provider.retirable):
            raise ValueError("Preview application is changing or not retirable.")
        authority = preview_authority(store, bound)
        targets = cast(list[dict[str, object]], authority["targets"])
        plans.append(
            ProductRetirementPreviewPlan(
                preview=bound.preview,
                slug=bound.slug,
                application_name=provider.application_name if provider else bound.application_name,
                ownership_proof="provider_absent"
                if provider is None
                else "recorded_target"
                if provider.target_id in bound.preview_target_ids
                else "product_source",
                authority_sha256=canonical_sha256(authority),
                provider_observation=provider,
                target_authority=tuple(targets),
                affected_record_refs=(
                    f"preview:{bound.preview.preview_id}",
                    *(f"{t['kind']}:{bound.preview.context}:{bound.slug}" for t in targets),
                ),
                routes=tuple(
                    sorted(
                        {bound.preview.canonical_url, *(provider.domain_hosts if provider else ())}
                    )
                ),
            )
        )
    # Planning is evidence only; reject a moving local snapshot after provider reads.
    if owned_previews(store, profile) != peers:
        raise ValueError("Preview authority changed during planning.")
    return tuple(plans)


def preview_checkpoint_id(operation_key: str, preview_id: str) -> str:
    return f"product-retirement-preview-{canonical_sha256((operation_key, preview_id))}"


def retire_product_previews(adapter: object, lease: object, operation_key: str) -> None:
    from control_plane.product_retirement import (
        DokployProductRetirementAdapter,
        _observation_allows_reconciliation,
        observe_tracked_dokploy_application,
    )
    from control_plane.provider_operations import ProviderOperationLease
    from control_plane.dokploy import source as dokploy_source
    from control_plane.workflows.preview_resource_destroy import destroy_dokploy_preview_resource

    runner = cast(DokployProductRetirementAdapter, adapter)
    active_lease = cast(ProviderOperationLease, lease)
    store = cast(PostgresRecordStore, runner._record_store)
    plan = runner._plan
    assert plan.profile_record is not None
    closed = set()
    for preview_plan in plan.preview_plans:
        try:
            checkpoint = store.read_product_retirement_record(
                preview_checkpoint_id(operation_key, preview_plan.preview.preview_id)
            )
        except FileNotFoundError:
            checkpoint = None
        if checkpoint is not None:
            if (
                checkpoint.plan_sha256 != plan.plan_sha256
                or checkpoint.identity != runner._identity
                or checkpoint.idempotency_key != runner._idempotency_key
                or checkpoint.mutation_evidence.provider_operation_key != operation_key
            ):
                raise ValueError("Preview retirement checkpoint does not match this operation.")
            expected_closed_ids = checkpoint.mutation_evidence.closed_preview_ids
            if (
                expected_closed_ids != (preview_plan.preview.preview_id,)
                or store.read_preview_record(preview_plan.preview.preview_id).state != "destroyed"
            ):
                raise ValueError("Preview retirement checkpoint is incomplete.")
            closed.add(preview_plan.preview.preview_id)
    peers = owned_previews(store, plan.profile_record)
    if {p.preview.preview_id for p in peers} != {
        p.preview.preview_id for p in plan.preview_plans
    } - closed:
        raise ValueError("Product preview set changed after review.")
    # Validate every preview before the first deletion. Existing completed siblings
    # stay identifiable during absence scans by their original preview identity.
    all_peers = tuple(
        bind_legacy_preview(
            store,
            LegacyPreviewReconciliationRequest(
                product=plan.product, preview_id=p.preview.preview_id, reason=plan.reason
            ),
            allow_tracked=True,
        )
        for p in plan.preview_plans
    )
    for preview_plan in plan.preview_plans:
        bound = next(
            p for p in all_peers if p.preview.preview_id == preview_plan.preview.preview_id
        )
        if (
            preview_plan.preview.preview_id not in closed
            and canonical_sha256(preview_authority(store, bound)) != preview_plan.authority_sha256
        ):
            raise ValueError("Reviewed preview authority changed.")
        if preview_plan.preview.preview_id in closed:
            from control_plane.workflows.launchplane import apply_preview_destroyed_transition

            checkpoint = store.read_product_retirement_record(
                preview_checkpoint_id(operation_key, preview_plan.preview.preview_id)
            )
            expected = apply_preview_destroyed_transition(
                preview=preview_plan.preview,
                destroyed_at=checkpoint.recorded_at,
                destroy_reason=plan.reason,
            )
            payload = preview_authority(store, bound)
            if bound.preview != expected or payload["targets"]:
                raise ValueError("Completed preview evidence changed.")
            payload["preview"] = preview_plan.preview.model_dump(mode="json")
            payload["targets"] = list(preview_plan.target_authority)
            if canonical_sha256(payload) != preview_plan.authority_sha256:
                raise ValueError("Completed preview history changed.")
        observed = observe_legacy_preview(
            control_plane_root=runner._control_plane_root, bound=bound, retirement_peers=all_peers
        )
        ids = cast(tuple[str, ...], observed["candidate_target_ids"])
        planned = preview_plan.provider_observation
        if ids and (planned is None or ids != (planned.target_id,)):
            raise ValueError("Preview provider identity changed after review.")
        if preview_plan.preview.preview_id in closed:
            if ids:
                raise ValueError("Completed preview provider has reappeared.")
            continue
        if ids:
            current = observe_tracked_dokploy_application(
                control_plane_root=runner._control_plane_root,
                target_id=ids[0],
                observed_at=runner._requested_at,
            )
            if (
                planned is None
                or not current.retirable
                or not _observation_allows_reconciliation(planned=planned, current=current)
            ):
                raise ValueError("Preview provider configuration changed after review.")
        elif planned is not None:
            # Complete enumeration proves an interrupted deletion is now absent.
            continue
    runner._ensure_retiring_profile()
    runner._checkpoint(active_lease, "profile_retiring")
    for preview_plan in plan.preview_plans:
        if preview_plan.preview.preview_id in closed:
            continue
        bound = bind_legacy_preview(
            store,
            LegacyPreviewReconciliationRequest(
                product=plan.product, preview_id=preview_plan.preview.preview_id, reason=plan.reason
            ),
            allow_tracked=True,
        )
        observed = observe_legacy_preview(
            control_plane_root=runner._control_plane_root, bound=bound, retirement_peers=all_peers
        )
        ids = cast(tuple[str, ...], observed["candidate_target_ids"])
        if ids:
            planned = preview_plan.provider_observation
            if planned is None or ids != (planned.target_id,):
                raise ValueError("Preview candidate changed before deletion.")
            host, token = dokploy_source.read_dokploy_config(
                control_plane_root=runner._control_plane_root
            )

            def before_delete(phase: str) -> None:
                fresh = observe_tracked_dokploy_application(
                    control_plane_root=runner._control_plane_root,
                    target_id=planned.target_id,
                    observed_at=runner._requested_at,
                )
                if fresh.state != "absent" and not _observation_allows_reconciliation(
                    planned=planned, current=fresh
                ):
                    raise ValueError("Preview provider changed before a destructive step.")
                runner._checkpoint(
                    active_lease, f"preview:{preview_plan.preview.preview_id}:{phase}"
                )

            result = destroy_dokploy_preview_resource(
                host=host,
                token=token,
                resource_type="application",
                resource_id=planned.target_id,
                continue_after_domain_cleanup_error=False,
                missing_resource_is_clean=True,
                before_provider_mutation=before_delete,
                propagate_domain_lookup_error=True,
            )
            runner._provider_effect_performed = True
            if result.status != "pass":
                raise ValueError("Preview teardown outcome requires reconciliation.")
        absence = observe_legacy_preview(
            control_plane_root=runner._control_plane_root, bound=bound, retirement_peers=all_peers
        )
        if not absence["provider_absence_verified"]:
            raise ValueError("Preview provider absence could not be verified.")
        active_lease.assert_current()
        checkpoint = runner._build_terminal_record(
            outcome="started", provider_operation_key=operation_key, provider_absence_verified=False
        ).model_copy(
            update={
                "record_id": preview_checkpoint_id(operation_key, preview_plan.preview.preview_id),
                "mutation_evidence": runner._build_terminal_record(
                    outcome="started",
                    provider_operation_key=operation_key,
                    provider_absence_verified=False,
                ).mutation_evidence.model_copy(
                    update={"closed_preview_ids": (preview_plan.preview.preview_id,)}
                ),
            }
        )
        store.commit_product_retirement_preview(
            plan=preview_plan, checkpoint=checkpoint, destroyed_at=runner._requested_at
        )
        closed.add(preview_plan.preview.preview_id)
    runner._closed_preview_ids = sorted(closed)


def verify_retired_previews(adapter: object, operation_key: str) -> None:
    from control_plane.product_retirement import DokployProductRetirementAdapter
    from control_plane.workflows.launchplane import apply_preview_destroyed_transition

    runner = cast(DokployProductRetirementAdapter, adapter)
    store = cast(PostgresRecordStore, runner._record_store)
    plans = runner._plan.preview_plans
    peers = tuple(
        bind_legacy_preview(
            store,
            LegacyPreviewReconciliationRequest(
                product=runner._plan.product,
                preview_id=plan.preview.preview_id,
                reason=runner._plan.reason,
            ),
            allow_tracked=True,
        )
        for plan in plans
    )
    for plan, bound in zip(plans, peers, strict=True):
        checkpoint = store.read_product_retirement_record(
            preview_checkpoint_id(operation_key, plan.preview.preview_id)
        )
        expected = apply_preview_destroyed_transition(
            preview=plan.preview,
            destroyed_at=checkpoint.recorded_at,
            destroy_reason=runner._plan.reason,
        )
        if (
            bound.preview != expected
            or checkpoint.plan_sha256 != runner._plan.plan_sha256
            or checkpoint.identity != runner._identity
            or checkpoint.mutation_evidence.provider_operation_key != operation_key
            or checkpoint.mutation_evidence.closed_preview_ids != (plan.preview.preview_id,)
        ):
            raise ValueError("Completed preview evidence changed.")
        payload = preview_authority(store, bound)
        if payload["targets"]:
            raise ValueError("Completed preview target authority reappeared.")
        payload["preview"] = plan.preview.model_dump(mode="json")
        payload["targets"] = list(plan.target_authority)
        if canonical_sha256(payload) != plan.authority_sha256:
            raise ValueError("Completed preview history changed.")
        observed = observe_legacy_preview(
            control_plane_root=runner._control_plane_root, bound=bound, retirement_peers=peers
        )
        if not observed["provider_absence_verified"]:
            raise ValueError("Completed preview provider absence is unproved.")
