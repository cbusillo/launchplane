from control_plane.contracts.production_recipient import ProductionRecipientRecord


def require_unretired_provider_name(store: object, name: str, target_id: str = "") -> None:
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore):
        return
    for payload in store.list_production_recipient_payloads():
        record = ProductionRecipientRecord.model_validate(payload)
        if not record.execution.get("retirement_started_at"):
            continue
        manifest = record.execution["retirement_manifest"]
        if name == manifest["project"] or target_id and target_id == manifest["target"]:
            raise ValueError(
                "The original retirement permanently reserves this provider name and identity; choose a new name."
            )
