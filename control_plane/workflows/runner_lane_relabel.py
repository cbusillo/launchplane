"""Exact existing-runner label updates; never register or restart a runner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationBlocker
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPlan
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.merge_train_github import MergeTrainGitHubTransport
from control_plane.workflows.runner_lane_registration_executor import AuditPoster


@dataclass(frozen=True)
class LabelSnapshot:
    custom: tuple[str, ...]
    automatic: tuple[str, ...]


def _labels(payload: object) -> LabelSnapshot:
    if not isinstance(payload, dict) or not isinstance(payload.get("labels"), list):
        raise ValueError("runner labels response is unavailable")
    custom: list[str] = []
    automatic: list[str] = []
    seen: set[str] = set()
    for item in payload["labels"]:
        if not isinstance(item, dict):
            raise ValueError("runner label shape is unavailable")
        name, kind = item.get("name"), item.get("type")
        if not isinstance(name, str) or not name.strip() or kind not in ("custom", "read-only"):
            raise ValueError("runner label identity/type is unavailable")
        name = name.strip().lower()
        if name in seen:
            raise ValueError("runner label identity is ambiguous")
        seen.add(name)
        (custom if kind == "custom" else automatic).append(name)
    if "self-hosted" not in automatic:
        raise ValueError("automatic self-hosted label is missing")
    return LabelSnapshot(tuple(sorted(custom)), tuple(sorted(automatic)))


def _expected_labels(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(value.strip().lower() for value in values)
    if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("custom labels must be nonempty and unique")
    return tuple(sorted(normalized))


def deliver_terminal_audit(
    packet: dict[str, object], audit_poster: AuditPoster
) -> dict[str, object]:
    """Deliver only the retained evidence, never repeat the label mutation."""
    audit = RunnerLaneRegistrationAuditRecord.model_validate(packet.get("terminal_audit"))
    key = f"runner-lane-relabel:{audit.audit_record_key}:terminal"
    if (
        audit.request.operation != "relabel"
        or audit.status == "planned"
        or not audit.request.mutate
        or packet.get("terminal_idempotency_key") != key
    ):
        raise ValueError("retained terminal relabel audit/key is invalid")
    receipt = audit_poster(audit, key)
    records = receipt.get("records")
    if (
        receipt.get("status") != "accepted"
        or not isinstance(records, dict)
        or records.get("runner_lane_registration_audit_record_key") != audit.audit_record_key
    ):
        raise ValueError("service did not confirm the retained audit")
    return {
        "status": "audit_delivered",
        "write_attempted": False,
        "audit_record_key": audit.audit_record_key,
        "audit_status": audit.status,
    }


def execute_relabel(
    *,
    request: RunnerLaneRegistrationRequest,
    policy: RunnerLaneRegistrationPolicy,
    transport: MergeTrainGitHubTransport,
    inventory_reader: Callable[[], RunnerLaneInventory],
    audit_poster: AuditPoster,
    protected_lane_names: tuple[str, ...],
    protected_labels: tuple[str, ...],
) -> dict[str, object]:
    """Write at most once. Any uncertain result requires readback, never replay."""
    if request.operation != "relabel":
        raise ValueError("relabel requires the relabel lifecycle operation")
    if request.repository not in policy.allowed_repositories:
        raise ValueError("runner repository is not approved")
    if request.host_name not in policy.approved_hosts:
        raise ValueError("runner host is not approved")
    # This operation uses only the already-approved root, without touching it.
    if request.registration_root not in policy.allowed_registration_roots:
        raise ValueError("runner registration root is not approved")
    if not request.audit_record_key:
        raise ValueError("relabel requires an audit key")
    desired = _expected_labels(request.labels)
    expected = _expected_labels(request.expected_custom_labels)
    protected = {value.strip().lower() for value in protected_labels}
    if set(desired) & protected:
        raise ValueError("operations routing labels cannot be added")
    path = f"/repos/{request.repository}/actions/runners/{request.expected_runner_id}/labels"

    def post_audit(record: RunnerLaneRegistrationAuditRecord, key: str) -> None:
        receipt = audit_poster(record, key)
        if request.mutate:
            records = receipt.get("records")
            if (
                receipt.get("status") != "accepted"
                or not isinstance(records, dict)
                or records.get("runner_lane_registration_audit_record_key")
                != request.audit_record_key
            ):
                raise ValueError("service did not confirm the exact lifecycle audit")

    def snapshot() -> tuple[RunnerLaneInventory, LabelSnapshot]:
        inventory = inventory_reader()
        if inventory.repository.lower() != request.repository:
            raise ValueError("runner inventory repository changed")
        matches = [lane for lane in inventory.lanes if lane.name.lower() == request.lane_name]
        if len(matches) != 1 or matches[0].github_id != request.expected_runner_id:
            raise ValueError("runner name/ID changed or is ambiguous")
        if (
            matches[0].name.lower() in {name.strip().lower() for name in protected_lane_names}
            or {label.lower() for label in matches[0].labels} & protected
        ):
            raise ValueError("operations execution lane must remain unchanged")
        if matches[0].repository.lower() != request.repository or matches[0].busy:
            raise ValueError("runner repository changed or runner is busy")
        if matches[0].status != "online":
            raise ValueError("runner must be online")
        observed = _labels(transport.request(method="GET", path=path))
        if set(observed.custom) & protected:
            raise ValueError("operations execution lane must remain unchanged")
        if observed.custom != expected:
            raise ValueError("runner custom labels changed since review")
        if set(desired) & set(observed.automatic):
            raise ValueError("automatic labels cannot be supplied as custom labels")
        return inventory, observed

    pre_inventory, before = snapshot()
    plan = RunnerLaneRegistrationPlan(
        operation="relabel",
        status="ready" if request.mutate else "blocked",
        repository=request.repository,
        host_name=request.host_name,
        lane_name=request.lane_name,
        registration_root=request.registration_root,
        labels=desired,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
        blockers=()
        if request.mutate
        else (
            RunnerLaneRegistrationBlocker(
                code="mutate_not_requested", message="relabel dry-run; no labels changed"
            ),
        ),
        summary="exact existing-runner custom-label replacement",
    )
    planned = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status="planned",
        request=request,
        plan=plan,
        pre_inventory=pre_inventory,
        message="existing-runner label update planned",
    )
    result: dict[str, object] = {
        "status": "dry_run",
        "runner_id": request.expected_runner_id,
        "before_custom_labels": before.custom,
        "desired_custom_labels": desired,
        "automatic_labels": before.automatic,
        "write_attempted": False,
    }
    # Audit failure must occur before any mutation, and must not cause a retry.
    post_audit(planned, f"runner-lane-relabel:{request.audit_record_key}:planned")
    if not request.mutate:
        return result
    # Posting the audit can take time; do not rely on its earlier inventory.
    terminal_status: Literal["failed", "completed"] = "failed"
    post_inventory: RunnerLaneInventory | None = None
    try:
        _, refreshed = snapshot()
        if refreshed != before:
            raise ValueError("runner labels changed during audit delivery")
        result["write_attempted"] = True
        result["status"] = "mutation_reconciliation_required"
        transport.request(method="PUT", path=path, body={"labels": list(desired)})
        after = _labels(transport.request(method="GET", path=path))
        post_inventory = inventory_reader()
        matches = [lane for lane in post_inventory.lanes if lane.name.lower() == request.lane_name]
        if (
            post_inventory.repository.lower() != request.repository
            or len(matches) != 1
            or matches[0].github_id != request.expected_runner_id
            or matches[0].repository.lower() != request.repository
            or after.custom != desired
            or after.automatic != before.automatic
        ):
            raise ValueError("post-update identity/labels do not match")
        result["status"] = "completed"
        result["observed_custom_labels"] = after.custom
        terminal_status = "completed"
    except Exception:  # Adapter errors can contain sensitive response data.
        result["status"] = (
            "mutation_reconciliation_required"
            if result["write_attempted"]
            else "refused_before_write"
        )
    terminal = RunnerLaneRegistrationAuditRecord(
        audit_record_key=request.audit_record_key,
        status=terminal_status,
        request=request,
        plan=plan,
        pre_inventory=pre_inventory,
        post_inventory=post_inventory
        if post_inventory and post_inventory.repository.lower() == request.repository
        else None,
        message=str(result["status"]),
    )
    try:
        post_audit(terminal, f"runner-lane-relabel:{request.audit_record_key}:terminal")
    except Exception:
        result["mutation_status"] = result["status"]
        result["status"] = "audit_delivery_pending"
        result["terminal_audit"] = terminal.model_dump(mode="json")
        result["terminal_idempotency_key"] = (
            f"runner-lane-relabel:{request.audit_record_key}:terminal"
        )
    return result
