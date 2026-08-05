from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_onboarding_manifest import (
    ProductOnboardingLaneManifest,
    ProductOnboardingManifest,
    ProductOnboardingPreviewManifest,
    ProductOnboardingTargetManifest,
)
from control_plane.contracts.product_profile_record import (
    PRODUCT_PREVIEW_DEFAULT_ENABLE_LABEL,
)


_PRODUCT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


class GenericWebOnboardingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    display_name: str
    repository: str
    repository_id: str
    repository_owner_id: str
    default_branch: str = "main"
    image_repository: str
    runtime_port: int = Field(ge=1, le=65535)
    health_path: str
    testing_context: str = ""
    target_name: str = ""
    project_name: str = ""
    environment_name: str = "testing"
    server_id: str = ""
    preview_context: str = ""
    preview_label: str = PRODUCT_PREVIEW_DEFAULT_ENABLE_LABEL
    preview_app_name_prefix: str = ""
    domain_certificate_type: Literal["none", "letsencrypt"] = "none"

    @model_validator(mode="after")
    def _validate_intent(self) -> "GenericWebOnboardingIntent":
        self.product = self.product.strip().lower()
        self.display_name = self.display_name.strip()
        self.repository = self.repository.strip()
        self.repository_id = self.repository_id.strip()
        self.repository_owner_id = self.repository_owner_id.strip()
        self.default_branch = self.default_branch.strip()
        self.image_repository = self.image_repository.strip()
        self.health_path = self.health_path.strip()
        self.testing_context = self.testing_context.strip() or f"{self.product}-testing"
        self.target_name = self.target_name.strip() or f"{self.product}-testing"
        self.project_name = self.project_name.strip() or self.product
        self.environment_name = self.environment_name.strip() or "testing"
        self.server_id = self.server_id.strip()
        self.preview_context = self.preview_context.strip() or f"{self.product}-preview"
        self.preview_label = self.preview_label.strip() or PRODUCT_PREVIEW_DEFAULT_ENABLE_LABEL
        self.preview_app_name_prefix = (
            self.preview_app_name_prefix.strip() or f"{self.product}-preview"
        )
        if _PRODUCT_PATTERN.fullmatch(self.product) is None:
            raise ValueError(
                "generic-web onboarding product must use lowercase letters, numbers, and hyphens"
            )
        if not self.display_name:
            raise ValueError("generic-web onboarding requires display_name")
        if _REPOSITORY_PATTERN.fullmatch(self.repository) is None:
            raise ValueError("generic-web onboarding repository must use owner/name form")
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
        ):
            if not value.isdecimal():
                raise ValueError(f"generic-web onboarding {label} must be a numeric GitHub ID")
        if _GIT_REF_PATTERN.fullmatch(self.default_branch) is None:
            raise ValueError("generic-web onboarding default_branch is invalid")
        if not self.image_repository:
            raise ValueError("generic-web onboarding requires image_repository")
        if not self.health_path.startswith("/"):
            raise ValueError("generic-web onboarding health_path must start with /")
        for label, value in (
            ("testing_context", self.testing_context),
            ("target_name", self.target_name),
            ("project_name", self.project_name),
            ("environment_name", self.environment_name),
            ("preview_context", self.preview_context),
            ("preview_label", self.preview_label),
            ("preview_app_name_prefix", self.preview_app_name_prefix),
        ):
            if not value:
                raise ValueError(f"generic-web onboarding requires {label}")
        if self.testing_context == self.preview_context:
            raise ValueError("generic-web onboarding testing and preview contexts must differ")
        return self


def generic_web_onboarding_plan_sha256(intent: GenericWebOnboardingIntent) -> str:
    payload = {
        "contract_version": 1,
        "intent": intent.model_dump(mode="json"),
        "provider_operation": "create-application",
        "stable_instance": "testing",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_generic_web_onboarding_manifest(
    *,
    intent: GenericWebOnboardingIntent,
    target_id: str,
) -> ProductOnboardingManifest:
    normalized_target_id = target_id.strip()
    if not normalized_target_id:
        raise ValueError("generic-web onboarding apply requires resolved target_id")
    return ProductOnboardingManifest(
        product=intent.product,
        display_name=intent.display_name,
        repository=intent.repository,
        repository_id=intent.repository_id,
        repository_owner_id=intent.repository_owner_id,
        default_branch=intent.default_branch,
        driver_id="generic-web",
        image_repository=intent.image_repository,
        runtime_port=intent.runtime_port,
        health_path=intent.health_path,
        lanes=(
            ProductOnboardingLaneManifest(
                instance="testing",
                context=intent.testing_context,
            ),
        ),
        preview=ProductOnboardingPreviewManifest(
            enabled=True,
            context=intent.preview_context,
            enable_label=intent.preview_label,
            app_name_prefix=intent.preview_app_name_prefix,
            template_instance="testing",
            domain_certificate_type=intent.domain_certificate_type,
            data_transport_mode="none",
        ),
        provider_targets=(
            ProductOnboardingTargetManifest(
                context=intent.testing_context,
                instance="testing",
                target_id=normalized_target_id,
                project_name=intent.project_name,
                target_type="application",
                target_name=intent.target_name,
                source_git_ref=f"origin/{intent.default_branch}",
                healthcheck_path=intent.health_path,
            ),
        ),
        source_label="service:generic-web-onboarding",
    )
