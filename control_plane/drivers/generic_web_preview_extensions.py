from __future__ import annotations

from pathlib import Path

import click

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.generic_web_preview_dispatch import (
    _generic_web_preview_anchor_head_sha,
    _generic_web_preview_anchor_pr_number,
    _generic_web_preview_anchor_pr_url,
    _generic_web_preview_anchor_repo,
)
from control_plane.verireel_read_http import (
    VeriReelPreviewDestroyEnvelope,
    VeriReelPreviewRefreshEnvelope,
    apply_verireel_preview_destroy_result,
    apply_verireel_preview_refresh_result,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewDestroyResult,
    GenericWebPreviewReadinessCheck,
    GenericWebPreviewReadinessResult,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
    GenericWebPreviewSmokeCheck,
    GenericWebPreviewSmokeResult,
    GenericWebPreviewTransportSummary,
    preview_pr_number_from_slug,
    resolve_generic_web_preview_slug,
)
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyRequest,
    VeriReelPreviewRefreshRequest,
)


def _successful_driver_refresh_evidence(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: GenericWebPreviewRefreshRequest,
    result: dict[str, object],
) -> tuple[GenericWebPreviewReadinessResult, GenericWebPreviewSmokeResult] | None:
    if result.get("refresh_status") != "pass":
        return None
    template_instance = profile.preview.template_instance.strip()
    template_lane = next(
        (lane for lane in profile.lanes if lane.instance == template_instance),
        None,
    )
    checked_at = str(result.get("refresh_finished_at") or result.get("refresh_started_at") or "")
    readiness = GenericWebPreviewReadinessResult(
        readiness_status="pass",
        checked_at=checked_at,
        product=profile.product,
        context=profile.preview.context,
        template_context=template_lane.context if template_lane is not None else "",
        template_instance=template_instance,
        source=request.source,
        missing_template_env_keys=(),
        missing_provider_fields=(),
        transport=GenericWebPreviewTransportSummary(
            data_transport_mode=profile.preview.data_transport_mode,
            copied_env_keys=profile.preview.copied_env_keys,
            omitted_env_keys=profile.preview.omitted_env_keys,
            override_env_keys=tuple(profile.preview.override_env),
            preview_url_env_keys=profile.preview.preview_url_env_keys,
            preview_domain_env_keys=profile.preview.preview_domain_env_keys,
            migration_command_configured=bool(profile.preview.migration_command.strip()),
            seed_command_configured=bool(profile.preview.seed_command.strip()),
        ),
        checks=(
            GenericWebPreviewReadinessCheck(
                check_id="driver_refresh",
                status="pass",
                message="Driver-owned preview provisioning prerequisites passed.",
            ),
        ),
    )
    smoke = GenericWebPreviewSmokeResult(
        smoke_status="pass",
        checked_at=checked_at,
        checks=(
            GenericWebPreviewSmokeCheck(
                check_id="driver_preview_health",
                status="pass",
                message="Driver-owned preview health check passed before product verification.",
            ),
        ),
    )
    return readiness, smoke


def apply_generic_web_preview_driver_refresh(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewRefreshRequest,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]] | None:
    if profile.preview.data_transport_mode != "driver":
        return None
    if profile.driver_id != "verireel":
        raise click.ClickException(
            f"Driver {profile.driver_id!r} does not register a generic-web preview refresh extension."
        )

    anchor_pr_number = _generic_web_preview_anchor_pr_number(
        request=request,
        profile=profile,
    )
    preview_slug = resolve_generic_web_preview_slug(
        profile=profile,
        preview_slug=request.preview_slug,
        anchor_pr_number=anchor_pr_number,
        label="Generic web preview refresh",
    )
    verireel_refresh_request = VeriReelPreviewRefreshRequest(
        context=profile.preview.context,
        anchor_repo=_generic_web_preview_anchor_repo(profile),
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=_generic_web_preview_anchor_pr_url(
            request=request,
            profile=profile,
            anchor_pr_number=anchor_pr_number,
        ),
        anchor_head_sha=_generic_web_preview_anchor_head_sha(request),
        preview_slug=preview_slug,
        preview_url=request.preview_url,
        image_reference=request.image_reference,
        timeout_seconds=request.timeout_seconds,
    )
    records, result = apply_verireel_preview_refresh_result(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=VeriReelPreviewRefreshEnvelope(
            product=profile.product,
            refresh=verireel_refresh_request,
        ),
    )
    driver_evidence = _successful_driver_refresh_evidence(
        profile=profile,
        request=request,
        result=result,
    )
    generic_result = GenericWebPreviewRefreshResult.model_validate(
        {
            **result,
            "product": profile.product,
            "context": profile.preview.context,
            "preview_slug": preview_slug,
            "readiness": driver_evidence[0] if driver_evidence is not None else None,
            "smoke": driver_evidence[1] if driver_evidence is not None else None,
        }
    )
    return records, generic_result.model_dump(mode="json")


def apply_generic_web_preview_driver_destroy(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebPreviewDestroyRequest,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]] | None:
    if profile.preview.data_transport_mode != "driver":
        return None
    if profile.driver_id != "verireel":
        raise click.ClickException(
            f"Driver {profile.driver_id!r} does not register a generic-web preview destroy extension."
        )

    anchor_pr_number = request.anchor_pr_number or preview_pr_number_from_slug(
        preview_slug=request.preview_slug,
        slug_template=profile.preview.slug_template,
    )
    if anchor_pr_number is None:
        raise click.ClickException(
            "Generic web preview destroy requires anchor_pr_number when preview_slug does not match the profile slug_template."
        )
    preview_slug = resolve_generic_web_preview_slug(
        profile=profile,
        preview_slug=request.preview_slug,
        anchor_pr_number=anchor_pr_number,
        label="Generic web preview destroy",
    )
    records, result = apply_verireel_preview_destroy_result(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=VeriReelPreviewDestroyEnvelope(
            product=profile.product,
            destroy=VeriReelPreviewDestroyRequest(
                context=profile.preview.context,
                anchor_repo=_generic_web_preview_anchor_repo(profile),
                anchor_pr_number=anchor_pr_number,
                preview_slug=preview_slug,
                destroy_reason=request.destroy_reason,
                timeout_seconds=request.timeout_seconds,
            ),
        ),
    )
    generic_result = GenericWebPreviewDestroyResult.model_validate(
        {
            "destroy_status": result["destroy_status"],
            "destroy_started_at": result["destroy_started_at"],
            "destroy_finished_at": result["destroy_finished_at"],
            "product": profile.product,
            "context": profile.preview.context,
            "preview_slug": preview_slug,
            "application_name": result["application_name"],
            "application_id": result["application_id"],
            "error_message": result.get("error_message", ""),
        }
    )
    return records, generic_result.model_dump(mode="json")
