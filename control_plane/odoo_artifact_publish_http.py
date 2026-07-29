from typing import Literal, cast

from pydantic import model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.artifact_identity import artifact_manifest_matches_image_repository
from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_repository_identity,
)
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
    schema_version: Literal[1, 2] = 1
    publish: OdooArtifactPublishEvidenceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooArtifactPublishEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo artifact publish")
        if self.schema_version != self.publish.schema_version:
            raise ValueError(
                "Odoo artifact publish envelope schema_version must match publish schema_version."
            )
        return self


def resolve_odoo_artifact_publish_product_route(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
) -> LaunchplaneProductProfileRecord:
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


def validate_odoo_artifact_publish_product_evidence(
    *,
    product_profile: LaunchplaneProductProfileRecord,
    request: OdooArtifactPublishEnvelope,
) -> None:
    expected_repository = product_profile.image.repository.strip().rstrip("/")
    if not expected_repository:
        raise ValueError("Odoo artifact publish requires a product profile image repository.")
    if not artifact_manifest_matches_image_repository(
        request.publish.manifest,
        expected_repository=expected_repository,
    ):
        raise ValueError(
            "Odoo artifact publish evidence image repository does not match product profile."
        )
    manifest = request.publish.manifest
    if manifest.schema_version == 2:
        dependency_provenance = manifest.dependency_provenance
        if dependency_provenance is None:
            raise ValueError(
                "Odoo artifact publish schema-v2 evidence requires dependency provenance."
            )
        tenant_lock = next(
            lock for lock in dependency_provenance.uv_locks if lock.scope == "tenant"
        )
        expected_source_repository = normalize_artifact_repository_identity(
            product_profile.repository,
            label="Odoo artifact publish product repository",
        )
        if tenant_lock.source_repository != expected_source_repository:
            raise ValueError(
                "Odoo artifact publish evidence source repository does not match product profile."
            )


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
