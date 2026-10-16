"""Purpose and exact managed-version binding for rendered MySQL read-only sources."""

from typing import Any

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.integration_allowances import canonical_sha256
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.storage.postgres import PostgresRecordStore


def upstream_connections(
    record: ProductionRecipientRecord, document: dict[str, Any], values: dict[str, str]
) -> list[dict[str, Any]]:
    """Private connection inputs; callers never receive these values or their hashes."""
    bindings = [SecretBinding.model_validate(row) for row in record.authority_snapshot["bindings"]]
    secrets = [SecretRecord.model_validate(row) for row in record.authority_snapshot["secrets"]]
    result = []
    for upstream in record.request.upstreams:
        if any(
            key == row.binding_key and row.status == "configured"
            for role, key in upstream.keys.items()
            if role != "password"
            for row in bindings
        ):
            raise ValueError("Upstream endpoints and identities require nonsecret lane settings.")
        binding = next((row for row in bindings if row.binding_id == upstream.binding_id), None)
        secret = next(
            (row for row in secrets if binding and row.secret_id == binding.secret_id), None
        )
        if (
            binding is None
            or secret is None
            or binding.status != "configured"
            or secret.status != "configured"
            or binding.integration != "runtime_environment"
            or (binding.context, binding.instance) != (record.context, "prod")
            or (secret.scope, secret.context, secret.instance)
            != ("context_instance", record.context, "prod")
            or binding.declared_secret_class != "shared_safe"
            or binding.sharing_reason is None
            or binding.sharing_reason.kind != "read_only_source"
            or secret.current_version_id != upstream.version_id
            or upstream.keys["password"] != binding.binding_key
        ):
            raise ValueError("The upstream requires its exact managed production read-only source.")
        environment = document["services"][upstream.service].get("environment", {})
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) for item in environment)
        settings = {role: str(environment.get(key, "")) for role, key in upstream.keys.items()}
        if (
            any(not value or len(value) > 4096 for value in settings.values())
            or any(settings[role] != values.get(key) for role, key in upstream.keys.items())
            or settings["password"] != values.get(binding.binding_key)
            or not settings["port"].isdecimal()
            or not 1 <= int(settings["port"]) <= 65535
            or settings["host"]
            in {*document["services"], "localhost", "127.0.0.1", "::1", "0.0.0.0"}
            or any(character in settings["host"] for character in "/:@?=# \t\r\n")
        ):
            raise ValueError("The rendered upstream and its managed connection do not agree.")
        if document.get("networks") or any(
            service.get("networks") for service in document["services"].values()
        ):
            raise ValueError(
                "Upstream probes and the recipient must use the same private default network."
            )
        # The local writer cannot be reclassified as an upstream by repeating its
        # credential under another key. This comparison stays inside the service.
        local = document["services"][record.request.database_service].get("environment", {})
        if isinstance(local, list):
            local = dict(item.split("=", 1) for item in local)
        if settings["password"] in local.values():
            raise ValueError("A local database credential cannot be an upstream credential.")
        result.append(
            {
                "settings": settings,
                "binding": binding.model_dump(mode="json"),
                "version_id": upstream.version_id,
                "service": upstream.service,
            }
        )
    return result


def require_delivered_upstreams(
    store: PostgresRecordStore, record: ProductionRecipientRecord
) -> None:
    if not record.request.upstreams:
        return
    delivered = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=record.context, instance_name="prod"
    )
    for upstream in record.request.upstreams:
        selected = [row for row in delivered.values() if row.binding_id == upstream.binding_id]
        if len(selected) != 1:
            raise ValueError(
                "The exact upstream binding must be delivered to this production lane."
            )
        secret = store.read_secret_record(selected[0].secret_id)
        version = store.read_secret_version(upstream.version_id)
        if (
            secret.current_version_id != upstream.version_id
            or version.secret_id != secret.secret_id
        ):
            raise ValueError("The upstream managed version changed.")


def upstream_binding(record: ProductionRecipientRecord) -> str:
    # Never hash plaintext credentials, even into private persisted evidence.
    return canonical_sha256(
        {
            "plan": record.plan_sha256,
            "recipient": record.target_id,
            "destination": record.request.destination_server_id,
            "upstreams": [item.model_dump(mode="json") for item in record.request.upstreams],
        }
    )
