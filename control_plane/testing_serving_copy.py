"""Resolve adopted copy custody without treating a namespace as serving authority."""

from pathlib import Path
from control_plane.runtime_environments import SiteRuntimeEnvironment
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.odoo_testing_restore import generation_snapshot_matches
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import candidate_resources
from control_plane.testing_candidate_runtime import candidate_runtime_fences
from control_plane.testing_publication_storage import read_publication, read_snapshot


def published_testing_copy(
    store: PostgresRecordStore,
    *,
    product: str,
    context: str,
    recovery_operation_id: str = "",
    for_rebuild_admission: bool = False,
) -> TestingPublicationRecord | None:
    try:
        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        identity = store.read_dokploy_target_id_record(
            context_name=context, instance_name="testing"
        )
    except FileNotFoundError:
        return None
    candidate_custody = store.find_testing_candidate_by_target(identity.target_id)
    try:
        binding = store.read_route_binding_record(
            product=product, context_name=context, instance_name="testing"
        )
    except FileNotFoundError:
        binding = None
    key = binding.source.source_versions.get("testing_publication", "") if binding else ""
    if not key:
        if target.source_label == "service:testing-rebuild" or candidate_custody is not None:
            raise ValueError("The native testing copy requires its recorded route provenance.")
        return None
    snapshot = read_snapshot(store, product=product, context=context)
    binding = snapshot.binding
    record = read_publication(store, key)
    if record is None or record.state != "applied":
        raise ValueError("Serving testing copy publication is unavailable.")
    plan = record.plan
    candidate = plan.candidate
    if recovery_operation_id and recovery_operation_id != candidate.operation_id:
        raise ValueError("Published-copy recovery belongs to another original operation.")
    app = candidate.application
    assert app is not None and plan.after.binding is not None
    profile = store.read_product_profile_record(product)
    if (
        not profile.is_active
        or profile.driver_id != "odoo"
        or not is_exclusive_product_context(
            context=context,
            product=product,
            owners=product_context_owner_map(store.list_product_profile_records()),
        )
    ):
        raise ValueError("The adopted copy requires its exclusive active Odoo testing context.")
    lane = next(
        (item for item in profile.lanes if (item.context, item.instance) == (context, "testing")),
        None,
    )
    if (candidate.product, candidate.context) != (product, context) or (
        lane is None
        # The original executor finishes physical retirement under its admitted
        # port. A later profile edit cannot change the already-running copy.
        # Ordinary maintenance still requires the current profile to agree.
        or not (recovery_operation_id or for_rebuild_admission)
        and profile.runtime_port != plan.profile.runtime_port
        or lane.base_url.rstrip("/") != app.plan.base_url
        or snapshot.target_id.target_id != candidate.compose_id
        or snapshot.provider.target_id != candidate.compose_id
        or snapshot.target.target_name != candidate.namespace
        or snapshot.target.project_name != candidate.namespace
        or snapshot.target.domains
        or snapshot.target.public_hosts
        or snapshot.target.pending_public_host_effects
        or binding is None
        or binding.provider_target.target_name != candidate.namespace
        or binding.source.source_versions.get("testing_route") != plan.route.digest
        or candidate_custody != candidate
        or binding.domains != plan.after.binding.domains
        or binding.ingress != plan.after.binding.ingress
        or binding.tls.owner != plan.after.binding.tls.owner
        or snapshot.runtime is None
    ):
        raise ValueError("Serving testing copy no longer matches its publication custody.")
    expected_resources = {
        **candidate_resources(candidate).volumes,
        **candidate_runtime_fences(candidate, runtime_port=plan.profile.runtime_port),
    }
    if any(snapshot.runtime.env.get(key) != value for key, value in expected_resources.items()):
        raise ValueError("Serving testing copy resources changed since adoption.")
    expected_generation = plan.after.generation
    if (expected_generation is None and snapshot.generation is not None) or (
        expected_generation is not None
        and not generation_snapshot_matches(snapshot.generation, expected_generation)
    ):
        raise ValueError("Serving testing copy restore witness changed since adoption.")
    reservation = store.read_provider_target_record(
        context_name=candidate.namespace, instance_name="testing-candidate"
    )
    if (reservation.provider_id, reservation.target_category, reservation.target_id) != (
        "dokploy",
        "compose",
        candidate.compose_id,
    ) or any(
        item.target_id == candidate.compose_id
        and (item.context, item.instance) != (context, "testing")
        for item in store.list_dokploy_target_id_records()
    ):
        raise ValueError("Serving testing copy acquired another lane's alias.")
    return record


def testing_maintenance_secret_keys(store: PostgresRecordStore, context: str) -> frozenset[str]:
    """Only lane-owned testing credentials can reach an adopted testing copy."""
    from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
    from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS

    bindings = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=context, instance_name="testing"
    )
    selected = set()
    for key, binding in bindings.items():
        record = store.read_secret_record(binding.secret_id)
        if binding.declared_secret_class in {"testing", "non_prod"} and (
            record.scope,
            record.context,
            record.instance,
        ) == ("context_instance", context, "testing"):
            selected.add(key)
    if not FRESH_TESTING_LOCAL_CREDENTIAL_KEYS <= selected or any(
        bindings[key].value_source != "generated" for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
    ):
        raise ValueError("The adopted copy requires its service-generated local credentials.")
    return frozenset(selected)


def omitted_maintenance_keys(store: PostgresRecordStore, context: str) -> frozenset[str]:
    from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
    from control_plane.odoo_testing_restore import FRESH_TESTING_OMITTED_SETTINGS
    from control_plane.dokploy.post_deploy import ODOO_UPSTREAM_RESTORE_WORKFLOW_ENV_KEYS

    delivered = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=context, instance_name="testing"
    )
    selected = testing_maintenance_secret_keys(store, context)
    return frozenset(
        {
            *FRESH_TESTING_OMITTED_SETTINGS,
            *ODOO_UPSTREAM_RESTORE_WORKFLOW_ENV_KEYS,
            *(key for key in delivered if key not in selected),
        }
    )


def render_maintained_testing_copy(
    record: TestingPublicationRecord, *, image_reference: str, hold_web: bool
) -> str:
    from control_plane.dokploy.compose import render_odoo_raw_compose_file

    return render_odoo_raw_compose_file(
        image_reference=image_reference,
        hold_web_until_integration_readback=hold_web,
        runtime_port=record.plan.profile.runtime_port,
        publish_host_ports=False,
        mount_workflow_ssh=False,
        isolated_testing=True,
        private_backend_alias=record.plan.candidate.namespace,
    )


def resolve_testing_maintenance_environment(
    root: Path, store: PostgresRecordStore, record: TestingPublicationRecord
) -> SiteRuntimeEnvironment:
    from control_plane.runtime_environments import resolve_site_runtime_environment
    from control_plane.testing_replacement_bootstrap import require_testing_credential_safety

    context = record.plan.candidate.context
    require_maintained_local_credentials(store, record)
    selected = testing_maintenance_secret_keys(store, context)
    omitted = omitted_maintenance_keys(store, context)
    require_testing_credential_safety(store, context, omitted)
    runtime = resolve_site_runtime_environment(
        control_plane_root=root,
        database_url=store.database_url,
        context_name=context,
        instance_name="testing",
        secret_binding_keys=selected,
        target_environment_override={},
    )
    values = {key: value for key, value in runtime.values.items() if key not in omitted}
    values.update(
        candidate_runtime_fences(
            record.plan.candidate, runtime_port=record.plan.profile.runtime_port
        )
    )
    return SiteRuntimeEnvironment(
        values=values,
        secret_keys=frozenset(key for key in runtime.secret_keys if key in values),
        retired_keys=runtime.retired_keys,
        site_keys=frozenset(values),
        setting_keys=frozenset(key for key in runtime.setting_keys if key in values),
    )


def require_maintained_local_credentials(
    store: PostgresRecordStore, record: TestingPublicationRecord
) -> None:
    """Changed local passwords are installed by a fresh copy, before traffic moves."""
    from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
    from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS

    context = record.plan.candidate.context
    delivered = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=context, instance_name="testing"
    )
    original = record.plan.secret_read_set
    for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS:
        current_binding = delivered.get(key)
        expected_binding = next(
            (
                item
                for item in original.bindings
                if item.binding_key == key and item.instance == "testing"
            ),
            None,
        )
        expected_secret = next(
            (
                item
                for item in original.records
                if expected_binding is not None and item.secret_id == expected_binding.secret_id
            ),
            None,
        )
        if (
            current_binding is None
            or expected_secret is None
            or current_binding.secret_id != expected_secret.secret_id
            or store.read_secret_record(current_binding.secret_id).current_version_id
            != expected_secret.current_version_id
        ):
            raise ValueError("Use the native testing rebuild to install changed local credentials.")
