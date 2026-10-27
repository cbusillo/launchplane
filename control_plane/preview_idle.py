from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
import hashlib
import json
import logging
from pathlib import Path
from threading import Event, Thread
from typing import Any
import uuid

import yaml

from control_plane._odoo_copy_runtime_guard import preview_has_source_connection
from control_plane.contracts.idempotency_record import (
    build_launchplane_mutation_reservation_id,
    LaunchplaneIdempotencyRecord,
)
from control_plane.contracts.preview_execution import (
    odoo_preview_reconciliation_key,
    odoo_preview_target_key,
    preview_execution_lock_name,
    PREVIEW_IDLE_SCOPE as IDLE_SCOPE,
    PREVIEW_IDLE_ROUTE as IDLE_ROUTE,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.memory_capacity import preview_host_memory_floor
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable
from control_plane.dokploy.preview_inventory import complete_preview_compose_inventory
from control_plane.drivers.registry import product_profile_uses_odoo_driver
from control_plane.disposable_lanes import require_disposable_lane
from control_plane.preview_gateway import PREVIEW_GATEWAY_SERVICE, PREVIEW_IDLE_SECONDS
from control_plane.preview_idle_runtime import PREVIEW_IDLE_RUNTIME_PROGRAM
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import (
    effective_preview_app_name_prefix,
    preview_application_name,
    resolve_generic_web_preview_slug,
    serialize_generic_web_preview_operation,
)
from control_plane.workflows.odoo_verification import DEFAULT_ODOO_RUNTIME_HEALTH_PATH


_LOGGER = logging.getLogger(__name__)
PREVIEW_IDLE_MAX_ATTEMPTS = 3


def idle_action(observation: dict[str, Any]) -> str:
    gateway, services = observation["gateway"], observation["services"]
    if set(services) != {"web", "database", "script-runner"}:
        raise ValueError("Preview idle service evidence is incomplete.")
    if gateway["mode"] == "asleep":
        return "awake" if gateway["requested"] else ""
    if (
        gateway["mode"] == "awake"
        and gateway["active"] == 0
        and not gateway["requested"]
        and gateway["idle_seconds"] >= PREVIEW_IDLE_SECONDS
        and all(item["running"] and item["healthy"] for item in services.values())
    ):
        return "sleep"
    return ""


def _fingerprint(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def idle_target_binding(target: dict[str, Any]) -> dict[str, Any]:
    spec = {
        key: value
        for key, value in target["spec"].items()
        if key not in {"memory_floor", "disk_reserve"}
    }
    return {
        **{key: value for key, value in target.items() if key != "authority_binding"},
        "spec": spec,
    }


def original_idle_intent(
    store: PostgresRecordStore, reservation: LaunchplaneIdempotencyRecord, product: str
) -> dict[str, Any]:
    intent = store.read_original_mutation_intent(reservation)
    if (
        (reservation.scope, reservation.route_path) != (IDLE_SCOPE, IDLE_ROUTE)
        or intent.get("product") != product
        or intent.get("action") not in {"sleep", "awake"}
    ):
        raise ValueError("Preview idle original intent is unavailable.")
    target = intent.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("spec"), dict):
        raise ValueError("Preview idle target binding is unavailable.")
    generation = target["spec"].get("generation")
    control = intent.get("control")
    if not isinstance(generation, dict) or not isinstance(control, dict):
        raise ValueError("Preview idle original generation or control is unavailable.")
    identity = RuntimeIdentity.model_validate_json(target["spec"]["identity"])
    if (
        identity.product != product
        or identity.environment_kind != "preview"
        or not identity.preview_id
        or not identity.preview_generation_id
        or identity.preview_id != intent.get("preview_id")
        or identity.context != target.get("context")
        or identity.instance != target.get("compose_name")
        or identity.image_reference != target["spec"].get("image_reference")
        or generation.get("instance") != "preview"
        or generation.get("namespace") != target["spec"].get("compose_project")
        or odoo_preview_target_key(context=identity.context, compose_name=identity.instance)
        != reservation.provider_target_key
        or odoo_preview_reconciliation_key(context=identity.context, compose_name=identity.instance)
        != reservation.reconciliation_key
        or _fingerprint(intent) != reservation.request_fingerprint
    ):
        raise ValueError("Preview idle original binding differs.")
    return intent


def observe_idle_reservation(
    store: PostgresRecordStore, reservation: LaunchplaneIdempotencyRecord, product: str, root: Path
) -> str:
    intent = original_idle_intent(store, reservation, product)
    host, token = source.read_dokploy_config(control_plane_root=root)
    with store.testing_replacement_execution_lock(
        preview_execution_lock_name(reservation.provider_target_key)
    ) as check:
        if check is None:
            return "in_progress"
        adapter = PreviewIdleAdapter(
            host=host,
            token=token,
            target=intent["target"],
            intent=intent,
            record_id=reservation.record_id,
            check=check,
        )
        return run_durable_provider_operation(
            store=store,
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            lease_owner=uuid.uuid4().hex,
            response_trace_id="preview-idle-observe:" + uuid.uuid4().hex,
            adapter=adapter,
            allow_mutation=False,
            original_intent=intent,
        ).status


def completed_idle_intent(
    store: PostgresRecordStore,
    record_id: str,
    product: str,
    target: dict[str, Any],
    action: str,
) -> dict[str, Any] | None:
    original = store.read_idempotency_record_by_id(record_id)
    if original is None or original.state != "completed":
        return None
    try:
        intent = original_idle_intent(store, original, product)
    except ValueError:
        return None
    if intent["target"] != idle_target_binding(target) or intent["action"] != action:
        return None
    return intent


def idle_reservation_is_quiescent(
    store: PostgresRecordStore,
    reservation: LaunchplaneIdempotencyRecord,
    product: str,
    compose_id: str,
    root: Path,
) -> bool:
    intent = original_idle_intent(store, reservation, product)
    if intent["target"]["compose_id"] != compose_id:
        return False
    host, token = source.read_dokploy_config(control_plane_root=root)
    adapter = PreviewIdleAdapter(
        host=host,
        token=token,
        target=intent["target"],
        intent=intent,
        record_id=reservation.record_id,
        check=lambda: None,
    )
    result = adapter.read()
    return set(result["services"]) == {"web", "database", "script-runner"}


class PreviewIdleAdapter:
    def __init__(
        self,
        *,
        host: str,
        token: str,
        target: dict[str, Any],
        intent: dict[str, Any],
        record_id: str,
        check: Callable[[], None],
        admit: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        self.host, self.token = host, token
        self.target, self.intent = target, intent
        self.record_id, self.check = record_id, check
        self.admit = admit

    def target_key(self) -> str:
        return odoo_preview_target_key(
            context=self.target["context"], compose_name=self.target["compose_name"]
        )

    def reconciliation_key(self) -> str:
        return odoo_preview_reconciliation_key(
            context=self.target["context"], compose_name=self.target["compose_name"]
        )

    def read(
        self, action: str = "observe", lease: ProviderOperationLease | None = None
    ) -> dict[str, Any]:
        self.check()
        frames: list[dict[str, object]] = []
        control = {
            "action": self.intent.get("action", "status"),
            "record_id": self.record_id,
            **self.intent.get("control", {}),
        }

        def dispatch(effect: Callable[[], None]) -> None:
            effect_started = False
            try:
                with self.admit() if lease is not None else nullcontext():
                    self.check()
                    if lease is not None:
                        effect_started = True
                        lease.checkpoint_effect("preview_idle_" + action)
                    effect()
            except Exception as error:
                if lease is not None and not effect_started:
                    raise ProviderMutationRejectedError(error) from error
                raise

        _read_remote(
            host=self.host,
            token=self.token,
            server_id=self.target["server_id"],
            program=PREVIEW_IDLE_RUNTIME_PROGRAM,
            payload={**self.target["spec"], "action": action, "control": control},
            receive=frames.append,
            timeout_seconds=720 if action == "awake" else 240,
            dispatch_guard=dispatch,
        )
        self.check()
        result = frames[0].get("result") if len(frames) == 1 else None
        if not isinstance(result, dict) or frames[0].get("type") != "complete":
            raise NativeSourceUnavailable("Preview idle observation is unavailable.")
        return result

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        result = self.read()
        gateway = result["gateway"]
        action = self.intent["action"]
        services = result["services"]
        present = (
            gateway["mode"] == "asleep"
            and gateway["sleep_record_id"] == self.record_id
            and all(not item["running"] for item in services.values())
            if action == "sleep"
            else gateway["mode"] == "awake"
            and gateway["wake_record_id"] == self.intent["control"].get("record_id", self.record_id)
        )
        if present:
            return ProviderObservation("present", response_status_code=200, response_payload=result)
        if action == "sleep" and gateway["mode"] == "awake":
            original = self.intent["control"]
            if (gateway["boot_id"], gateway["sequence"]) != (
                original["boot_id"],
                original["sequence"],
            ):
                return ProviderObservation(
                    "present", response_status_code=200, response_payload={"accepted": False}
                )
        retry = (
            action == "sleep"
            and gateway["mode"] == "asleep"
            and gateway["sleep_record_id"] == self.record_id
            or action == "sleep"
            and gateway["mode"] == "awake"
            or action == "awake"
            and gateway["mode"] == "asleep"
            and gateway["sleep_record_id"] == self.intent["control"]["sleep_record_id"]
            and gateway["wake_record_id"]
            in {"", self.intent["control"].get("record_id", self.record_id)}
        )
        return ProviderObservation(
            "absent" if retry else "unknown",
            retry_safe=retry,
            preserve_effect_checkpoint=True,
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        result = self.read(self.intent["action"], lease)
        return ProviderMutationOutcome(
            response_status_code=200,
            response_payload=result,
            provider_effect_performed=result.get("accepted") is True,
        )


class PreviewCannotSleep(ValueError):
    pass


def require_preview_idle_binding(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    preview_id: str,
    host: str,
    token: str,
    *,
    inventory: tuple[tuple[str, str, str], ...] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        not profile.is_active
        or not profile.preview.enabled
        or not product_profile_uses_odoo_driver(profile)
    ):
        raise PreviewCannotSleep("Idle sleep is not enabled for this product.")
    preview = store.read_preview_record(preview_id)
    if preview.context != profile.preview.context or preview.anchor_repo not in {
        profile.repository,
        profile.repository.partition("/")[2],
    }:
        raise PreviewCannotSleep("Preview ownership does not match the product.")
    generation_id = preview.serving_generation_id
    if (
        preview.state != "active"
        or not generation_id
        or preview.active_generation_id != generation_id
    ):
        raise PreviewCannotSleep("No current serving preview generation is recorded.")
    generation = store.read_preview_generation_record(generation_id)
    identity = generation.runtime_identity
    if (
        generation.state != "ready"
        or generation.verify_status != "pass"
        or identity is None
        or identity.environment_kind != "preview"
        or identity.product != profile.product
        or identity.context != profile.preview.context
        or identity.preview_id != preview_id
        or identity.preview_generation_id != generation_id
    ):
        raise PreviewCannotSleep("The serving generation has not passed identity verification.")
    slug = resolve_generic_web_preview_slug(
        profile=profile,
        preview_slug="",
        anchor_pr_number=preview.anchor_pr_number,
        label="Preview idle",
    )
    name = preview_application_name(
        app_name_prefix=effective_preview_app_name_prefix(profile=profile), preview_slug=slug
    )
    matches = [
        entry[1]
        for entry in (
            inventory
            if inventory is not None
            else complete_preview_compose_inventory(host=host, token=token)
        )
        if entry[2] == name
    ]
    if len(matches) != 1 or identity.instance != name:
        raise PreviewCannotSleep("The preview target cannot be uniquely bound to its generation.")
    authority_binding = require_disposable_lane(
        store,
        product=profile.product,
        context=profile.preview.context,
        instance=slug,
        preview=preview,
        preview_operation="refresh",
    )
    compose_id = matches[0]
    for registered in store.list_dokploy_target_id_records():
        if registered.target_id == compose_id:
            if (registered.context, registered.instance) != (profile.preview.context, slug):
                raise PreviewCannotSleep("The provider target is registered to another lane.")
            target = store.read_dokploy_target_record(
                context_name=registered.context, instance_name=registered.instance
            )
            if target.policies.staff_testing_hold is not None:
                raise PreviewCannotSleep("Staff testing is holding this preview awake.")
    if payload is None:
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=compose_id
        )
    declaration = yaml.safe_load(str(payload.get("composeFile") or ""))
    if (
        payload.get("name") != name
        or payload.get("sourceType") != "raw"
        or not isinstance(declaration, dict)
        or PREVIEW_GATEWAY_SERVICE not in declaration.get("services", {})
    ):
        raise PreviewCannotSleep("The idle gateway is missing; an ordinary rebuild installs it.")
    env = api.parse_dokploy_env_text(str(payload.get("env") or ""))
    raw_identity = runtime_identity_env(identity)["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]
    if env.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON") != raw_identity:
        raise PreviewCannotSleep(
            "The provider runtime identity differs from the serving generation."
        )
    if preview_has_source_connection(env):
        raise PreviewCannotSleep("Upstream or SSH connection settings remain on this preview.")
    budget = next(
        (
            lane.odoo_testing_drill_storage
            for lane in profile.lanes
            if lane.context == profile.preview.context and lane.instance == "testing"
        ),
        None,
    )
    return {
        "context": profile.preview.context,
        "compose_name": name,
        "compose_id": compose_id,
        "server_id": str(payload.get("serverId") or ""),
        "slug": slug,
        "authority_binding": authority_binding,
        "spec": {
            "generation": {
                "instance": "preview",
                "namespace": str(payload.get("appName") or ""),
            },
            "compose_project": str(payload.get("appName") or ""),
            "database_name": env.get("ODOO_DB_NAME", ""),
            "health_path": DEFAULT_ODOO_RUNTIME_HEALTH_PATH,
            "image_reference": identity.image_reference,
            "preview_volumes": {
                "data": env.get("ODOO_DATA_VOLUME", ""),
                "database": env.get("ODOO_DB_VOLUME", ""),
                "logs": env.get("ODOO_LOG_VOLUME", ""),
            },
            "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
            "identity": raw_identity,
            "usage_only": True,
            "memory_floor": preview_host_memory_floor(
                server_id=str(payload.get("serverId") or ""),
                record_store=store,
            ),
            "disk_reserve": budget.reserve_bytes if budget else 0,
        },
    }


class PreviewIdleScheduler:
    def __init__(self, store: PostgresRecordStore, *, control_plane_root: Path) -> None:
        self.store, self.control_plane_root = store, control_plane_root
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="preview-idle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if not self._thread.is_alive():
                self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                _LOGGER.exception("Preview idle pass is unavailable")
            self._stop.wait(15)

    def _bind(
        self, profile: LaunchplaneProductProfileRecord, preview_id: str, host: str, token: str
    ) -> dict[str, Any] | None:
        try:
            return require_preview_idle_binding(self.store, profile, preview_id, host, token)
        except PreviewCannotSleep:
            return None

    def run_once(self) -> None:
        host, token = source.read_dokploy_config(control_plane_root=self.control_plane_root)
        for held in self.store.list_held_provider_target_reservations():
            if self._stop.is_set():
                return
            if held.route_path != IDLE_ROUTE:
                continue
            try:
                retained = self.store.read_original_mutation_intent(held)
                observe_idle_reservation(
                    self.store, held, str(retained.get("product") or ""), self.control_plane_root
                )
            except Exception:
                _LOGGER.warning(
                    "Preview idle original observation is unavailable for %s", held.record_id
                )
        for profile in self.store.list_product_profile_records():
            if (
                not profile.is_active
                or not profile.preview.enabled
                or not product_profile_uses_odoo_driver(profile)
            ):
                continue
            for preview in self.store.list_preview_records(context_name=profile.preview.context):
                if self._stop.is_set():
                    return
                try:
                    self._run_preview(profile, preview.preview_id, host, token)
                except Exception:
                    _LOGGER.warning(
                        "Preview idle observation unavailable for %s", preview.preview_id
                    )

    def _run_preview(
        self, profile: LaunchplaneProductProfileRecord, preview_id: str, host: str, token: str
    ) -> None:
        target = self._bind(profile, preview_id, host, token)
        if target is None:
            return
        key = odoo_preview_target_key(
            context=target["context"], compose_name=target["compose_name"]
        )
        held_before = self.store.list_held_provider_target_reservations(provider_target_key=key)
        if held_before and any(
            (item.scope, item.route_path) != (IDLE_SCOPE, IDLE_ROUTE) for item in held_before
        ):
            return
        if not held_before:
            observer = PreviewIdleAdapter(
                host=host, token=token, target=target, intent={}, record_id="", check=lambda: None
            )
            if not idle_action(observer.read("poll")):
                return
        with self.store.testing_replacement_execution_lock(
            preview_execution_lock_name(key)
        ) as check:
            if check is None:
                return
            with ExitStack() as locks:
                locks.enter_context(
                    self.store.serialize_preview_refresh(preview_id=preview_id, dedicated=True)
                )
                locks.enter_context(
                    serialize_generic_web_preview_operation(
                        record_store=self.store, profile=profile, preview_slug=target["slug"]
                    )
                )
                current = self._bind(
                    self.store.read_product_profile_record(profile.product), preview_id, host, token
                )
                if current != target:
                    return
                check()

                @contextmanager
                def admit() -> Iterator[None]:
                    with self.store.serialize_existing_preview_refresh():
                        fresh_profile = self.store.read_product_profile_record(profile.product)
                        fresh_preview = self.store.read_preview_record(preview_id)
                        fresh_generation = self.store.read_preview_generation_record(
                            fresh_preview.serving_generation_id
                        )
                        if (
                            not fresh_profile.is_active
                            or not fresh_profile.preview.enabled
                            or fresh_preview.state != "active"
                            or fresh_preview.active_generation_id
                            != fresh_preview.serving_generation_id
                            or fresh_generation.state != "ready"
                            or fresh_generation.runtime_identity is None
                            or runtime_identity_env(fresh_generation.runtime_identity)[
                                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"
                            ]
                            != target["spec"]["identity"]
                            or require_disposable_lane(
                                self.store,
                                product=profile.product,
                                context=profile.preview.context,
                                instance=target["slug"],
                                preview=fresh_preview,
                                preview_operation="refresh",
                            )
                            != target["authority_binding"]
                        ):
                            raise ValueError("Preview idle authority changed before dispatch.")
                        yield

                held = self.store.list_held_provider_target_reservations(provider_target_key=key)
                intent: dict[str, Any] = {}
                idempotency_key = ""
                if held:
                    if len(held) != 1 or (held[0].scope, held[0].route_path) != (
                        IDLE_SCOPE,
                        IDLE_ROUTE,
                    ):
                        return
                    intent = original_idle_intent(self.store, held[0], profile.product)
                    if intent.get("target") != idle_target_binding(target) or intent.get(
                        "action"
                    ) not in {
                        "sleep",
                        "awake",
                    }:
                        return
                    idempotency_key = held[0].idempotency_key
                else:
                    observer = PreviewIdleAdapter(
                        host=host, token=token, target=target, intent={}, record_id="", check=check
                    )
                    observation = observer.read()
                    action = idle_action(observation)
                    if not action:
                        return
                    gateway = observation["gateway"]
                    if action == "awake":
                        if (
                            completed_idle_intent(
                                self.store,
                                gateway["sleep_record_id"],
                                profile.product,
                                target,
                                "sleep",
                            )
                            is None
                        ):
                            return
                    control = (
                        {"boot_id": gateway["boot_id"], "sequence": gateway["sequence"]}
                        if action == "sleep"
                        else {"sleep_record_id": gateway["sleep_record_id"]}
                    )
                    if action == "awake" and gateway["wake_record_id"]:
                        original_wake = completed_idle_intent(
                            self.store, gateway["wake_record_id"], profile.product, target, "awake"
                        )
                        if (
                            original_wake is None
                            or original_wake["control"].get("sleep_record_id")
                            != gateway["sleep_record_id"]
                        ):
                            return
                        control["record_id"] = gateway["wake_record_id"]
                    intent = {
                        "product": profile.product,
                        "preview_id": preview_id,
                        "target": idle_target_binding(target),
                        "action": action,
                        "control": control,
                        "attempt_id": uuid.uuid4().hex,
                    }
                    idempotency_key = _fingerprint(intent)
                record_id = build_launchplane_mutation_reservation_id(
                    scope=IDLE_SCOPE, route_path=IDLE_ROUTE, idempotency_key=idempotency_key
                )
                adapter = PreviewIdleAdapter(
                    host=host,
                    token=token,
                    target=target,
                    intent=intent,
                    record_id=record_id,
                    check=check,
                    admit=admit,
                )
                allow_mutation = not held or held[0].attempt < PREVIEW_IDLE_MAX_ATTEMPTS
                outcome = run_durable_provider_operation(
                    store=self.store,
                    scope=IDLE_SCOPE,
                    route_path=IDLE_ROUTE,
                    idempotency_key=idempotency_key,
                    request_fingerprint=_fingerprint(intent),
                    lease_owner=uuid.uuid4().hex,
                    response_trace_id="preview-idle:" + uuid.uuid4().hex,
                    adapter=adapter,
                    original_intent=intent,
                    lease_seconds=900,
                    allow_mutation=allow_mutation,
                )
                if (
                    allow_mutation
                    and outcome.record is not None
                    and outcome.record.state == "reconcile_required"
                    and outcome.record.attempt >= PREVIEW_IDLE_MAX_ATTEMPTS
                ):
                    _LOGGER.warning(
                        "Preview %s %s retries exhausted; retain operation %s for observation "
                        "or qualified refresh/destroy recovery",
                        preview_id,
                        intent["action"],
                        outcome.record.record_id,
                    )
