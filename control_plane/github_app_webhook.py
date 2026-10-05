"""Receive Launchplane's GitHub App webhook and record product reconcile requests.

A delivery only says which target to look at again. Nothing in the body is
trusted beyond choosing that target; the reconcile re-reads every fact it acts
on from GitHub.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import JsonValue

import click

from control_plane import secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import (
    GitHubAppWebhookDeliveryRecord,
    ProductReconcileTarget,
)
from control_plane.contracts.repository_inventory import (
    RepositoryInventoryRecord,
    normalize_repository,
)
from control_plane.product_repository_identity import stored_identity_matches_inventory
from control_plane.product_config_authority_events import (
    config_authority_event_supported,
    config_authority_event_request,
    request_product_config_authority_event,
)
from control_plane.repository_inventory import get_repository_inventory_read_model
from control_plane.workflows.launchplane import verify_github_webhook_signature
from control_plane.workflows.ship import utc_now_timestamp

GITHUB_APP_WEBHOOK_ROUTE = "/v1/github/app-webhook"
GITHUB_APP_WEBHOOK_SECRET_INTEGRATION = "github_app_webhook"
GITHUB_APP_WEBHOOK_SECRET_CONTEXT = "launchplane"
GITHUB_APP_WEBHOOK_SECRET_BINDING_KEY = "webhook_secret"
PRODUCT_BUILD_WORKFLOW_PATH = ".github/workflows/build.yml"
_PREVIEW_PULL_REQUEST_ACTIONS = frozenset(
    {
        "opened",
        "reopened",
        "synchronize",
        "labeled",
        "unlabeled",
        "closed",
    }
)

GitHubAppWebhookStatus = Literal["recorded", "duplicate", "ignored"]


class GitHubAppWebhookStore(Protocol):
    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...

    def record_github_app_webhook_delivery(
        self,
        delivery: GitHubAppWebhookDeliveryRecord,
        targets: tuple[ProductReconcileTarget, ...],
        requested_at: str,
    ) -> Literal["recorded", "duplicate"]: ...


def _resolve_managed_webhook_secret() -> str:
    return secrets.resolve_context_secret_value(
        integration=GITHUB_APP_WEBHOOK_SECRET_INTEGRATION,
        context_name=GITHUB_APP_WEBHOOK_SECRET_CONTEXT,
        binding_key=GITHUB_APP_WEBHOOK_SECRET_BINDING_KEY,
    )


def _wake_merge_train(record_store: object, event: str, payload: dict[str, object]) -> bool:
    from control_plane.merge_train_events import wake_merge_train_for_event

    return wake_merge_train_for_event(record_store, event, payload)


@dataclass(frozen=True)
class GitHubAppWebhookDependencies:
    webhook_secret: Callable[[], str] = _resolve_managed_webhook_secret
    verify_signature: Callable[..., None] = verify_github_webhook_signature
    now: Callable[[], str] = utc_now_timestamp
    wake_merge_train: Callable[[object, str, dict[str, object]], bool] = _wake_merge_train
    config_authority: Callable[
        [object, RepositoryInventoryRecord, str, dict[str, object]], dict[str, JsonValue]
    ] = request_product_config_authority_event


def handle_github_app_webhook_request(
    payload_bytes: bytes,
    event_name: str,
    delivery_id: str,
    signature_header: str,
    record_store: object,
    control_plane_root: Path,
    trace_id: str,
    *,
    dependencies: GitHubAppWebhookDependencies | None = None,
) -> tuple[int, dict[str, object]]:
    del control_plane_root
    dependencies = dependencies or GitHubAppWebhookDependencies()

    def error(status: int, code: str, message: str) -> tuple[int, dict[str, object]]:
        return status, {
            "status": "error",
            "trace_id": trace_id,
            "error": {"code": code, "message": message},
        }

    config_authority: dict[str, JsonValue] = {}

    def accepted(
        status: GitHubAppWebhookStatus, *, reason: str = "", target_keys: tuple[str, ...] = ()
    ) -> tuple[int, dict[str, object]]:
        return 202, {
            "status": "accepted",
            "trace_id": trace_id,
            "result": {
                "status": status,
                "reason": reason,
                "target_keys": list(target_keys),
                **({"config_authority": config_authority} if config_authority else {}),
            },
        }

    normalized_delivery_id = delivery_id.strip()
    if not normalized_delivery_id:
        return error(400, "invalid_payload", "GitHub delivery id is required.")
    try:
        secret = dependencies.webhook_secret().strip()
    except Exception:
        secret = ""
    if not secret:
        return error(
            503,
            "github_app_webhook_unconfigured",
            "The GitHub App webhook secret is not configured.",
        )
    try:
        dependencies.verify_signature(
            payload_bytes=payload_bytes,
            signature_header=signature_header,
            secret=secret,
        )
    except click.ClickException:
        return error(401, "invalid_signature", "GitHub webhook signature is invalid.")
    try:
        payload = json.loads(payload_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error(400, "invalid_payload", "GitHub webhook body is invalid JSON.")
    if not isinstance(payload, dict):
        return error(400, "invalid_payload", "GitHub webhook body must be an object.")

    normalized_event = event_name.strip().lower()
    action = _string(payload, "action").lower()
    # Only verified deliveries can wake a worker. A failed notification never
    # prevents product reconciliation; the train's timed sweep recovers it.
    try:
        train_woken = dependencies.wake_merge_train(record_store, normalized_event, payload)
    except Exception:
        logging.getLogger(__name__).warning(
            "Merge train event wake failed; timed sweep will retry."
        )
        train_woken = False
    target_shapes = _target_shapes(event_name=normalized_event, action=action, payload=payload)
    scan_event = config_authority_event_supported(normalized_event, payload)
    if not target_shapes and not scan_event:
        return accepted(
            "ignored", reason="merge_train_woken" if train_woken else "unsupported_event"
        )
    repository_id = _positive_id(_mapping(payload, "repository"), "id")
    if not repository_id:
        return accepted("ignored", reason="missing_repository")
    if not hasattr(record_store, "record_github_app_webhook_delivery"):
        return error(503, "github_app_webhook_unavailable", "Webhook storage is unavailable.")
    store = cast(GitHubAppWebhookStore, record_store)
    try:
        if scan_event:
            inventory = get_repository_inventory_read_model(
                repository_id=repository_id, store=store
            ).current_record
            if inventory is None or inventory.inventory_state != "tracked":
                return accepted("ignored", reason="repository_not_mapped")
            try:
                config_authority = dependencies.config_authority(
                    record_store, inventory, normalized_event, payload
                )
            except Exception:
                config_authority = {
                    "status": "pending",
                    "request_error": "source_request_unavailable",
                    "request": config_authority_event_request(normalized_event, payload),
                }
        product, unmapped_reason = _product_for_repository(store=store, repository_id=repository_id)
    except Exception:
        logging.getLogger(__name__).warning("GitHub source event verification unavailable.")
        return error(
            503, "github_source_evidence_unavailable", "Source verification is unavailable."
        )
    if not product and not config_authority:
        return accepted("ignored", reason=unmapped_reason)
    if not config_authority and not target_shapes:
        return accepted("ignored", reason="config_authority_disabled")
    targets = tuple(
        ProductReconcileTarget(
            product=product, target_kind=target_kind, pull_request_number=pull_request_number
        )
        for target_kind, pull_request_number in target_shapes
        if product
    )
    target_keys = tuple(dict.fromkeys(target.target_key for target in targets))
    received_at = dependencies.now()
    delivery = GitHubAppWebhookDeliveryRecord(
        delivery_id=normalized_delivery_id,
        event=normalized_event,
        action=action,
        repository_id=repository_id,
        received_at=received_at,
        target_keys=target_keys,
        config_authority={
            key: value for key, value in config_authority.items() if key != "request"
        },
        config_authority_request=cast(dict[str, JsonValue], config_authority.get("request", {})),
        config_authority_state="pending" if config_authority.get("status") == "pending" else "",
    )
    try:
        status = store.record_github_app_webhook_delivery(delivery, targets, received_at)
    except Exception:
        return error(503, "github_app_webhook_unavailable", "Webhook storage is unavailable.")
    return accepted(status, target_keys=target_keys)


def _target_shapes(
    *, event_name: str, action: str, payload: dict[str, object]
) -> tuple[tuple[Literal["testing", "preview"], int | None], ...]:
    if event_name == "workflow_run":
        workflow_run = _mapping(payload, "workflow_run")
        if action != "completed" or _string(workflow_run, "path") != PRODUCT_BUILD_WORKFLOW_PATH:
            return ()
        trigger = _string(workflow_run, "event")
        if trigger == "push":
            return (("testing", None),)
        if trigger == "pull_request":
            pull_requests = workflow_run.get("pull_requests") if workflow_run else None
            if not isinstance(pull_requests, list):
                return ()
            numbers = (
                _positive_int(cast(dict[str, object], entry), "number")
                for entry in pull_requests
                if isinstance(entry, dict)
            )
            return tuple(("preview", number) for number in numbers if number is not None)
        return ()
    retargeted = action == "edited" and _mapping(_mapping(payload, "changes"), "base") is not None
    if event_name == "pull_request" and (action in _PREVIEW_PULL_REQUEST_ACTIONS or retargeted):
        # A retarget adds no commit, so only this event says again whether an
        # acceptance carried on the old base still applies.
        number = _positive_int(payload, "number")
        return (("preview", number),) if number is not None else ()
    return ()


def _product_for_repository(*, store: GitHubAppWebhookStore, repository_id: str) -> tuple[str, str]:
    """The one active product whose repository the inventory names for this id, or why not.

    The repository inventory is the authority for the immutable id; ids stored on
    the profile are only a cross-check and must agree with it.
    """
    inventory = get_repository_inventory_read_model(repository_id=repository_id, store=store)
    current = inventory.current_record
    if current is None or current.inventory_state != "tracked":
        return "", "repository_not_mapped"
    matches = tuple(
        profile
        for profile in store.list_product_profile_records()
        if profile.is_active and _normalized_repository(profile.repository) == current.repository
    )
    if len(matches) != 1:
        return "", "repository_not_mapped"
    if not stored_identity_matches_inventory(matches[0], current):
        return "", "repository_identity_mismatch"
    return matches[0].product, ""


def _normalized_repository(repository: str) -> str:
    try:
        return normalize_repository(repository, "repository")
    except ValueError:
        return ""


def _mapping(mapping: dict[str, object] | None, key: str) -> dict[str, object] | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def _positive_int(mapping: dict[str, object] | None, key: str) -> int | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _positive_id(mapping: dict[str, object] | None, key: str) -> str:
    value = _positive_int(mapping, key)
    return str(value) if value is not None else ""
