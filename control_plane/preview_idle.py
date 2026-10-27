from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
import logging
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any
import uuid

import yaml

from control_plane._odoo_copy_runtime_guard import preview_has_source_connection
from control_plane.contracts.preview_execution import (
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
from control_plane.disposable_lanes import require_disposable_lane
from control_plane.preview_gateway import PREVIEW_GATEWAY_SERVICE, PREVIEW_IDLE_SECONDS
from control_plane.preview_idle_runtime import (
    PREVIEW_IDLE_RUNTIME_PROGRAM,
    PREVIEW_IDLE_WAKE_TIMEOUT_SECONDS,
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
PREVIEW_IDLE_FAILURE_BACKOFF_SECONDS = 5 * 60


def idle_action(observation: dict[str, Any]) -> str:
    if observation.get("busy"):
        return ""
    gateway, services = observation["gateway"], observation["services"]
    if set(services) != {"web", "database", "script-runner"}:
        raise ValueError("Preview idle service evidence is incomplete.")
    if gateway["mode"] == "asleep":
        running = any(item["running"] for item in services.values())
        wanted = gateway["requested"] and gateway["idle_seconds"] < PREVIEW_IDLE_SECONDS
        if wanted and (gateway["wake_record_id"] or not running):
            return "awake"
        return "sleep" if running else ""
    if (
        gateway["mode"] == "awake"
        and gateway["active"] == 0
        and not gateway["requested"]
        and gateway["idle_seconds"] >= PREVIEW_IDLE_SECONDS
        and all(item["running"] and item["healthy"] for item in services.values())
    ):
        return "sleep"
    return ""


class PreviewIdleClient:
    def __init__(
        self,
        *,
        host: str,
        token: str,
        target: dict[str, Any],
        check: Callable[[], None],
        admit: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        self.host, self.token, self.target = host, token, target
        self.check, self.admit = check, admit
        self.dispatched = False

    def read(
        self, action: str = "observe", control: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.dispatched = False
        self.check()
        frames: list[dict[str, object]] = []

        def dispatch(effect: Callable[[], None]) -> None:
            with self.admit() if action in {"sleep", "awake"} else nullcontext():
                self.check()
                self.dispatched = True
                effect()

        _read_remote(
            host=self.host,
            token=self.token,
            server_id=self.target["server_id"],
            program=PREVIEW_IDLE_RUNTIME_PROGRAM,
            payload={**self.target["spec"], "action": action, "control": control or {}},
            receive=frames.append,
            timeout_seconds=PREVIEW_IDLE_WAKE_TIMEOUT_SECONDS + 120 if action == "awake" else 240,
            dispatch_guard=dispatch,
        )
        self.check()
        result = frames[0].get("result") if len(frames) == 1 else None
        if not isinstance(result, dict) or frames[0].get("type") != "complete":
            raise NativeSourceUnavailable("Preview idle observation is unavailable.")
        return result


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
        self._retry_after: dict[str, float] = {}

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
                if monotonic() < self._retry_after.get(preview.preview_id, 0):
                    continue
                try:
                    self._run_preview(profile, preview.preview_id, host, token)
                    self._retry_after.pop(preview.preview_id, None)
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
        if self.store.list_held_provider_target_reservations(provider_target_key=key):
            return
        observer = PreviewIdleClient(host=host, token=token, target=target, check=lambda: None)
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

                if self.store.list_held_provider_target_reservations(provider_target_key=key):
                    return
                client = PreviewIdleClient(
                    host=host, token=token, target=target, check=check, admit=admit
                )
                observation = client.read()
                action = idle_action(observation)
                if not action:
                    return
                gateway = observation["gateway"]
                record_id = "mutation-reservation-" + uuid.uuid4().hex
                control = (
                    {
                        "boot_id": gateway["boot_id"],
                        "sequence": gateway["sequence"],
                        "record_id": gateway["sleep_record_id"]
                        if gateway["mode"] == "asleep"
                        else record_id,
                    }
                    if action == "sleep"
                    else {
                        "sleep_record_id": gateway["sleep_record_id"],
                        "record_id": gateway["wake_record_id"] or record_id,
                    }
                )
                try:
                    client.read(action, {**control, "action": action})
                except Exception:
                    if client.dispatched:
                        self._retry_after[preview_id] = (
                            monotonic() + PREVIEW_IDLE_FAILURE_BACKOFF_SECONDS
                        )
                    raise
