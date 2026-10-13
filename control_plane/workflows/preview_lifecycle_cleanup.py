from datetime import datetime
from pathlib import Path
from typing import Any

import click

from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
    PreviewLifecycleCleanupResult,
    build_preview_lifecycle_cleanup_id,
)
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_mutation_request import PreviewDestroyMutationRequest
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview,
    resolve_launchplane_preview_id,
)
from control_plane.drivers.verireel_postgres_transport import _preview_refresh_serialization
from control_plane.workflows.launchplane import find_preview_record
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewDestroyResult,
    _execute_generic_web_preview_destroy_unserialized,
    GenericWebPreviewDesiredStateRequest,
    discover_generic_web_preview_desired_state,
    resolve_generic_web_preview_profile,
    serialize_generic_web_preview_operation,
    generic_web_preview_anchor_repo,
    preview_pr_number_from_slug,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.drivers.generic_web_preview_extensions import (
    apply_generic_web_preview_driver_destroy,
)


def _planned_results(plan: PreviewLifecyclePlanRecord) -> tuple[PreviewLifecycleCleanupResult, ...]:
    return tuple(
        PreviewLifecycleCleanupResult(
            preview_slug=preview_slug,
            status="planned",
        )
        for preview_slug in plan.orphaned_slugs
    )


def _blocked_record(
    *,
    plan: PreviewLifecyclePlanRecord,
    requested_at: str,
    source: str,
    apply: bool,
    error_message: str,
) -> PreviewLifecycleCleanupRecord:
    return PreviewLifecycleCleanupRecord(
        cleanup_id=build_preview_lifecycle_cleanup_id(
            context_name=plan.context,
            requested_at=requested_at,
        ),
        product=plan.product,
        context=plan.context,
        plan_id=plan.plan_id,
        inventory_scan_id=plan.inventory_scan_id,
        requested_at=requested_at,
        source=source,
        apply=apply,
        status="blocked",
        planned_slugs=plan.orphaned_slugs,
        blocked_slugs=plan.orphaned_slugs,
        results=tuple(
            PreviewLifecycleCleanupResult(
                preview_slug=preview_slug,
                status="blocked",
                error_message=error_message,
            )
            for preview_slug in plan.orphaned_slugs
        ),
        error_message=error_message,
    )


def _orphan_preflight(
    *,
    plan: PreviewLifecyclePlanRecord,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    anchor_repo: str,
    anchor_pr_number: int,
    control_plane_root: Path,
    record_store: Any,
) -> str:
    try:
        current_profile = resolve_generic_web_preview_profile(
            record_store=record_store, product=plan.product
        )
    except (click.ClickException, FileNotFoundError):
        return "Current preview profile is unavailable; generate a fresh plan."
    if current_profile != profile or not current_profile.is_active:
        return "Preview profile changed or retired; generate a fresh plan."
    preview = find_preview_record(
        record_store=record_store,
        context_name=plan.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview is None:
        return "Cleanup requires a matching stored preview; generate a fresh plan."
    # A timestamp is the lifecycle revision, including generation-pointer changes.
    # Equal timestamps cannot prove which happened first and also refuse.
    try:
        planned_at = datetime.fromisoformat(plan.planned_at.replace("Z", "+00:00"))
        updated_at = datetime.fromisoformat(preview.updated_at.replace("Z", "+00:00"))
        created_at = datetime.fromisoformat(preview.created_at.replace("Z", "+00:00"))
        if any(value.tzinfo is None for value in (planned_at, updated_at, created_at)):
            return "Cleanup requires timezone-aware lifecycle timestamps; generate a fresh plan."
        if max(updated_at, created_at) >= planned_at:
            return "Preview lifecycle changed since planning; generate a fresh plan."
    except ValueError:
        return "Cleanup cannot establish lifecycle ordering; generate a fresh plan."
    desired = discover_generic_web_preview_desired_state(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=GenericWebPreviewDesiredStateRequest(product=plan.product, source=plan.source),
        discovered_at=utc_now_timestamp(),
        profile=profile,
    )
    if (
        desired.status != "pass"
        or desired.product != plan.product
        or desired.context != plan.context
        or desired.repository != profile.repository
        or desired.anchor_repo != anchor_repo
    ):
        return "Current desired previews are unknown; no cleanup was attempted."
    if any(
        item.anchor_pr_number == anchor_pr_number or item.preview_slug == preview_slug
        for item in desired.desired_previews
    ):
        return "The PR currently requires a preview; generate a fresh plan."
    current = find_preview_record(
        record_store=record_store,
        context_name=plan.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if current != preview:
        return "Preview lifecycle changed during validation; generate a fresh plan."
    return ""


def _build_generic_web_cleanup_record(
    *,
    plan: PreviewLifecyclePlanRecord,
    requested_at: str,
    source: str,
    cleanup_id: str,
    destroy_reason: str,
    control_plane_root: Path,
    record_store: Any,
    timeout_seconds: int,
    preview_slug_template: str,
    profile: LaunchplaneProductProfileRecord | None = None,
) -> PreviewLifecycleCleanupRecord:
    if profile is None:
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store, product=plan.product
            )
        except (click.ClickException, FileNotFoundError) as exc:
            return _blocked_record(
                plan=plan,
                requested_at=requested_at,
                source=source,
                apply=True,
                error_message=str(exc),
            )
    if (
        not profile.is_active
        or profile.product != plan.product
        or profile.preview.context != plan.context
        or not profile.preview.enabled
        or not profile.preview.context.strip()
    ):
        return _blocked_record(
            plan=plan,
            requested_at=requested_at,
            source=source,
            apply=True,
            error_message="Cleanup requires enabled previews and a plan matching its product/preview context; generate a fresh plan within the caller's existing authority.",
        )
    parsed_previews: list[tuple[str, int]] = []
    try:
        anchor_repo = generic_web_preview_anchor_repo(profile.repository)
    except click.ClickException as exc:
        return _blocked_record(
            plan=plan,
            requested_at=requested_at,
            source=source,
            apply=True,
            error_message=str(exc),
        )
    for preview_slug in plan.orphaned_slugs:
        anchor_pr_number = preview_pr_number_from_slug(
            preview_slug=preview_slug,
            slug_template=preview_slug_template,
        )
        if anchor_pr_number is None:
            return _blocked_record(
                plan=plan,
                requested_at=requested_at,
                source=source,
                apply=True,
                error_message=(
                    "Generic web preview cleanup could not derive a PR number from "
                    f"preview slug {preview_slug!r} using template {preview_slug_template!r}."
                ),
            )
        preview = find_preview_record(
            record_store=record_store,
            context_name=plan.context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        )
        if preview is None:
            return _blocked_record(
                plan=plan,
                requested_at=requested_at,
                source=source,
                apply=True,
                error_message=(
                    "Launchplane will not destroy preview provider state without a matching "
                    f"stored preview record for {plan.context}/{anchor_repo}/{preview_slug}."
                ),
            )
        parsed_previews.append((preview_slug, anchor_pr_number))

    results: list[PreviewLifecycleCleanupResult] = []
    destroyed_slugs: list[str] = []
    failed_slugs: list[str] = []
    blocked_slugs: list[str] = []
    for preview_slug, anchor_pr_number in parsed_previews:
        preview_id = resolve_launchplane_preview_id(
            context=plan.context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        )
        # Refresh holds this identity lock until its generation evidence is saved.
        # Take it before the provider locks to keep the same order as refresh.
        with _preview_refresh_serialization(
            record_store=record_store, preview_id=preview_id, dedicated=True
        ):
            with serialize_generic_web_preview_operation(
                record_store=record_store, profile=profile, preview_slug=preview_slug
            ):
                error = _orphan_preflight(
                    plan=plan,
                    profile=profile,
                    preview_slug=preview_slug,
                    anchor_repo=anchor_repo,
                    anchor_pr_number=anchor_pr_number,
                    control_plane_root=control_plane_root,
                    record_store=record_store,
                )
                if error:
                    blocked_slugs.append(preview_slug)
                    results.append(
                        PreviewLifecycleCleanupResult(
                            preview_slug=preview_slug,
                            anchor_repo=anchor_repo,
                            anchor_pr_number=anchor_pr_number,
                            status="blocked",
                            error_message=error,
                        )
                    )
                    continue
                destroy_request = GenericWebPreviewDestroyRequest(
                    product=plan.product,
                    preview_slug=preview_slug,
                    anchor_pr_number=anchor_pr_number,
                    destroy_reason=destroy_reason,
                    timeout_seconds=timeout_seconds,
                )
                extension_result = apply_generic_web_preview_driver_destroy(
                    control_plane_root=control_plane_root,
                    record_store=record_store,
                    request=destroy_request,
                    profile=profile,
                )
                destroy_result = (
                    GenericWebPreviewDestroyResult.model_validate(extension_result[1])
                    if extension_result is not None
                    else _execute_generic_web_preview_destroy_unserialized(
                        control_plane_root=control_plane_root,
                        record_store=record_store,
                        request=destroy_request,
                        profile=profile,
                    )
                )
                if destroy_result.destroy_status == "pass":
                    try:
                        apply_launchplane_destroy_preview(
                            record_store=record_store,
                            request=PreviewDestroyMutationRequest(
                                context=plan.context,
                                anchor_repo=anchor_repo,
                                anchor_pr_number=anchor_pr_number,
                                destroyed_at=destroy_result.destroy_finished_at,
                                destroy_reason=destroy_reason,
                            ),
                        )
                        destroyed_slugs.append(preview_slug)
                        results.append(
                            PreviewLifecycleCleanupResult(
                                preview_slug=preview_slug,
                                anchor_repo=anchor_repo,
                                anchor_pr_number=anchor_pr_number,
                                status="destroyed",
                                application_name=destroy_result.application_name,
                                application_id=destroy_result.application_id,
                            )
                        )
                    except click.ClickException as exc:
                        failed_slugs.append(preview_slug)
                        results.append(
                            PreviewLifecycleCleanupResult(
                                preview_slug=preview_slug,
                                anchor_repo=anchor_repo,
                                anchor_pr_number=anchor_pr_number,
                                status="failed",
                                application_name=destroy_result.application_name,
                                application_id=destroy_result.application_id,
                                error_message=str(exc),
                            )
                        )
                    continue
                failed_slugs.append(preview_slug)
                results.append(
                    PreviewLifecycleCleanupResult(
                        preview_slug=preview_slug,
                        anchor_repo=anchor_repo,
                        anchor_pr_number=anchor_pr_number,
                        status="failed",
                        application_name=destroy_result.application_name,
                        application_id=destroy_result.application_id,
                        error_message=destroy_result.error_message,
                    )
                )
    return PreviewLifecycleCleanupRecord(
        cleanup_id=cleanup_id,
        product=plan.product,
        context=plan.context,
        plan_id=plan.plan_id,
        inventory_scan_id=plan.inventory_scan_id,
        requested_at=requested_at,
        source=source,
        apply=True,
        status="fail" if failed_slugs else "blocked" if blocked_slugs else "pass",
        planned_slugs=plan.orphaned_slugs,
        destroyed_slugs=tuple(destroyed_slugs),
        failed_slugs=tuple(failed_slugs),
        blocked_slugs=tuple(blocked_slugs),
        results=tuple(results),
        error_message=(
            "One or more preview cleanup actions failed."
            if failed_slugs
            else "Preview cleanup intent changed; generate a fresh plan."
            if blocked_slugs
            else ""
        ),
    )


def build_preview_lifecycle_cleanup_record(
    *,
    plan: PreviewLifecyclePlanRecord,
    requested_at: str,
    source: str,
    apply: bool,
    destroy_reason: str,
    control_plane_root: Path,
    record_store: Any,
    timeout_seconds: int,
    driver_id: str = "",
    preview_slug_template: str = "pr-{number}",
    profile: LaunchplaneProductProfileRecord | None = None,
) -> PreviewLifecycleCleanupRecord:
    cleanup_id = build_preview_lifecycle_cleanup_id(
        context_name=plan.context,
        requested_at=requested_at,
    )
    if plan.status != "pass":
        return _blocked_record(
            plan=plan,
            requested_at=requested_at,
            source=source,
            apply=apply,
            error_message=f"Preview lifecycle plan status is {plan.status}; cleanup requires pass.",
        )
    if not apply:
        return PreviewLifecycleCleanupRecord(
            cleanup_id=cleanup_id,
            product=plan.product,
            context=plan.context,
            plan_id=plan.plan_id,
            inventory_scan_id=plan.inventory_scan_id,
            requested_at=requested_at,
            source=source,
            status="report_only",
            planned_slugs=plan.orphaned_slugs,
            results=_planned_results(plan),
        )
    if driver_id.strip() != "generic-web":
        return _blocked_record(
            plan=plan,
            requested_at=requested_at,
            source=source,
            apply=True,
            error_message="Preview cleanup requires the shared generic-web driver.",
        )
    return _build_generic_web_cleanup_record(
        plan=plan,
        requested_at=requested_at,
        source=source,
        cleanup_id=cleanup_id,
        destroy_reason=destroy_reason,
        control_plane_root=control_plane_root,
        record_store=record_store,
        timeout_seconds=timeout_seconds,
        preview_slug_template=preview_slug_template,
        profile=profile,
    )
