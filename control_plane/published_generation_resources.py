from typing import Any, Protocol, runtime_checkable

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


@runtime_checkable
class PublishedGenerationResourceStore(Protocol):
    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def read_production_recipient(self, product: str, context: str) -> dict[str, Any] | None: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def list_testing_application_activations(
        self, product: str, context: str
    ) -> tuple[dict[str, Any], ...]: ...

    def read_testing_activation(self, product: str, context: str) -> dict[str, Any] | None: ...


def published_generation_resources(
    store: object, *, context: str, instance: str
) -> dict[str, str] | None:
    if instance not in {"prod", "testing"}:
        return None
    from control_plane.storage.filesystem import FilesystemRecordStore

    if isinstance(store, FilesystemRecordStore):
        return None
    if not isinstance(store, PublishedGenerationResourceStore):
        raise TypeError("Published resources require generation record reads.")
    products = [
        profile.product
        for profile in store.list_product_profile_records()
        if any((lane.context, lane.instance) == (context, instance) for lane in profile.lanes)
    ]
    if not products:
        return None
    if len(products) != 1:
        raise ValueError("Published resources require an exclusively owned lane.")
    product = products[0]
    if instance == "prod":
        from control_plane.contracts.production_recipient import ProductionRecipientRecord
        from control_plane.contracts.dokploy_target_record import DokployTargetRecord

        payload = store.read_production_recipient(product, context)
        if payload is None:
            return None
        record = ProductionRecipientRecord.model_validate(payload)
        identity = store.read_dokploy_target_id_record(context_name=context, instance_name=instance)
        if (record.product, record.context) != (product, context):
            raise ValueError("Published resource custody belongs to another lane.")
        inverse = record.phase in {"inverse_published", "rolled_back"}
        if record.publication:
            snapshot = record.publication["before" if inverse else "published"]
            target = DokployTargetRecord.model_validate(snapshot["target"])
            expected_identity = DokployTargetIdRecord.model_validate(snapshot["target_id"])
        else:
            target = record.retained_target
            expected_identity = record.retained_target_id
        if (
            (target.context, target.instance) != (context, instance)
            or (expected_identity.context, expected_identity.instance) != (context, instance)
            or identity.target_id != expected_identity.target_id
        ):
            raise ValueError("Generation resources no longer match the serving target.")
        keys = {*record.request.volume_keys.values(), *record.request.ports}
        if record.publication and not inverse and not keys <= target.env.keys():
            raise ValueError("Published resource custody is incomplete.")
        return {key: value for key, value in target.env.items() if key in keys}
    from control_plane.testing_activation import activation_record

    activation = activation_record(store, product, context)
    if activation is None:
        return None
    identity = store.read_dokploy_target_id_record(context_name=context, instance_name=instance)
    if (activation.product, activation.context) != (product, context):
        raise ValueError("Published resource custody belongs to another lane.")
    if activation.published:
        if identity.target_id != activation.target_id or activation.published_target is None:
            raise ValueError("Published resources no longer match the serving target.")
        target = activation.published_target
    else:
        if identity.target_id != activation.retained_target_id.target_id:
            raise ValueError("Retained resources no longer match the serving target.")
        target = activation.retained_target
    keys = {*activation.request.volume_keys.values(), *activation.request.port_keys}
    if activation.published and not keys <= target.env.keys():
        raise ValueError("Published resource custody is incomplete.")
    return {key: value for key, value in target.env.items() if key in keys}
