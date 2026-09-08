from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackCancellationInstructionRecord,
    EveryCodeFeedbackHandoffReceiptRecord,
    EveryCodeFeedbackLaunchBinding,
    EveryCodeFeedbackLaunchObservationRecord,
    EveryCodeFeedbackProcessBindingRecord,
    EveryCodeFeedbackStartupReceiptRecord,
    parse_every_code_feedback_timestamp,
)

LaunchPhase = Literal[
    "launch_pending",
    "registered",
    "released",
    "started",
    "handoff_accepted",
    "delivery_unknown",
    "reconcile_required",
    "cancelled",
    "cancellation_unknown",
]
LaunchAction = Literal[
    "register",
    "release",
    "record_started",
    "record_handoff_accepted",
    "adopt",
    "record_proven_absence",
    "cancel_gate",
    "request_exact_cancellation",
    "no_action",
]


@dataclass(frozen=True)
class EveryCodeFeedbackLaunchDecision:
    """Pure recommendation; `phase` is advisory when `action` is `no_action`."""

    action: LaunchAction
    phase: LaunchPhase
    reason: str
    process_binding: EveryCodeFeedbackProcessBindingRecord | None = None
    cancellation: EveryCodeFeedbackCancellationInstructionRecord | None = None


class EveryCodeFeedbackLaunchAdapter(Protocol):
    def inspect(
        self, binding: EveryCodeFeedbackLaunchBinding
    ) -> EveryCodeFeedbackLaunchObservationRecord | None: ...


class UnavailableEveryCodeFeedbackLaunchAdapter:
    """Inert default with no inspection, launch, signalling, or termination capability."""

    def inspect(
        self, binding: EveryCodeFeedbackLaunchBinding
    ) -> EveryCodeFeedbackLaunchObservationRecord | None:
        del binding
        return None


def deterministic_every_code_feedback_session_name(binding: EveryCodeFeedbackLaunchBinding) -> str:
    request_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", binding.request_id).strip("-_")
    digest = hashlib.sha256(
        json.dumps(binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    prefix = f"every-code-feedback-{request_slug or 'request'}"
    return f"{prefix[:54].rstrip('-_')}-{digest}"


def decide_binding_registration(
    *,
    expected: EveryCodeFeedbackLaunchBinding,
    existing: EveryCodeFeedbackProcessBindingRecord | None,
    proposed: EveryCodeFeedbackProcessBindingRecord,
) -> EveryCodeFeedbackLaunchDecision:
    if proposed.binding != expected:
        return _decision("no_action", "reconcile_required", "launch_binding_mismatch")
    if proposed.session_name != deterministic_every_code_feedback_session_name(expected):
        return _decision("no_action", "reconcile_required", "session_name_mismatch")
    if existing is None:
        return _decision("register", "registered", "binding_registered", proposed)
    if existing == proposed:
        return _decision("no_action", "registered", "binding_registration_replayed", existing)
    return _decision("no_action", "reconcile_required", "binding_registration_conflict")


def decide_gate_release(
    *,
    expected: EveryCodeFeedbackLaunchBinding,
    process_binding: EveryCodeFeedbackProcessBindingRecord | None,
    pull_request_open: bool,
    lease_current: bool,
    authorized: bool,
) -> EveryCodeFeedbackLaunchDecision:
    if not pull_request_open:
        return _decision("cancel_gate", "cancelled", "pull_request_closed_before_release")
    if not lease_current:
        return _decision("no_action", "reconcile_required", "lease_not_current")
    if not authorized:
        return _decision("no_action", "reconcile_required", "authorization_unavailable")
    if process_binding is None or process_binding.binding != expected:
        return _decision("no_action", "reconcile_required", "exact_binding_not_registered")
    if process_binding.session_name != deterministic_every_code_feedback_session_name(expected):
        return _decision("no_action", "reconcile_required", "registered_session_name_mismatch")
    return _decision("release", "released", "exact_binding_released", process_binding)


def decide_startup_receipt(
    *,
    expected_operation_id: str,
    expected_process: EveryCodeFeedbackProcessBindingRecord,
    receipt: EveryCodeFeedbackStartupReceiptRecord,
) -> EveryCodeFeedbackLaunchDecision:
    if (
        receipt.operation_id != expected_operation_id
        or receipt.binding != expected_process.binding
        or receipt.process_binding_sha256 != expected_process.process_binding_sha256
        or parse_every_code_feedback_timestamp(receipt.recorded_at)
        < parse_every_code_feedback_timestamp(expected_process.registered_at)
    ):
        return _decision("no_action", "delivery_unknown", "startup_receipt_mismatch")
    return _decision("record_started", "started", "wrapper_started", expected_process)


def decide_handoff_receipt(
    *,
    expected_operation_id: str,
    expected_process: EveryCodeFeedbackProcessBindingRecord,
    expected_handoff_sha256: str,
    receipt: EveryCodeFeedbackHandoffReceiptRecord,
) -> EveryCodeFeedbackLaunchDecision:
    if receipt.operation_id != expected_operation_id or receipt.binding != expected_process.binding:
        return _decision("no_action", "delivery_unknown", "handoff_binding_mismatch")
    if parse_every_code_feedback_timestamp(
        receipt.recorded_at
    ) < parse_every_code_feedback_timestamp(expected_process.registered_at):
        return _decision("no_action", "delivery_unknown", "handoff_receipt_predates_registration")
    if receipt.handoff_sha256 != expected_handoff_sha256:
        return _decision("no_action", "delivery_unknown", "handoff_digest_mismatch")
    return _decision(
        "record_handoff_accepted", "handoff_accepted", "exact_handoff_accepted", expected_process
    )


def decide_reconciliation(
    *,
    expected_process: EveryCodeFeedbackProcessBindingRecord,
    observation: EveryCodeFeedbackLaunchObservationRecord | None,
) -> EveryCodeFeedbackLaunchDecision:
    if observation is None:
        return _decision("no_action", "reconcile_required", "inspection_capability_unavailable")
    if observation.binding != expected_process.binding:
        return _decision("no_action", "reconcile_required", "inspection_binding_mismatch")
    if observation.evidence_status == "exact_match":
        assert observation.observed_process_binding is not None
        if observation.observed_process_binding != expected_process:
            return _decision("no_action", "reconcile_required", "observed_process_mismatch")
        return _decision(
            "adopt", "registered", "exact_binding_observed", observation.observed_process_binding
        )
    if observation.evidence_status == "proven_absent":
        return _decision(
            "record_proven_absence", "reconcile_required", "exact_process_tree_proven_absent"
        )
    reason = (
        "process_binding_mismatch"
        if observation.evidence_status == "mismatch"
        else "process_inspection_unavailable"
    )
    return _decision("no_action", "reconcile_required", reason)


def decide_closure(
    *,
    operation_id: str,
    process_binding: EveryCodeFeedbackProcessBindingRecord | None,
    gate_released: bool,
    requested_at: str,
) -> EveryCodeFeedbackLaunchDecision:
    if not gate_released:
        return _decision("cancel_gate", "cancelled", "pull_request_closed_before_release")
    if process_binding is None:
        return _decision("no_action", "cancellation_unknown", "released_binding_unavailable")
    instruction = EveryCodeFeedbackCancellationInstructionRecord(
        operation_id=operation_id, process_binding=process_binding, requested_at=requested_at
    )
    return EveryCodeFeedbackLaunchDecision(
        "request_exact_cancellation",
        "cancellation_unknown",
        "exact_cancellation_requested",
        process_binding,
        instruction,
    )


def cancellation_matches_process(
    instruction: EveryCodeFeedbackCancellationInstructionRecord,
    observed: EveryCodeFeedbackProcessBindingRecord,
) -> bool:
    return instruction.process_binding == observed


def _decision(
    action: LaunchAction,
    phase: LaunchPhase,
    reason: str,
    process_binding: EveryCodeFeedbackProcessBindingRecord | None = None,
) -> EveryCodeFeedbackLaunchDecision:
    return EveryCodeFeedbackLaunchDecision(action, phase, reason, process_binding)
