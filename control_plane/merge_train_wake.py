"""Bounded observations of a train wake, never admission or landing evidence."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import json
from typing import cast


_RECEIPT_PREFIX = "merge-train-wake-v1:"
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


def encode_merge_train_hint(target_key: str, receipt: MergeTrainEventReceipt | None) -> str | None:
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
    """Decode advisory routing and bounded observations from atomic notifications."""
    target_keys: set[str] = set()
    receipts: dict[MergeTrainTargetReceipt, None] = {}
    truncated = False
    full_sweep = reason != "event_hints"
    for payload in payloads:
        if not payload.startswith(_RECEIPT_PREFIX):
            target_key = payload
            receipt = None
        else:
            if len(payload.encode("utf-8")) > _MAX_RECEIPT_BYTES:
                continue
            try:
                value = json.loads(payload.removeprefix(_RECEIPT_PREFIX))
            except (ValueError, RecursionError):
                continue
            if not isinstance(value, dict) or not isinstance(value.get("target_key"), str):
                continue
            target_key = cast(str, value["target_key"])
            receipt = MergeTrainEventReceipt.from_payload(value.get("receipt"))
        if not target_key:
            full_sweep = True
            if reason == "event_hints":
                reason = "empty_hint_sweep"
        else:
            target_keys.add(target_key)
        if receipt is None:
            continue
        item = MergeTrainTargetReceipt(target_key, receipt)
        receipts.pop(item, None)
        receipts[item] = None
        if len(receipts) > MAX_MERGE_TRAIN_WAKE_RECEIPTS:
            truncated = True
            receipts.pop(next(iter(receipts)))
    if not target_keys and not full_sweep:
        full_sweep = True
        reason = "empty_hint_sweep"
    return MergeTrainWake(
        target_keys=None if full_sweep else frozenset(target_keys),
        reason=reason,
        receipts=tuple(receipts),
        receipts_truncated=truncated,
        hint_observed_at=hint_observed_at,
    )
