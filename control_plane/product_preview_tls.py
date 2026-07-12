import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


ProductPreviewTlsMode = Literal["dry-run", "apply"]
ProductPreviewDomainCertificateType = Literal["none", "letsencrypt"]
PRODUCT_PREVIEW_TLS_SOURCE: Literal["service:product-preview-tls"] = "service:product-preview-tls"


class ProductPreviewTlsDriverError(ValueError):
    pass


class ProductPreviewTlsApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    mode: ProductPreviewTlsMode = "dry-run"
    domain_certificate_type: ProductPreviewDomainCertificateType
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductPreviewTlsApplyRequest":
        self.product = self.product.strip()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if not self.product:
            raise ValueError("Product preview TLS request requires product.")
        if not self.reason:
            raise ValueError("Product preview TLS request requires reason.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Product preview TLS dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply" and not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
            raise ValueError(
                "Product preview TLS apply requires a reviewed 64-character plan SHA-256."
            )
        return self


class ProductPreviewTlsPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductPreviewTlsMode
    product: str
    field: Literal["preview.domain_certificate_type"] = "preview.domain_certificate_type"
    current_value: ProductPreviewDomainCertificateType
    requested_value: ProductPreviewDomainCertificateType
    changed: bool
    applied: bool = False
    reason: str
    source_label: Literal["service:product-preview-tls"] = PRODUCT_PREVIEW_TLS_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    plan_sha256: str


def build_product_preview_tls_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductPreviewTlsApplyRequest,
) -> ProductPreviewTlsPlan:
    if profile.product != request.product:
        raise ValueError("Product preview TLS target does not match the loaded profile.")
    if profile.driver_id != "odoo":
        raise ProductPreviewTlsDriverError("Product preview TLS policy requires the Odoo driver.")
    current_value = profile.preview.domain_certificate_type
    plan_evidence = {
        "schema_version": 1,
        "product": profile.product,
        "field": "preview.domain_certificate_type",
        "current_value": current_value,
        "requested_value": request.domain_certificate_type,
        "profile_updated_at": profile.updated_at,
        "source_label": PRODUCT_PREVIEW_TLS_SOURCE,
        "reason": request.reason,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(
            plan_evidence,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ProductPreviewTlsPlan(
        mode=request.mode,
        product=profile.product,
        current_value=current_value,
        requested_value=request.domain_certificate_type,
        changed=current_value != request.domain_certificate_type,
        reason=request.reason,
        profile_updated_at_before=profile.updated_at,
        plan_sha256=plan_sha256,
    )


def updated_product_preview_tls_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductPreviewTlsApplyRequest,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    updated_preview = profile.preview.model_copy(
        update={"domain_certificate_type": request.domain_certificate_type}
    )
    updated_profile = profile.model_copy(
        update={
            "preview": updated_preview,
            "updated_at": updated_at,
            "source": PRODUCT_PREVIEW_TLS_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(
        updated_profile.model_dump(mode="json")
    ).validate_write_contract()
