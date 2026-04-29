import re
from pathlib import Path
from typing import Any

import click

from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
    PreviewLifecycleCleanupResult,
    build_preview_lifecycle_cleanup_id,
)
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_mutation_request import PreviewDestroyMutationRequest
from control_plane.launchplane_mutations import apply_launchplane_destroy_preview
from control_plane.workflows.launchplane import find_preview_record
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyRequest,
    execute_verireel_preview_destroy,
)

_VERIREEL_PREVIEW_SLUG_PATTERN = re.compile(r"^pr-(?P<number>[1-9][0-9]*)$")


def _verireel_anchor_pr_number(preview_slug: str) -> int:
    match = _VERIREEL_PREVIEW_SLUG_PATTERN.fullmatch(preview_slug.strip())
    if match is None:
        raise ValueError(f"VeriReel preview cleanup only supports PR preview slugs: {preview_slug}")
    return int(match.group("number"))


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
            apply=False,
            status="report_only",
            planned_slugs=plan.orphaned_slugs,
            results=_planned_results(plan),
        )
    if plan.product != "verireel" or plan.context != "verireel-testing":
        return _blocked_record(
            plan=plan,
            requested_at=requested_at,
            source=source,
            apply=True,
            error_message="Preview lifecycle cleanup execution is currently implemented for verireel-testing only.",
        )

    parsed_previews: list[tuple[str, int]] = []
    for preview_slug in plan.orphaned_slugs:
        try:
            anchor_pr_number = _verireel_anchor_pr_number(preview_slug)
        except ValueError as exc:
            return _blocked_record(
                plan=plan,
                requested_at=requested_at,
                source=source,
                apply=True,
                error_message=str(exc),
            )
        preview = find_preview_record(
            record_store=record_store,
            context_name=plan.context,
            anchor_repo="verireel",
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
                    f"stored preview record for {plan.context}/verireel/{preview_slug}."
                ),
            )
        parsed_previews.append((preview_slug, anchor_pr_number))

    results: list[PreviewLifecycleCleanupResult] = []
    destroyed_slugs: list[str] = []
    failed_slugs: list[str] = []
    for preview_slug, anchor_pr_number in parsed_previews:
        destroy_result = execute_verireel_preview_destroy(
            control_plane_root=control_plane_root,
            request=VeriReelPreviewDestroyRequest(
                context=plan.context,
                anchor_repo="verireel",
                anchor_pr_number=anchor_pr_number,
                preview_slug=preview_slug,
                destroy_reason=destroy_reason,
                timeout_seconds=timeout_seconds,
            ),
        )
        if destroy_result.destroy_status == "pass":
            try:
                apply_launchplane_destroy_preview(
                    record_store=record_store,
                    request=PreviewDestroyMutationRequest(
                        context=plan.context,
                        anchor_repo="verireel",
                        anchor_pr_number=anchor_pr_number,
                        destroyed_at=destroy_result.destroy_finished_at,
                        destroy_reason=destroy_reason,
                    ),
                )
                destroyed_slugs.append(preview_slug)
                results.append(
                    PreviewLifecycleCleanupResult(
                        preview_slug=preview_slug,
                        anchor_repo="verireel",
                        anchor_pr_number=anchor_pr_number,
                        status="destroyed",
                        application_name=destroy_result.application_name,
                        application_id=destroy_result.application_id,
                        preview_url=destroy_result.preview_url,
                    )
                )
            except click.ClickException as exc:
                failed_slugs.append(preview_slug)
                results.append(
                    PreviewLifecycleCleanupResult(
                        preview_slug=preview_slug,
                        anchor_repo="verireel",
                        anchor_pr_number=anchor_pr_number,
                        status="failed",
                        application_name=destroy_result.application_name,
                        application_id=destroy_result.application_id,
                        preview_url=destroy_result.preview_url,
                        error_message=str(exc),
                    )
                )
            continue
        failed_slugs.append(preview_slug)
        results.append(
            PreviewLifecycleCleanupResult(
                preview_slug=preview_slug,
                anchor_repo="verireel",
                anchor_pr_number=anchor_pr_number,
                status="failed",
                application_name=destroy_result.application_name,
                application_id=destroy_result.application_id,
                preview_url=destroy_result.preview_url,
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
        status="pass" if not failed_slugs else "fail",
        planned_slugs=plan.orphaned_slugs,
        destroyed_slugs=tuple(destroyed_slugs),
        failed_slugs=tuple(failed_slugs),
        results=tuple(results),
        error_message="" if not failed_slugs else "One or more preview cleanup actions failed.",
    )
