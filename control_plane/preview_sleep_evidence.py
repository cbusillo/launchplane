from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
from time import monotonic
from typing import Any

import yaml

from control_plane.contracts.preview_inventory_scan_record import (
    PreviewSleepBinding,
    PreviewSleepEvidence,
    PreviewSleepObservation,
    PreviewSleepReceipts,
    PreviewSleepServices,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_execution import odoo_preview_target_key
from control_plane.dokploy import api
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable
from control_plane.preview_gateway import PREVIEW_GATEWAY_SERVICE
from control_plane.preview_idle import PreviewCannotSleep, require_preview_idle_binding
from control_plane.preview_idle_runtime import PREVIEW_IDLE_RUNTIME_PROGRAM
from control_plane.preview_inventory_evidence import PREVIEW_INVENTORY_INTERVAL_SECONDS
from control_plane.preview_lifecycle_cleanup_routes import PreviewLifecycleSweepCancelled
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import preview_slug_from_application_name

_LOGGER = logging.getLogger(__name__)
PREVIEW_SLEEP_READ_BUDGET_SECONDS = PREVIEW_INVENTORY_INTERVAL_SECONDS // 5
PREVIEW_SLEEP_POLL_TIMEOUT_SECONDS = PREVIEW_SLEEP_READ_BUDGET_SECONDS // 4


def poll_preview_sleep(
    *, host: str, token: str, target: dict[str, Any], timeout: int
) -> dict[str, Any]:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=target["server_id"],
        program=PREVIEW_IDLE_RUNTIME_PROGRAM,
        payload={**target["spec"], "action": "poll", "retain_evidence": True},
        receive=frames.append,
        timeout_seconds=timeout,
        dispatch_guard=lambda dispatch: dispatch(),
    )
    result = frames[0].get("result") if len(frames) == 1 else None
    if not isinstance(result, dict) or frames[0].get("type") != "complete":
        raise NativeSourceUnavailable("Preview sleep observation is unavailable.")
    return result


def _sleep_phase(evidence: PreviewSleepEvidence) -> str:
    receipts = evidence.receipts
    agreements = (
        receipts.identity_agrees,
        receipts.mode_agrees,
        receipts.sleep_agrees,
        receipts.wake_agrees,
    )
    if False in agreements:
        return "inconsistent"
    services = (evidence.services.web, evidence.services.database, evidence.services.script_runner)
    if any(item.running is None for item in services):
        return "unknown"
    if evidence.gateway_mode == "awake":
        if any(item.running is False or item.healthy is False for item in services):
            return "inconsistent"
        return "awake" if all(item.healthy is True for item in services) else "unknown"
    if evidence.gateway_mode != "asleep" or not all(agreements):
        return "unknown"
    if receipts.sleep_present is None or receipts.wake_present is None:
        return "unknown"
    if not receipts.sleep_present:
        return "inconsistent"
    if receipts.wake_present:
        return "waking"
    return "stopping" if any(item.running for item in services) else "asleep"


def _retain_poll_evidence(
    observation: dict[str, Any], target: dict[str, Any]
) -> PreviewSleepEvidence:
    evidence = PreviewSleepEvidence(coverage="partial")
    gateway = observation.get("gateway", {})
    mode = gateway.get("mode")
    if mode in {"awake", "asleep"}:
        evidence.gateway_mode = mode
    requested, active = gateway.get("requested"), gateway.get("active")
    evidence.activity_requested = requested if type(requested) is bool else None
    evidence.active_connections = active if type(active) is int and active >= 0 else None
    projected = observation.get("evidence")
    if not isinstance(projected, dict):
        return evidence
    try:
        binding = PreviewSleepBinding.model_validate(
            {**projected["binding"], "provider_target_id": target["compose_id"]}
        )
        expected = json.loads(target["spec"]["identity"])
        if any(
            value != expected.get("preview_generation_id" if key == "generation_id" else key)
            for key, value in binding.model_dump().items()
            if key != "provider_target_id"
        ):
            raise ValueError("Observed binding differs from selected target")
        evidence.binding = binding
        evidence.services = PreviewSleepServices.model_validate(projected["services"])
        evidence.receipts = PreviewSleepReceipts.model_validate(projected["receipts"])
    except (ValueError, TypeError, KeyError):
        _LOGGER.warning("Preview sleep poll projection is incomplete")
        return evidence
    services = (evidence.services.web, evidence.services.database, evidence.services.script_runner)
    if (
        evidence.gateway_mode != "unknown"
        and evidence.activity_requested is not None
        and evidence.active_connections is not None
        and all(item.running is not None and item.healthy is not None for item in services)
        and evidence.receipts.persisted_state == "observed"
        and all(value is not None for value in evidence.receipts.model_dump().values())
    ):
        evidence.coverage = "complete"
    return evidence.model_copy(update={"phase": _sleep_phase(evidence)})


def collect_preview_sleep_observations(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    host: str,
    token: str,
    inventory: tuple[tuple[str, str, str], ...],
    prefix: str,
    should_stop: Callable[[], bool] = lambda: False,
    scan_slot: int = 0,
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
    candidates = tuple(
        (compose_id, slug)
        for _, compose_id, name in inventory
        if (
            slug := preview_slug_from_application_name(
                app_name_suffix=profile.preview.app_name_suffix,
                app_name_prefix=prefix,
                application_name=name,
            )
        )
    )
    offset = scan_slot % len(candidates) if candidates else 0
    for compose_id, slug in candidates[offset:] + candidates[:offset]:
        if should_stop():
            raise PreviewLifecycleSweepCancelled()
        state = PreviewSleepObservation(
            slug=slug,
            state="unknown",
            reason="Sleep eligibility could not be verified; inspect preview diagnostics.",
            evidence=PreviewSleepEvidence(coverage="not_attempted"),
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
            if payload.get("composeId") != compose_id:
                raise NativeSourceUnavailable("Preview target identity differs.")
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
            state.evidence = _retain_poll_evidence(observation, target)
            services = observation["services"]
            if set(services) != {"web", "database", "script-runner"}:
                raise ValueError("Incomplete application state")
            if observation["gateway"]["mode"] == "asleep" and all(
                not item["running"] for item in services.values()
            ):
                state = state.model_copy(update={"state": "asleep", "reason": ""})
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
                state = state.model_copy(update={"state": "awake", "reason": ""})
            else:
                raise PreviewCannotSleep(
                    "Application services are stopped, unhealthy or changing sleep state."
                )
        except PreviewCannotSleep as error:
            state = state.model_copy(update={"state": "cannot_sleep", "reason": str(error)})
        except Exception:
            _LOGGER.warning("Preview sleep observation is unavailable for %s", slug)
            if state.evidence is not None and state.evidence.coverage == "not_attempted":
                state.evidence.coverage = "unavailable"
        evidence = state.evidence
        if evidence is not None:
            evidence.completed_at = datetime.now(timezone.utc).isoformat()
        observations.append(state)
    if should_stop():
        raise PreviewLifecycleSweepCancelled()
    return tuple(observations)
