from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.product_onboarding_manifest import (
    ProductOnboardingExpectedConfigManifest,
    ProductOnboardingLaneManifest,
    ProductOnboardingManifest,
    ProductOnboardingPreviewManifest,
    ProductOnboardingRuntimeEnvironmentManifest,
)
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    ProductOdooLaneDataPolicy,
    ProductRuntimeConfigRequirement,
    ProductSecretConfigRequirement,
)
from control_plane.generic_web_onboarding import (
    normalize_preview_base_url,
    validate_new_product_identity,
)

_LABEL = "Odoo onboarding"
_CONTEXT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_PREVIEW_BASE_URL_ENV_KEY = "LAUNCHPLANE_PREVIEW_BASE_URL"
_ODOO_HTTP_PORT = 8069
_ODOO_HEALTH_PATH = "/web/health"
_ODOO_DB_USER = "odoo"


class OdooOnboardingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    display_name: str
    repository: str
    repository_id: str
    repository_owner_id: str
    default_branch: str = "main"
    image_repository: str
    context: str
    preview_base_url: str
    database_name: str = ""

    @model_validator(mode="after")
    def _validate_intent(self) -> OdooOnboardingIntent:
        self.product = self.product.strip().lower()
        self.display_name = self.display_name.strip()
        self.repository = self.repository.strip()
        self.repository_id = self.repository_id.strip()
        self.repository_owner_id = self.repository_owner_id.strip()
        self.default_branch = self.default_branch.strip()
        self.image_repository = self.image_repository.strip()
        self.context = self.context.strip()
        self.database_name = self.database_name.strip() or self.context
        self.preview_base_url = normalize_preview_base_url(self.preview_base_url, label=_LABEL)
        validate_new_product_identity(self, label=_LABEL)
        for field_name, value in (("context", self.context), ("database_name", self.database_name)):
            if _CONTEXT_PATTERN.fullmatch(value) is None:
                raise ValueError(
                    f"{_LABEL} {field_name} must use lowercase letters, numbers, and underscores"
                )
        return self

    @property
    def testing_context(self) -> str:
        return self.context

    @property
    def preview_context(self) -> str:
        return self.context


def odoo_onboarding_plan_sha256(intent: OdooOnboardingIntent) -> str:
    return nonvalidating_compact_sorted_json_sha256(
        {"contract_version": 1, "driver_id": "odoo", "intent": intent.model_dump(mode="json")}
    )


def build_odoo_onboarding_manifest(intent: OdooOnboardingIntent) -> ProductOnboardingManifest:
    return ProductOnboardingManifest(
        product=intent.product,
        display_name=intent.display_name,
        repository=intent.repository,
        repository_id=intent.repository_id,
        repository_owner_id=intent.repository_owner_id,
        default_branch=intent.default_branch,
        driver_id="odoo",
        image_repository=intent.image_repository,
        runtime_port=_ODOO_HTTP_PORT,
        health_path=_ODOO_HEALTH_PATH,
        lanes=(
            ProductOnboardingLaneManifest(
                instance="testing",
                context=intent.context,
                odoo_data_policy=ProductOdooLaneDataPolicy(
                    data_authority="resettable", allowed_rebuild_sources=("empty",)
                ),
            ),
        ),
        preview=ProductOnboardingPreviewManifest(enabled=True, context=intent.context),
        runtime_environments=(
            ProductOnboardingRuntimeEnvironmentManifest(
                scope="context",
                context=intent.context,
                env={_PREVIEW_BASE_URL_ENV_KEY: intent.preview_base_url},
            ),
            ProductOnboardingRuntimeEnvironmentManifest(
                scope="instance",
                context=intent.context,
                instance="testing",
                env={"ODOO_DB_NAME": intent.database_name, "ODOO_DB_USER": _ODOO_DB_USER},
            ),
        ),
        expected_config=ProductOnboardingExpectedConfigManifest(
            runtime_environment_keys=(
                ProductRuntimeConfigRequirement(
                    key=_PREVIEW_BASE_URL_ENV_KEY, context=intent.context
                ),
                *(
                    ProductRuntimeConfigRequirement(
                        key=key, context=intent.context, instance="testing"
                    )
                    for key in ("ODOO_DB_NAME", "ODOO_DB_USER")
                ),
            ),
            managed_secret_bindings=tuple(
                ProductSecretConfigRequirement(
                    binding_key=key, context=intent.context, instance="testing"
                )
                for key in sorted(FRESH_TESTING_LOCAL_CREDENTIAL_KEYS)
            ),
        ),
        source_label="service:odoo-onboarding",
    )
