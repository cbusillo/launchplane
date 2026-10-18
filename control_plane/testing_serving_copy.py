"""Resolve adopted copy custody without treating a namespace as serving authority."""

from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.odoo_testing_restore import generation_snapshot_matches
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import candidate_resources
from control_plane.testing_publication_storage import read_publication, read_snapshot


def published_testing_copy(
    store: PostgresRecordStore, *, product: str, context: str
) -> TestingPublicationRecord | None:
    target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
    identity = store.read_dokploy_target_id_record(context_name=context, instance_name="testing")
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
    app = candidate.application
    assert app is not None and plan.after.binding is not None
    profile = store.read_product_profile_record(product)
    lane = next(
        (item for item in profile.lanes if (item.context, item.instance) == (context, "testing")),
        None,
    )
    if (candidate.product, candidate.context) != (product, context) or (
        lane is None
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
        "ODOO_PROJECT_NAME": candidate.namespace,
        "ODOO_STACK_NAME": candidate.namespace,
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
