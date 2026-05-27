from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.generic_promotion_inputs import (
    build_generic_promotion_backup_record_id,
    GenericPromotionInputsRequest,
    GenericPromotionInputsResult,
    GenericPromotionInputsStore,
    resolve_generic_promotion_inputs,
)


class OdooProdPromotionInputsStore(GenericPromotionInputsStore, Protocol):
    pass


class OdooProdPromotionInputsRequest(GenericPromotionInputsRequest):
    @model_validator(mode="after")
    def _validate_odoo_request(self) -> "OdooProdPromotionInputsRequest":
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("Odoo prod promotion inputs require testing -> prod.")
        if not self.request_id:
            raise ValueError("Odoo prod promotion inputs require request_id.")
        return self


class OdooProdPromotionInputsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    from_instance: str
    to_instance: str
    request_id: str
    input_status: Literal["ready", "blocked"]
    artifact_id: str = ""
    source_git_ref: str = ""
    backup_record_id: str = ""
    release_tuple_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    error_message: str = ""


def resolve_odoo_prod_promotion_inputs(
    *,
    record_store: OdooProdPromotionInputsStore,
    request: OdooProdPromotionInputsRequest,
) -> OdooProdPromotionInputsResult:
    generic_result = resolve_generic_promotion_inputs(
        record_store=record_store,
        request=request,
        product_label="Odoo prod",
    )
    backup_record_id = ""
    if generic_result.input_status == "ready":
        backup_record_id = build_odoo_prod_backup_record_id(
            context=request.context,
            instance=request.to_instance,
            request_id=request.request_id,
        )
    return _odoo_result_from_generic(
        generic_result=generic_result,
        backup_record_id=backup_record_id,
    )


def _odoo_result_from_generic(
    *,
    generic_result: GenericPromotionInputsResult,
    backup_record_id: str = "",
) -> OdooProdPromotionInputsResult:
    return OdooProdPromotionInputsResult(
        context=generic_result.context,
        from_instance=generic_result.from_instance,
        to_instance=generic_result.to_instance,
        request_id=generic_result.request_id,
        input_status=generic_result.input_status,
        artifact_id=generic_result.artifact_id,
        source_git_ref=generic_result.source_git_ref,
        backup_record_id=backup_record_id,
        release_tuple_id=generic_result.release_tuple_id,
        image_repository=generic_result.image_repository,
        image_digest=generic_result.image_digest,
        error_message=generic_result.error_message,
    )


def build_odoo_prod_backup_record_id(*, context: str, instance: str, request_id: str) -> str:
    return build_generic_promotion_backup_record_id(
        context=context,
        instance=instance,
        request_id=request_id,
    )
