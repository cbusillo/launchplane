from control_plane.storage.postgres import PostgresRecordStore


def published_generation_resources(
    store: PostgresRecordStore, *, context: str, instance: str
) -> dict[str, str]:
    if instance not in {"prod", "testing"}:
        return {}
    products = [
        profile.product
        for profile in store.list_product_profile_records()
        if any((lane.context, lane.instance) == (context, instance) for lane in profile.lanes)
    ]
    if not products:
        return {}
    if len(products) != 1:
        raise ValueError("Published resources require an exclusively owned lane.")
    product = products[0]
    if instance == "prod":
        from control_plane.contracts.production_recipient import ProductionRecipientRecord
        from control_plane.production_recipient import recipient_resource_environment

        payload = store.read_production_recipient(product, context)
        if payload is None:
            return {}
        record = ProductionRecipientRecord.model_validate(payload)
        identity = store.read_dokploy_target_id_record(context_name=context, instance_name=instance)
        if (record.product, record.context) != (product, context):
            raise ValueError("Published resource custody belongs to another lane.")
        if (
            record.phase in {"published", "reverting", "retiring_old", "old_retired"}
            and record.publication
        ):
            if identity.target_id != record.target_id:
                raise ValueError("Published resources no longer match the serving target.")
            return recipient_resource_environment(record)
        if record.publication and record.phase not in {"inverse_published", "rolled_back"}:
            raise ValueError("Published resource custody has an unqualified phase.")
        if identity.target_id != record.retained_target_id.target_id:
            raise ValueError("Retained resources no longer match the serving target.")
        keys = {*record.request.volume_keys.values(), *record.request.ports}
        return {key: value for key, value in record.retained_target.env.items() if key in keys}
    from control_plane.testing_activation import activation_record

    activation = activation_record(store, product, context)
    if activation is None:
        return {}
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
