from typing import cast

from pydantic import Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishEvidenceRequest,
    OdooArtifactPublishEvidenceStore,
    ingest_odoo_artifact_publish_evidence,
)


ODOO_ARTIFACT_PUBLISH_ROUTE = "/v1/drivers/odoo/artifact-publish"


class OdooArtifactPublishProductMismatchError(OdooProductMismatchError):
    pass


class OdooArtifactPublishRouteDependencyError(OdooRouteDependencyError):
    pass


class OdooArtifactPublishEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    publish: OdooArtifactPublishEvidenceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooArtifactPublishEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo artifact publish")
        return self


def resolve_odoo_artifact_publish_product_route(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
) -> LaunchplaneProductProfileRecord | None:
    try:
        return resolve_odoo_product_route(
            record_store=record_store,
            product=product,
            context=context,
            instance=instance,
        )
    except OdooRouteDependencyError as error:
        raise OdooArtifactPublishRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooArtifactPublishProductMismatchError from error


def ingest_odoo_artifact_publish_evidence_result(
    *,
    record_store: object,
    request: OdooArtifactPublishEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = ingest_odoo_artifact_publish_evidence(
        record_store=cast(OdooArtifactPublishEvidenceStore, record_store),
        request=request.publish,
    )
    result = driver_result.model_dump(mode="json")
    records: dict[str, object] = {"artifact_id": driver_result.artifact_id}
    return records, result


def should_store_odoo_artifact_publish_idempotency(
    driver_result: dict[str, object],
) -> bool:
    return str(driver_result.get("status", "")).strip() == "pass"
