"""Bounded observations of a train wake, never admission or landing evidence."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import json
from typing import cast


_RECEIPT_PREFIX = "merge-train-receipt-v1:"
_MAX_RECEIPT_BYTES = 4096
MAX_MERGE_TRAIN_WAKE_RECEIPTS = 64


@dataclass(frozen=True)
class MergeTrainEventReceipt:
    delivery_id: str
    request_trace_id: str
    received_at: str
    event: str
    action: str = ""
    head_sha: str = ""
    source_id: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> "MergeTrainEventReceipt | None":
        if not isinstance(payload, dict):
            return None
        values: dict[str, str] = {}
        for item in fields(cls):
            value = payload.get(item.name, "")
            if not isinstance(value, str):
                return None
            try:
                if len(value.encode("utf-8")) > 256:
                    return None
            except UnicodeError:
                return None
            if value and not value.isprintable():
                return None
            values[item.name] = value
        receipt = cls(**values)
        if not all(
            (receipt.delivery_id, receipt.request_trace_id, receipt.received_at, receipt.event)
        ):
            return None
        try:
            timestamp = datetime.fromisoformat(receipt.received_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return receipt if timestamp.tzinfo is not None else None


@dataclass(frozen=True)
class MergeTrainTargetReceipt:
    target_key: str
    receipt: MergeTrainEventReceipt


@dataclass(frozen=True)
class MergeTrainWakeObservation:
    reason: str
    pass_started_at: str
    hint_observed_at: str = ""
    event_receipts: tuple[MergeTrainEventReceipt, ...] = ()
    receipts_truncated: bool = False


@dataclass(frozen=True)
class MergeTrainWake:
    target_keys: frozenset[str] | None
    reason: str
    receipts: tuple[MergeTrainTargetReceipt, ...] = ()
    receipts_truncated: bool = False
    pass_started_at: str = ""
    hint_observed_at: str = ""
    pass_error_code: str = ""

    def for_target(self, target_key: str) -> MergeTrainWakeObservation:
        return MergeTrainWakeObservation(
            reason=self.reason,
            pass_started_at=self.pass_started_at,
            hint_observed_at=self.hint_observed_at,
            event_receipts=tuple(
                item.receipt for item in self.receipts if item.target_key == target_key
            ),
            receipts_truncated=self.receipts_truncated,
        )


def encode_merge_train_receipt(
    target_key: str, receipt: MergeTrainEventReceipt | None
) -> str | None:
    if receipt is None or MergeTrainEventReceipt.from_payload(asdict(receipt)) is None:
        return None
    payload = _RECEIPT_PREFIX + json.dumps(
        {"target_key": target_key, "receipt": asdict(receipt)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return payload if len(payload.encode("utf-8")) <= _MAX_RECEIPT_BYTES else None


def decode_merge_train_wake(
    payloads: list[str], *, reason: str, hint_observed_at: str = ""
) -> MergeTrainWake:
    """Metadata never supplies a routing key or narrows an existing full sweep."""
    target_keys: set[str] = set()
    receipts: list[MergeTrainTargetReceipt] = []
    retained: set[MergeTrainTargetReceipt] = set()
    truncated = False
    full_sweep = reason != "event_hints"
    for payload in payloads:
        if not payload:
            full_sweep = True
            if reason == "event_hints":
                reason = "empty_hint_sweep"
            continue
        if not payload.startswith(_RECEIPT_PREFIX):
            target_keys.add(payload)
            continue
        if len(payload.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            continue
        try:
            value = json.loads(payload.removeprefix(_RECEIPT_PREFIX))
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, dict) or not isinstance(value.get("target_key"), str):
            continue
        receipt = MergeTrainEventReceipt.from_payload(value.get("receipt"))
        if receipt is None:
            continue
        item = MergeTrainTargetReceipt(cast(str, value["target_key"]), receipt)
        if item in retained:
            continue
        if len(receipts) >= MAX_MERGE_TRAIN_WAKE_RECEIPTS:
            truncated = True
            continue
        retained.add(item)
        receipts.append(item)
    return MergeTrainWake(
        target_keys=None if full_sweep else frozenset(target_keys),
        reason=reason,
        receipts=tuple(item for item in receipts if item.target_key in target_keys),
        receipts_truncated=truncated,
        hint_observed_at=hint_observed_at,
    )
