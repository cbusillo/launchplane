from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from control_plane import secrets
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetPolicies
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.secret_record import SecretScope
from control_plane.contracts.product_profile_record import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.integration_readback import (
    INTEGRATION_READBACK_PROGRAM,
    integration_readback_policy,
)
from control_plane.odoo_preview_apply_http import build_odoo_preview_runtime_identity
from control_plane.odoo_preview_resources import preview_resource_environment
from control_plane.odoo_runtime_reads import (
    OdooRuntimeSelection,
    OdooRuntimeReadError,
    assert_selection_current,
    select_preview_runtime,
)
from control_plane.odoo_testing_usage import OdooTestingUsage, read_odoo_testing_usage
from control_plane.preview_reservation_recovery import original_preview_intent
from control_plane.preview_serving_evidence import PreviewServingEvidenceError
from control_plane.runtime_platform_credentials import (
    PLATFORM_CREDENTIAL_KEYS,
    looks_like_credential,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_preview_runtime import OdooPreviewDokployApplyResult


class PreviewQualificationMissing(ValueError):
    pass


@dataclass(frozen=True)
class PreviewInspectionAuthority:
    selection: OdooRuntimeSelection
    target: DokployTargetRecord
    target_id: DokployTargetIdRecord
    sha256: str


@dataclass(frozen=True)
class PreviewQualification:
    usage: OdooTestingUsage
    reference: str


def read_preview_inspection_authority(
    store: PostgresRecordStore, preview: PreviewRecord
) -> PreviewInspectionAuthority:
    if not preview.serving_generation_id:
        raise PreviewQualificationMissing("preview_serving_generation_missing")
    try:
        store.read_preview_generation_record(preview.serving_generation_id)
    except FileNotFoundError:
        raise PreviewQualificationMissing("preview_serving_generation_unavailable") from None
    selection = select_preview_runtime(store, preview)
    identity = selection.identity
    profile = store.read_product_profile_record(identity.product)
    operation = store.read_idempotency_record_by_id(identity.deployment_record_id)
    if (
        operation is None
        or operation.state != "completed"
        or operation.response_status_code not in {200, 202}
    ):
        raise PreviewQualificationMissing("preview_completed_deployment_unavailable")
    try:
        retained = original_preview_intent(operation, profile=profile, store=store)
    except ValueError:
        raise PreviewQualificationMissing("preview_original_intent_unavailable") from None
    if (
        build_odoo_preview_runtime_identity(
            profile=profile,
            issued_plan=retained.issued_plan,
            deployment_record_id=operation.record_id,
        )
        != identity
    ):
        raise PreviewQualificationMissing("preview_issued_runtime_identity_mismatch")
    payload = operation.response_payload
    if (
        payload.get("records", {}).get("preview_id") != preview.preview_id
        or payload.get("records", {}).get("generation_id") != identity.preview_generation_id
    ):
        raise PreviewQualificationMissing("preview_completed_generation_mismatch")
    result = OdooPreviewDokployApplyResult.model_validate(payload.get("result"))
    plan = retained.issued_plan.dry_run_plan
    if (
        result.status != "pass"
        or result.operation != "refresh"
        or not result.compose_id
        or result.product != identity.product
        or result.repository != profile.repository
        or result.preview_slug != retained.issued_plan.preview_slug
        or result.compose_name != plan.compose_name
        or result.compose_name != identity.instance
        or not plan.compose_ref.startswith("${created.")
        and plan.compose_ref != result.compose_id
    ):
        raise PreviewQualificationMissing("preview_completed_target_mismatch")
    if any(row.target_id == result.compose_id for row in store.list_dokploy_target_id_records()):
        raise PreviewQualificationMissing("preview_target_alias")
    if any(
        row.provider_id == "dokploy" and row.target_id == result.compose_id
        for row in store.list_physical_provider_target_records()
    ):
        raise PreviewQualificationMissing("preview_target_alias")
    return PreviewInspectionAuthority(
        selection,
        DokployTargetRecord(
            context=identity.context,
            instance=identity.instance,
            target_name=result.compose_name,
            updated_at=identity.deployed_at,
        ),
        DokployTargetIdRecord(
            context=identity.context,
            instance=identity.instance,
            target_id=result.compose_id,
            updated_at=identity.deployed_at,
        ),
        canonical_json_sha256(
            {
                "operation": operation.model_dump(mode="json"),
                "intent": store.read_original_mutation_intent(operation),
                "identity": identity.model_dump(mode="json"),
            }
        ),
    )


def production_credentials(
    store: PostgresRecordStore, *, product: str, root: Path
) -> dict[str, str]:
    profile = store.read_product_profile_record(product)
    lanes = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(lanes) != 1:
        raise PreviewQualificationMissing("preview_production_lane_unavailable")
    context = lanes[0].context
    managed = secrets.resolve_secret_values_for_integration_from_store(
        record_store=store,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context,
        instance_name="prod",
        scopes=frozenset[SecretScope]({"context", "context_instance"}),
    )
    if any(not managed.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS):
        raise PreviewQualificationMissing("preview_production_credential_versions_unavailable")
    target = store.read_dokploy_target_record(context_name=context, instance_name="prod")
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name="prod")
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type=target.target_type, target_id=target_id.target_id
    )
    if payload.get("name") != target.target_name or not isinstance(payload.get("env"), str):
        raise PreviewQualificationMissing("preview_production_provider_credentials_unavailable")
    current = api.parse_dokploy_env_text(str(payload["env"]))
    if any(not current.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS):
        raise PreviewQualificationMissing("preview_production_provider_credentials_unavailable")
    if any(
        not current.get(key) for key in ("ODOO_DATA_VOLUME", "ODOO_DB_VOLUME", "ODOO_LOG_VOLUME")
    ):
        raise PreviewQualificationMissing("preview_production_resource_identity_unavailable")
    return {
        "target:binding": canonical_json_sha256(
            {
                "target": target.model_dump(mode="json"),
                "id": target_id.model_dump(mode="json"),
                "provider": payload,
            }
        ),
        **{f"managed:{key}": value for key, value in managed.items() if value},
        **{f"provider:{key}": value for key, value in current.items() if value},
    }


def collect_odoo_preview_qualification(
    store: PostgresRecordStore, *, preview: PreviewRecord, root: Path
) -> PreviewQualification:
    checkpoint = "preview_serving_authority_unavailable"
    try:
        authority = read_preview_inspection_authority(store, preview)
        identity = authority.selection.identity
        checkpoint = "preview_production_credentials_unavailable"
        credentials = production_credentials(store, product=identity.product, root=root)
        environment = preview_resource_environment(authority.target.target_name)
        if set(environment.values()) & {
            value
            for key, value in credentials.items()
            if key.startswith("provider:") and key.endswith("_VOLUME")
        }:
            raise PreviewQualificationMissing("preview_production_resources_shared")
        policy = integration_readback_policy(
            instance_name="preview", policies=DokployTargetPolicies(), workflow_mode="restore"
        )
        checkpoint = "preview_usage_and_resources_unavailable"
        credential_nonce = os.urandom(32).hex()
        usage = read_odoo_testing_usage(
            target=authority.target,
            target_id=authority.target_id,
            control_plane_root=root,
            database_url=store.database_url,
            preview_spec={
                "runtime_identity": identity.model_dump(mode="json"),
                "image_reference": identity.image_reference,
                "database_name": environment["ODOO_DB_NAME"],
                "preview_volumes": {
                    "data": environment["ODOO_DATA_VOLUME"],
                    "database": environment["ODOO_DB_VOLUME"],
                    "logs": environment["ODOO_LOG_VOLUME"],
                },
                "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
                "usage_only": True,
                "credential_nonce": credential_nonce,
                "forbidden_credential_keys": sorted(PLATFORM_CREDENTIAL_KEYS),
                "production_credential_hashes": sorted(
                    {
                        hashlib.sha256((credential_nonce + value).encode()).hexdigest()
                        for key, value in credentials.items()
                        if key.startswith("managed:")
                        or key.startswith("provider:")
                        and looks_like_credential(key, value)
                    }
                ),
                "integration_spec": policy.encoded_spec(),
                "integration_program": INTEGRATION_READBACK_PROGRAM,
            },
        )
        if usage.usage == "unknown" or not usage.resources_qualified:
            raise PreviewQualificationMissing("preview_qualification_missing:" + usage.reason_code)
        checkpoint = "preview_qualification_authority_changed"
        assert_selection_current(store, authority.selection)
        if (
            read_preview_inspection_authority(store, store.read_preview_record(preview.preview_id))
            != authority
            or production_credentials(store, product=identity.product, root=root) != credentials
        ):
            raise PreviewQualificationMissing(checkpoint)
        return PreviewQualification(usage, "service:odoo-preview-inspection:" + authority.sha256)
    except NativeSourceAuthorizationDenied:
        raise
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied(
                "The provider refused the preview qualification read."
            ) from None
        raise PreviewQualificationMissing(checkpoint) from None
    except PreviewQualificationMissing:
        raise
    except (OdooRuntimeReadError, PreviewServingEvidenceError) as error:
        raise PreviewQualificationMissing("preview_qualification_missing:" + error.code) from None
    except ValueError:
        raise PreviewQualificationMissing(checkpoint) from None
    except (OSError, RuntimeError, TypeError, AttributeError, KeyError):
        raise PreviewQualificationMissing(checkpoint) from None
