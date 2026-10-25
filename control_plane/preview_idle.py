from collections.abc import Callable
from contextlib import ExitStack
import hashlib
import json
import logging
from pathlib import Path
from threading import Event, Thread
from typing import Any
import uuid

import yaml

from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.preview_execution import (
    odoo_preview_reconciliation_key,
    odoo_preview_target_key,
    preview_execution_lock_name,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import runtime_identity_env
from control_plane.dokploy import api, source
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.memory_capacity import preview_host_memory_floor
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable
from control_plane.dokploy.preview_inventory import complete_preview_compose_inventory
from control_plane.drivers.registry import product_profile_uses_odoo_driver
from control_plane.preview_gateway import PREVIEW_GATEWAY_SERVICE, PREVIEW_IDLE_SECONDS
from control_plane.preview_idle_runtime import PREVIEW_IDLE_RUNTIME_PROGRAM
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationUnknownError,
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


IDLE_SCOPE = "launchplane:preview-idle:v1"
IDLE_ROUTE = "service:preview-idle"
_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self.host, self.token = host, token
        self.target, self.intent = target, intent
        self.record_id, self.check = record_id, check

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
            self.check()
            if lease is not None:
                lease.checkpoint_effect("preview_idle_" + action)
            effect()

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
            and gateway["wake_record_id"] == self.record_id
            and all(item["running"] and item["healthy"] for item in services.values())
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
        )
        return ProviderObservation("absent" if retry else "unknown", retry_safe=retry)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        result = self.read(self.intent["action"], lease)
        if result.get("capacity") is False:
            raise ProviderMutationUnknownError(
                "Preview wake is waiting for capacity under its original operation."
            )
        return ProviderMutationOutcome(response_status_code=200, response_payload=result)


class PreviewIdleScheduler:
    def __init__(self, store: PostgresRecordStore, *, control_plane_root: Path) -> None:
        self.store, self.control_plane_root = store, control_plane_root
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="preview-idle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
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
        if (
            not profile.is_active
            or not profile.preview.enabled
            or not product_profile_uses_odoo_driver(profile)
        ):
            return None
        preview = self.store.read_preview_record(preview_id)
        if preview.context != profile.preview.context or preview.anchor_repo not in {
            profile.repository,
            profile.repository.partition("/")[2],
        }:
            return None
        generation_id = preview.serving_generation_id
        if (
            preview.state != "active"
            or not generation_id
            or preview.active_generation_id != generation_id
        ):
            return None
        generation = self.store.read_preview_generation_record(generation_id)
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
            return None
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
            for entry in complete_preview_compose_inventory(host=host, token=token)
            if entry[2] == name
        ]
        if len(matches) != 1 or identity.instance != name:
            return None
        compose_id = matches[0]
        for registered in self.store.list_dokploy_target_id_records():
            if registered.target_id == compose_id:
                if (registered.context, registered.instance) != (profile.preview.context, slug):
                    return None
                target = self.store.read_dokploy_target_record(
                    context_name=registered.context, instance_name=registered.instance
                )
                if target.policies.staff_testing_hold is not None:
                    return None
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
            return None
        env = api.parse_dokploy_env_text(str(payload.get("env") or ""))
        raw_identity = runtime_identity_env(identity)["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]
        if env.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON") != raw_identity:
            return None
        if any(
            value and key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_"))
            for key, value in env.items()
        ):
            return None
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
            "spec": {
                "generation": {
                    "instance": "preview",
                    "namespace": str(payload.get("appName") or ""),
                },
                "compose_project": str(payload.get("appName") or ""),
                "database_name": env.get("ODOO_DB_NAME", ""),
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
                    host=host,
                    token=token,
                    server_id=str(payload.get("serverId") or ""),
                    record_store=self.store,
                ),
                "disk_reserve": budget.reserve_bytes if budget else 0,
            },
        }

    def run_once(self) -> None:
        host, token = source.read_dokploy_config(control_plane_root=self.control_plane_root)
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
                held = self.store.list_held_provider_target_reservations(provider_target_key=key)
                intent: dict[str, Any] = {}
                idempotency_key = ""
                if held:
                    if len(held) != 1 or (held[0].scope, held[0].route_path) != (
                        IDLE_SCOPE,
                        IDLE_ROUTE,
                    ):
                        return
                    intent = self.store.read_original_mutation_intent(held[0])
                    if intent.get("target") != target or intent.get("action") not in {
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
                        original = self.store.read_idempotency_record_by_id(
                            gateway["sleep_record_id"]
                        )
                        if (
                            original is None
                            or original.state != "completed"
                            or (original.scope, original.route_path) != (IDLE_SCOPE, IDLE_ROUTE)
                        ):
                            return
                        original_intent = self.store.read_original_mutation_intent(original)
                        if (
                            original_intent.get("target") != target
                            or original_intent.get("action") != "sleep"
                        ):
                            return
                    control = (
                        {"boot_id": gateway["boot_id"], "sequence": gateway["sequence"]}
                        if action == "sleep"
                        else {"sleep_record_id": gateway["sleep_record_id"]}
                    )
                    intent = {"target": target, "action": action, "control": control}
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
                )
                run_durable_provider_operation(
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
                )
