from collections.abc import Callable
import logging
from time import monotonic
from typing import Any

import yaml

from control_plane.contracts.preview_inventory_scan_record import PreviewSleepObservation
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_execution import odoo_preview_target_key
from control_plane.dokploy import api
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable
from control_plane.preview_gateway import PREVIEW_GATEWAY_SERVICE
from control_plane.preview_idle import PreviewCannotSleep, require_preview_idle_binding
from control_plane.preview_idle_runtime import PREVIEW_IDLE_RUNTIME_PROGRAM
from control_plane.preview_lifecycle_cleanup_routes import PreviewLifecycleSweepCancelled
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import preview_slug_from_application_name

_LOGGER = logging.getLogger(__name__)
PREVIEW_SLEEP_READ_BUDGET_SECONDS = 15
PREVIEW_SLEEP_POLL_TIMEOUT_SECONDS = 5


def poll_preview_sleep(
    *, host: str, token: str, target: dict[str, Any], timeout: int
) -> dict[str, Any]:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=target["server_id"],
        program=PREVIEW_IDLE_RUNTIME_PROGRAM,
        payload={**target["spec"], "action": "poll"},
        receive=frames.append,
        timeout_seconds=timeout,
        dispatch_guard=lambda dispatch: dispatch(),
    )
    result = frames[0].get("result") if len(frames) == 1 else None
    if not isinstance(result, dict) or frames[0].get("type") != "complete":
        raise NativeSourceUnavailable("Preview sleep observation is unavailable.")
    return result


def collect_preview_sleep_observations(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    host: str,
    token: str,
    inventory: tuple[tuple[str, str, str], ...],
    prefix: str,
    should_stop: Callable[[], bool] = lambda: False,
) -> tuple[PreviewSleepObservation, ...]:
    deadline = monotonic() + PREVIEW_SLEEP_READ_BUDGET_SECONDS
    records_available = True
    try:
        previews = store.list_preview_records(context_name=profile.preview.context, limit=None)
    except Exception:
        _LOGGER.warning("Preview sleep records are unavailable for %s", profile.product)
        previews = ()
        records_available = False
    observations: list[PreviewSleepObservation] = []
    for _, compose_id, name in inventory:
        if should_stop():
            raise PreviewLifecycleSweepCancelled()
        slug = preview_slug_from_application_name(
            app_name_suffix=profile.preview.app_name_suffix,
            app_name_prefix=prefix,
            application_name=name,
        )
        if not slug:
            continue
        state = PreviewSleepObservation(
            slug=slug,
            state="unknown",
            reason="Sleep eligibility could not be verified; inspect preview diagnostics.",
        )
        if monotonic() >= deadline:
            observations.append(
                state.model_copy(update={"reason": "Sleep observation exceeded its read budget."})
            )
            continue
        try:
            if not records_available:
                raise NativeSourceUnavailable("Preview records are unavailable.")
            payload = api.dokploy_request(
                host=host,
                token=token,
                path="/api/compose.one",
                query={"composeId": compose_id},
                timeout_seconds=min(
                    PREVIEW_SLEEP_POLL_TIMEOUT_SECONDS, max(0.1, deadline - monotonic())
                ),
            )
            if not isinstance(payload, dict):
                raise NativeSourceUnavailable("Preview target read is unavailable.")
            declaration = yaml.safe_load(str(payload.get("composeFile") or ""))
            if not isinstance(declaration, dict) or PREVIEW_GATEWAY_SERVICE not in declaration.get(
                "services", {}
            ):
                raise PreviewCannotSleep(
                    "The idle gateway is missing; an ordinary rebuild installs it."
                )
            matching = [
                preview
                for preview in previews
                if preview.anchor_repo in {profile.repository, profile.repository.partition("/")[2]}
                and profile.preview.slug_template.format(number=preview.anchor_pr_number) == slug
                and preview.state == "active"
            ]
            if len(matching) != 1:
                raise PreviewCannotSleep(
                    "No unique serving preview record is available; inspect deployment recovery."
                )
            target = require_preview_idle_binding(
                store,
                profile,
                matching[0].preview_id,
                host,
                token,
                inventory=inventory,
                payload=payload,
            )
            timeout = min(PREVIEW_SLEEP_POLL_TIMEOUT_SECONDS, int(deadline - monotonic()))
            if timeout <= 0 or should_stop():
                raise NativeSourceUnavailable(
                    "Sleep observation budget or shutdown boundary reached."
                )
            observation = poll_preview_sleep(host=host, token=token, target=target, timeout=timeout)
            services = observation["services"]
            if set(services) != {"web", "database", "script-runner"}:
                raise ValueError("Incomplete application state")
            if observation["gateway"]["mode"] == "asleep" and all(
                not item["running"] for item in services.values()
            ):
                state = PreviewSleepObservation(slug=slug, state="asleep")
            elif store.list_held_provider_target_reservations(
                provider_target_key=odoo_preview_target_key(
                    context=target["context"], compose_name=target["compose_name"]
                )
            ):
                raise PreviewCannotSleep(
                    "An operation is holding this preview; inspect its recovery status."
                )
            elif observation["gateway"]["mode"] == "awake" and all(
                item["running"] and item["healthy"] for item in services.values()
            ):
                state = PreviewSleepObservation(slug=slug, state="awake")
            else:
                raise PreviewCannotSleep(
                    "Application services are stopped, unhealthy or changing sleep state."
                )
        except PreviewCannotSleep as error:
            state = state.model_copy(update={"state": "cannot_sleep", "reason": str(error)})
        except Exception:
            _LOGGER.warning("Preview sleep observation is unavailable for %s", slug)
        observations.append(state)
    if should_stop():
        raise PreviewLifecycleSweepCancelled()
    return tuple(observations)
