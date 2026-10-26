import copy
import hashlib
import hmac
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.production_consumer_reads import (
    ConsumerReadEvidence,
    ProductionConsumerReadResult,
    ProductionConsumerReview,
    ReviewedOdooConsumer,
)
from control_plane.dokploy import source
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied, _read_remote
from control_plane.integration_allowances import canonical_sha256
from control_plane.odoo_instance_overrides import render_post_deploy_payload
from control_plane.odoo_runtime_source_prerequisites import (
    PAYLOAD_ADDON,
    PAYLOAD_ROOT,
    SOURCE_ADDONS,
    producer_files,
)
from control_plane.odoo_secret_references import (
    read_secret_reference_authority,
    selected_config_parameter_bindings,
)
from control_plane.production_consumer_programs import (
    CONSUMER_CONTAINER_PROGRAM,
    CONSUMER_HOST_PROGRAM,
    SOURCE_WITNESS_PROGRAM,
)
from control_plane.production_recipient import (
    ProductionRecipientAdapter,
    authority_snapshot,
    authority_identity,
    custody,
    qualified_render,
    runtime_values,
)
from control_plane.production_recipient_publication import require_owned_publication_rows
from control_plane.production_recipient_published_recovery import published_mounts
from control_plane.runtime_environments import resolve_site_runtime_environment
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_activation_compose import environment_mapping
from control_plane.secrets import _decrypt_secret_value
from control_plane.contracts.runtime_identity import RUNTIME_IDENTITY_ENV_KEY
from control_plane.workflows.ship import utc_now_timestamp

_READ_SLOTS = threading.BoundedSemaphore(2)
MAX_OBSERVATION_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class QualifiedSourceCycle:
    reference: str
    observed: datetime
    started: datetime
    finished: datetime


def qualify_source_cycle(
    observation: Any,
    identity: dict[str, Any],
    published: datetime,
    now: datetime,
    prior: QualifiedSourceCycle | None,
) -> QualifiedSourceCycle:
    observed = datetime.fromisoformat(observation.observed_at)
    if (
        published.utcoffset() != timedelta(0)
        or observed.utcoffset() is None
        or not published <= observed <= now
        or now - observed > MAX_OBSERVATION_AGE
        or observation.status != "pass"
        or observation.expected_runtime_identity is None
        or observation.expected_runtime_identity.model_dump(mode="json") != identity
    ):
        raise ValueError("The source observation does not qualify this publication.")
    targets = [item for item in observation.targets if item.sync is not None]
    if len(targets) != 1:
        raise ValueError("The source cycle is ambiguous.")
    target = targets[0]
    cycle = target.sync.cycle
    if (
        target.status != "pass"
        or target.runtime_identity_status != "match"
        or target.observed_runtime_identity is None
        or target.observed_runtime_identity.model_dump(mode="json") != identity
        or cycle is None
        or cycle.mode != "incremental"
        or cycle.freshness_status != "fresh"
        or cycle.cycle_started_at is None
    ):
        raise ValueError("A fresh incremental source cycle is required.")
    started = datetime.fromisoformat(cycle.cycle_started_at)
    if not published <= started <= observed:
        raise ValueError("The source cycle is outside this publication.")
    if cycle.status == "running" and prior is not None:
        if started < prior.finished or now - prior.observed > MAX_OBSERVATION_AGE:
            raise ValueError("The completed source cycle is stale or not a predecessor.")
        return prior
    if cycle.status != "success" or cycle.cycle_finished_at is None:
        raise ValueError("A completed source cycle is required.")
    finished = datetime.fromisoformat(cycle.cycle_finished_at)
    if (
        not started <= finished <= observed
        or prior is not None
        and (started < prior.started or finished < prior.finished)
    ):
        raise ValueError("The completed source cycle was replayed or is future-dated.")
    return QualifiedSourceCycle(observation.record_id, observed, started, finished)


def stable_source_authority(current: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(authority_identity(current))
    for record in result.get("secrets", []):
        record.pop("last_validated_at", None)
    if result.get("inventory") is not None:
        result["inventory"].pop("updated_at", None)
    return result


@dataclass(frozen=True, repr=False)
class PublishedConsumerSource:
    snapshot: str
    publication: str
    observation: str
    review: ProductionConsumerReview
    spec: dict[str, Any]
    server_id: str
    cycle: QualifiedSourceCycle | None = None


def reviewed_consumers(
    store: PostgresRecordStore, product: str, context: str
) -> ProductionConsumerReview:
    record = custody(store, product, context)
    if record is None:
        raise ValueError("Reviewed consumer authority is missing.")
    return ProductionConsumerReview.model_validate(record.activation["request"]["consumer_review"])


def current_source(
    store: PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    prior: QualifiedSourceCycle | None = None,
) -> PublishedConsumerSource:
    record = custody(store, product, context)
    if record is None or record.phase != "published":
        raise ValueError("A current published recipient is required.")
    require_owned_publication_rows(store, record, inventory_timestamp_required=False)
    current = authority_snapshot(store, product, context)
    adapter = ProductionRecipientAdapter(
        root, store, product, record.request, record.actor, record.scope, "", record.original_key
    )
    adapter.verify(record.model_copy(update={"authority_snapshot": current}), published=True)
    review = ProductionConsumerReview.model_validate(
        record.activation["request"]["consumer_review"]
    )
    profile = store.read_product_profile_record(product)
    lanes = [lane for lane in profile.lanes if lane.context == context and lane.instance == "prod"]
    checks = [
        check
        for lane in lanes
        for check in lane.health_monitoring.checks
        if check.enabled
        and check.kind == "private_http"
        and check.require_runtime_identity
        and check.private_endpoint_key
        in {row["endpoint_key"] for row in current["health_endpoints"]}
    ]
    if len(checks) != 1:
        raise ValueError("One current published identity-bearing source check is required.")
    rows = store.list_public_ingress_observation_records(
        product=product,
        context_name=context,
        instance_name="prod",
        check_name=checks[0].name,
        check_kind="private_http",
        limit=64,
    )
    rows = tuple(
        row
        for row in rows
        if row.purpose == "probe"
        and row.check_kind == checks[0].kind
        and row.check_name == checks[0].name
    )
    if not rows:
        raise ValueError("Current source-cycle evidence is required.")
    observation = rows[0]
    now = datetime.now(timezone.utc)
    published = datetime.fromisoformat(record.publication["published_at"])
    identity = current["inventory"]["runtime_identity"]
    cycle = qualify_source_cycle(observation, identity, published, now, prior)
    rendered = qualified_render(record, runtime_values(store, root, record, published=True))
    database_environment = environment_mapping(
        rendered["services"][record.request.database_service].get("environment", {})
    )
    database = database_environment.get("MARIADB_DATABASE") or database_environment.get(
        "MYSQL_DATABASE"
    )
    if not isinstance(database, str) or not database or len(database) > 128:
        raise ValueError("The database identity is unavailable.")
    spec = {
        "namespace": record.namespace,
        "binding": record.plan_sha256,
        "project": record.app_name,
        "volumes": record.volumes,
        "mounts": published_mounts(record, rendered),
        "database_service": record.request.database_service,
        "application_service": record.request.application_service,
        "database_image": rendered["services"][record.request.database_service]["image"],
        "application_image": record.image_reference,
        "database": database,
    }
    return PublishedConsumerSource(
        canonical_sha256(
            {
                "record": record.model_dump(mode="json"),
                "authority": stable_source_authority(current),
            }
        ),
        record.publication["published_sha256"],
        cycle.reference,
        review,
        spec,
        record.request.destination_server_id,
        cycle,
    )


def select_consumer(
    store: PostgresRecordStore, member: ReviewedOdooConsumer
) -> reads.OdooRuntimeSelection:
    if member.preview_id:
        selection = reads.select_preview_runtime(
            store, store.read_preview_record(member.preview_id)
        )
        if selection.identity.product != member.product:
            raise ValueError("The reviewed preview changed ownership.")
        return selection
    return reads.select_stable_runtime(
        store, store.read_product_profile_record(member.product), member.environment
    )


def consumer_authority(
    store: PostgresRecordStore, root: Path, selection: reads.OdooRuntimeSelection
) -> tuple[dict[str, Any], str]:
    identity = selection.identity
    override = store.read_odoo_instance_override_record(
        context_name=identity.context, instance_name=identity.instance
    )
    authority = read_secret_reference_authority(store, override)
    binding = selected_config_parameter_bindings(
        override, authority, ("repairshopr.sync_db.password",)
    )["repairshopr.sync_db.password"]
    if (
        binding.declared_secret_class != "shared_safe"
        or binding.sharing_reason is None
        or binding.sharing_reason.kind != "read_only_source"
    ):
        raise ValueError("An existing purpose-bound read-only account is required.")
    secret = store.read_secret_record(binding.secret_id)
    version = store.read_secret_version(secret.current_version_id)
    if version.secret_id != secret.secret_id or version.version_id != secret.current_version_id:
        raise ValueError("Current managed version metadata is required.")
    environment = resolve_site_runtime_environment(
        control_plane_root=root,
        context_name=identity.context,
        instance_name=identity.instance,
        database_url=store.database_url,
    )
    if binding.binding_key not in environment.secret_keys or not hmac.compare_digest(
        environment.values.get(binding.binding_key, "").encode(),
        _decrypt_secret_value(version.ciphertext, version.key_id).encode(),
    ):
        raise ValueError("The current managed password cannot be proven.")
    payload = render_post_deploy_payload(
        override, record_store=store, clear_undeclared_shopify=False
    ).model_dump(mode="json")
    parameters = [
        item
        for item in payload["config_parameters"]
        if item["key"].startswith("repairshopr.sync_db.")
    ]
    keys = {
        item["value"]["environment_variable"]
        for item in parameters
        if item["value"].get("environment_variable")
    }
    if not keys <= environment.values.keys() or keys & environment.retired_keys:
        raise ValueError("Every referenced runtime value must be current and unretired.")
    inputs = {
        "desired_payload": {"config_parameters": parameters},
        "desired_env": {key: environment.values[key] for key in keys},
    }
    metadata = authority.metadata.model_dump(mode="json")
    for item in metadata.get("records", []):
        item.pop("last_validated_at", None)
    for item in metadata.get("bindings", []):
        item.pop("created_at", None)
        item.pop("updated_at", None)
    snapshot = json.dumps(
        {
            "override": override.model_dump(mode="json"),
            "metadata": metadata,
            "runtime": authority.runtime.model_dump(mode="json"),
            "version": version.model_dump(mode="json"),
            "inputs": inputs,
        },
        sort_keys=True,
    )
    return inputs, snapshot


def consumer_files(
    store: PostgresRecordStore, connection: reads.OdooRuntimeConnection
) -> dict[str, dict[str, str]]:
    profile = store.read_product_profile_record(connection.selection.identity.product)
    manifest = store.read_artifact_manifest(connection.selection.identity.artifact_id)
    files = producer_files(manifest, connection, profile.repository)
    declaration = manifest.release_compatibility
    candidates = (
        [
            item
            for item in declaration.sources
            if item.input_name.startswith("addon:")
            and any(file.path == "external_ids/utils/ssl_context.py" for file in item.files)
        ]
        if declaration
        else []
    )
    if len(candidates) != 1:
        raise ValueError("Verified installed TLS semantics are unavailable.")
    files["tls"] = {
        f"{PAYLOAD_ROOT}/{file.path}": file.sha256
        for file in candidates[0].files
        if file.path
        in {
            "external_ids/__init__.py",
            "external_ids/__manifest__.py",
            "external_ids/utils/__init__.py",
            "external_ids/utils/ssl_context.py",
        }
    }
    return files


def witness_source(
    root: Path, source_state: PublishedConsumerSource, nonce: str, connection_id: int
) -> bool:
    host, token = source.read_dokploy_config(control_plane_root=root)
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=source_state.server_id,
        program=SOURCE_WITNESS_PROGRAM,
        payload={**source_state.spec, "nonce": nonce, "connection_id": connection_id},
        receive=frames.append,
        timeout_seconds=60,
    )
    return frames == [{"type": "complete", "result": {"verified": True}}]


def read_consumer(
    store: PostgresRecordStore,
    root: Path,
    member: ReviewedOdooConsumer,
    source_state: PublishedConsumerSource,
    selection: reads.OdooRuntimeSelection,
) -> ConsumerReadEvidence:
    connection = reads.connect_runtime(
        store=store, selection=selection, control_plane_root=root, database_url=store.database_url
    )
    desired, snapshot = consumer_authority(store, root, selection)
    files = consumer_files(store, connection)
    nonce = uuid.uuid4().hex
    desired = {
        "desired_payload": desired["desired_payload"],
        "desired_env_sha256": {
            key: hmac.new(bytes.fromhex(nonce), value.encode(), hashlib.sha256).hexdigest()
            for key, value in desired["desired_env"].items()
        },
    }
    frames: list[dict[str, object]] = []
    destination = False

    def receive(frame: dict[str, object]) -> None:
        nonlocal destination
        if frame.get("type") == "connection":
            identifier = frame.get("connection_id")
            if frames or type(identifier) is not int or not 0 < identifier < 2**64:
                raise ValueError("Invalid consumer connection witness.")
            destination = witness_source(root, source_state, nonce, identifier)
        frames.append(frame)

    before = reads.inspect_runtime_container(
        connection.host,
        connection.token,
        connection.server_id,
        connection.evidence.container_id,
        connection.app_name,
        selection.identity,
    )
    program = CONSUMER_CONTAINER_PROGRAM.replace(
        "__CONFIG_PATH__", repr(ODOO_WEB_CONFIG_PATH)
    ).replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY))
    _read_remote(
        host=connection.host,
        token=connection.token,
        server_id=connection.server_id,
        program=CONSUMER_HOST_PROGRAM.replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)),
        payload={
            "project": connection.app_name,
            "container": connection.evidence.container_id,
            "identity": selection.identity.model_dump(mode="json"),
            "files": files,
            "addons": {
                **{name: value[0] for name, value in SOURCE_ADDONS.items()},
                "tls": "external_ids",
            },
            "prefixes": {"repairshopr": "repairshopr.sync_db"},
            "option_fields": ("port", "use_ssl", "ssl_verify", "batch_size"),
            "payload_addon": PAYLOAD_ADDON,
            "payload_root": PAYLOAD_ROOT,
            "nonce": nonce,
            "required_populated_roles": [
                role.value for role in source_state.review.required_populated_roles
            ],
            "container_program": program,
            **desired,
        },
        receive=receive,
        timeout_seconds=100,
    )
    reads.assert_selection_current(store, selection)
    after = reads.inspect_runtime_container(
        connection.host,
        connection.token,
        connection.server_id,
        connection.evidence.container_id,
        connection.app_name,
        selection.identity,
    )
    if (
        before != after
        or consumer_authority(store, root, selection)[1] != snapshot
        or consumer_files(store, connection) != files
        or not 1 <= len(frames) <= 2
    ):
        raise ValueError("Consumer authority changed during the read.")
    last = frames[-1]
    if (
        set(last) != {"type", "result"}
        or last["type"] != "complete"
        or not isinstance(last["result"], dict)
    ):
        raise ValueError("Incomplete consumer evidence.")
    result = last["result"]
    if set(result) != {"query", "populated"}:
        raise ValueError("Invalid consumer evidence.")
    if not source_state.review.required_populated_roles:
        result = {**result, "populated": "unknown"}
    evidence = ConsumerReadEvidence(
        consumer=member,
        **result,
        destination="verified" if destination else "unknown",
        evidence_reference=canonical_sha256(
            {
                "nonce": nonce,
                "publication": source_state.publication,
                "runtime": selection.identity.model_dump(mode="json"),
            }
        ),
    )
    status = (
        "verified"
        if evidence.query == "verified" and evidence.populated == "verified" and destination
        else "failed"
        if evidence.query == "failed" or evidence.populated == "failed"
        else "unsupported"
        if evidence.query == "unsupported"
        else "unknown"
    )
    return evidence.model_copy(update={"status": status})


def qualify_consumer_reads(
    *,
    store: PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    can_read: Callable[[str, str, str], bool],
) -> ProductionConsumerReadResult:
    started = utc_now_timestamp()
    result = ProductionConsumerReadResult(observed_at=started, completed_at=started)
    members: list[ConsumerReadEvidence] = []
    acquired = False
    try:
        review = reviewed_consumers(store, product, context)
        members = [ConsumerReadEvidence(consumer=member) for member in review.consumers]
        result = result.model_copy(update={"consumers": tuple(members)})
        acquired = _READ_SLOTS.acquire(blocking=False)
        if not acquired:
            return result.model_copy(update={"busy": True, "completed_at": utc_now_timestamp()})
        current = current_source(store, root, product, context)
        if current.review != review:
            raise ValueError("Reviewed consumer authority changed.")

        def observe(member: ReviewedOdooConsumer) -> ConsumerReadEvidence:
            try:
                selection = select_consumer(store, member)
                if can_read(
                    member.product, selection.identity.context, selection.identity.instance
                ):
                    return read_consumer(store, root, member, current, selection)
            except NativeSourceAuthorizationDenied:
                raise
            except Exception:
                return ConsumerReadEvidence(consumer=member)
            return ConsumerReadEvidence(consumer=member)

        with ThreadPoolExecutor(max_workers=len(review.consumers)) as executor:
            members = list(executor.map(observe, review.consumers))
        after = current_source(store, root, product, context, current.cycle)
        if after.snapshot != current.snapshot:
            members = [ConsumerReadEvidence(consumer=item.consumer) for item in members]
        verified = (
            bool(review.required_populated_roles)
            and len(members) == len(current.review.consumers)
            and all(item.status == "verified" for item in members)
        )
        return ProductionConsumerReadResult(
            status="verified" if verified else "unknown",
            observed_at=started,
            completed_at=utc_now_timestamp(),
            publication_sha256=current.publication,
            source_observation_reference=after.observation,
            consumers=tuple(members),
        )
    except NativeSourceAuthorizationDenied:
        raise
    except Exception:
        return result.model_copy(
            update={
                "completed_at": utc_now_timestamp(),
                "consumers": tuple(
                    ConsumerReadEvidence(consumer=item.consumer) for item in members
                ),
            }
        )
    finally:
        if acquired:
            _READ_SLOTS.release()
