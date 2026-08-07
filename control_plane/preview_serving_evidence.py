from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_sha256_digest,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity


PreviewServingEvidenceCode = Literal[
    "preview_inactive",
    "serving_generation_missing",
    "serving_generation_mismatch",
    "generation_not_ready",
    "generation_verification_failed",
    "preview_identity_mismatch",
    "artifact_identity_missing",
    "runtime_identity_missing",
    "runtime_identity_mismatch",
]


class PreviewServingEvidenceError(ValueError):
    def __init__(self, *, code: PreviewServingEvidenceCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class VerifiedServingPreview:
    product: str
    context: str
    anchor_repo: str
    anchor_pr_number: int
    anchor_pr_url: str
    head_sha: str
    preview_id: str
    serving_generation_id: str
    artifact_id: str
    artifact_image_digest: str
    manifest_fingerprint: str
    preview_url: str
    runtime_identity: RuntimeIdentity


def immutable_image_digest(image_reference: str, *, label: str) -> str:
    normalized = image_reference.strip()
    _, separator, digest = normalized.rpartition("@")
    if not separator:
        raise ValueError(f"{label} requires an immutable image reference")
    return normalize_artifact_sha256_digest(digest, label=f"{label} image digest")


def verify_serving_preview(
    *,
    product: str,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
    require_runtime_generation_id: bool = False,
) -> VerifiedServingPreview:
    normalized_product = product.strip().lower()
    if preview.state != "active":
        raise PreviewServingEvidenceError(
            code="preview_inactive",
            message="The preview is not active.",
        )
    if not preview.serving_generation_id:
        raise PreviewServingEvidenceError(
            code="serving_generation_missing",
            message="The preview does not have a serving generation.",
        )
    if (
        generation.preview_id != preview.preview_id
        or generation.generation_id != preview.serving_generation_id
    ):
        raise PreviewServingEvidenceError(
            code="serving_generation_mismatch",
            message="The supplied generation is not the preview's serving generation.",
        )
    if generation.state != "ready":
        raise PreviewServingEvidenceError(
            code="generation_not_ready",
            message="The serving preview generation is not ready.",
        )
    if any(
        status != "pass"
        for status in (
            generation.deploy_status,
            generation.verify_status,
            generation.overall_health_status,
        )
    ):
        raise PreviewServingEvidenceError(
            code="generation_verification_failed",
            message="The serving preview generation has not passed deployment and verification.",
        )
    if (
        generation.anchor_summary.repo.lower() != preview.anchor_repo.lower()
        or generation.anchor_summary.pr_number != preview.anchor_pr_number
        or generation.anchor_summary.pr_url != preview.anchor_pr_url
        or preview.latest_manifest_fingerprint != generation.resolved_manifest_fingerprint
    ):
        raise PreviewServingEvidenceError(
            code="preview_identity_mismatch",
            message="The preview and serving generation identity do not match.",
        )
    if not generation.artifact_id:
        raise PreviewServingEvidenceError(
            code="artifact_identity_missing",
            message="The serving preview generation does not have immutable artifact identity.",
        )
    if generation.runtime_identity is None:
        raise PreviewServingEvidenceError(
            code="runtime_identity_missing",
            message="The serving preview generation does not have verified runtime identity.",
        )
    runtime_identity = generation.runtime_identity
    expected_runtime_values = {
        "product": normalized_product,
        "context": preview.context.strip().lower(),
        "artifact_id": generation.artifact_id,
        "source_git_ref": generation.anchor_summary.head_sha.strip().lower(),
    }
    mismatches = [
        field_name
        for field_name, expected_value in expected_runtime_values.items()
        if str(getattr(runtime_identity, field_name)).strip().lower() != expected_value.lower()
    ]
    if runtime_identity.environment_kind.strip().lower() != "preview":
        mismatches.append("environment_kind")
    if runtime_identity.preview_id.strip() != preview.preview_id:
        mismatches.append("preview_id")
    runtime_generation_id = runtime_identity.preview_generation_id.strip()
    if (require_runtime_generation_id and runtime_generation_id != generation.generation_id) or (
        not require_runtime_generation_id
        and runtime_generation_id
        and runtime_generation_id != generation.generation_id
    ):
        mismatches.append("preview_generation_id")
    try:
        artifact_image_digest = immutable_image_digest(
            runtime_identity.image_reference,
            label="preview serving runtime identity",
        )
    except ValueError as error:
        raise PreviewServingEvidenceError(
            code="runtime_identity_mismatch",
            message="The serving preview runtime identity is incomplete or inconsistent.",
        ) from error
    if mismatches:
        raise PreviewServingEvidenceError(
            code="runtime_identity_mismatch",
            message="The serving preview runtime identity is incomplete or inconsistent.",
        )
    return VerifiedServingPreview(
        product=normalized_product,
        context=preview.context,
        anchor_repo=preview.anchor_repo,
        anchor_pr_number=preview.anchor_pr_number,
        anchor_pr_url=preview.anchor_pr_url,
        head_sha=generation.anchor_summary.head_sha,
        preview_id=preview.preview_id,
        serving_generation_id=generation.generation_id,
        artifact_id=generation.artifact_id,
        artifact_image_digest=artifact_image_digest,
        manifest_fingerprint=generation.resolved_manifest_fingerprint,
        preview_url=preview.canonical_url,
        runtime_identity=runtime_identity,
    )
