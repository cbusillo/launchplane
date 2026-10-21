"""Original application-testing effects, retained in their deployment evidence."""

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.dokploy import api
from control_plane.provider_operations import ProviderMutationUnknownError

EXECUTION_EVIDENCE_KEY = "generic_web_original_execution"


def evidence_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ExecutionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["schedule_run", "schedule_write", "schedule_delete", "product_executor"]
    name: str = Field(max_length=128)
    target_id: str = Field(max_length=256)
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_ids: tuple[str, ...] = ()
    acknowledgement: Literal["none", "identified", "synchronous", "completed", "rejected"] = "none"
    deployment_id: str = Field(default="", max_length=256)
    status: Literal["", "done", "running", "pending", "queued", "error", "unknown"] = ""
    terminal: Literal["", "done", "not_started", "write_completed", "executor_completed"] = ""


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    inventory_baseline_digest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    entries: tuple[ExecutionEntry, ...] = ()


def execution_receipt(record: DeploymentRecord) -> ExecutionReceipt | None:
    raw = record.post_deploy_update.evidence.get(EXECUTION_EVIDENCE_KEY)
    return ExecutionReceipt.model_validate_json(raw) if raw else None


def require_receipt_extension(previous: ExecutionReceipt, current: ExecutionReceipt) -> None:
    if previous.inventory_baseline_digest != current.inventory_baseline_digest:
        raise ValueError("Original execution inventory baseline changed.")
    if len(current.entries) < len(previous.entries):
        raise ValueError("Original execution evidence cannot be removed.")
    for old, new in zip(previous.entries, current.entries, strict=False):
        for name in ("kind", "name", "target_id", "payload_digest", "baseline_ids"):
            if getattr(old, name) != getattr(new, name):
                raise ValueError("Original execution intent changed.")
        for name in ("acknowledgement", "deployment_id", "status", "terminal"):
            value = getattr(old, name)
            if value and value != "none" and value != getattr(new, name):
                raise ValueError("Original execution proof changed.")


def require_execution_record_extension(
    previous: DeploymentRecord, current: DeploymentRecord
) -> None:
    prior = execution_receipt(previous)
    if prior is None:
        return
    receipt = execution_receipt(current)
    if receipt is None:
        raise ValueError("Original execution evidence cannot be removed.")
    require_receipt_extension(prior, receipt)
    for name in (
        "record_id",
        "context",
        "instance",
        "artifact_identity",
        "source_git_ref",
        "resolved_target",
        "deployed_target",
    ):
        if getattr(previous, name) != getattr(current, name):
            raise ValueError("Original execution deployment binding changed.")
    from control_plane.workflows.generic_web_application_snapshot import (
        recorded_application_lifecycle,
    )

    old = recorded_application_lifecycle(previous)
    new = recorded_application_lifecycle(current)
    if (old is None) != (new is None) or (
        old is not None
        and new is not None
        and old.model_dump(exclude={"state"}) != new.model_dump(exclude={"state"})
    ):
        raise ValueError("Original execution lifecycle baseline changed.")


def schedule_completed(entry: ExecutionEntry, history: tuple[api.JsonObject, ...]) -> bool:
    if len({api.deployment_key(row) for row in history}) != len(history):
        return False
    if any(
        not api.deployment_key(row)
        or (row.get("scheduleId") is not None and row.get("scheduleId") != entry.target_id)
        for row in history
    ):
        return False
    new = tuple(row for row in history if api.deployment_key(row) not in entry.baseline_ids)
    if entry.acknowledgement == "identified":
        matches = tuple(row for row in new if api.deployment_key(row) == entry.deployment_id)
        return len(matches) == 1 and api.deployment_status(matches[0]) == "done"
    if entry.acknowledgement == "synchronous":
        return bool(new) and all(api.deployment_status(row) == "done" for row in new)
    return entry.acknowledgement == "rejected" and entry.terminal == "not_started"


class ScheduleHistoryBindingRefused(ValueError):
    """Provider evidence names a different intended schedule or application."""


def require_schedule_history_binding(
    history: tuple[api.JsonObject, ...], *, schedule_id: str, application_id: object
) -> None:
    for row in history:
        nested = api.as_json_object(row.get("schedule"))
        for identity in (row, nested or {}):
            if identity.get("scheduleId") and identity["scheduleId"] != schedule_id:
                raise ScheduleHistoryBindingRefused(
                    "Original schedule history has a foreign schedule."
                )
            if identity.get("applicationId") and identity["applicationId"] != application_id:
                raise ScheduleHistoryBindingRefused(
                    "Original schedule history has a foreign application."
                )


@dataclass(frozen=True)
class ExecutionObservation:
    outcome: Literal["terminal", "unknown"]
    digest_evidence: dict[str, object]


def observe_execution(
    receipt: ExecutionReceipt,
    read_history: Callable[[str], tuple[api.JsonObject, ...]],
) -> ExecutionObservation:
    rows: dict[str, object] = {}
    terminal = True
    for index, entry in enumerate(receipt.entries):
        if entry.kind == "schedule_run" and not entry.terminal:
            try:
                history = read_history(entry.target_id)
            except Exception:
                terminal = False
                rows[str(index)] = "unavailable"
                continue
            rows[str(index)] = sorted(
                (
                    (
                        api.deployment_key(row),
                        api.deployment_status(row),
                        str(row.get("scheduleId", "")),
                    )
                    for row in history
                ),
            )
            terminal = schedule_completed(entry, history) and terminal
        else:
            terminal = bool(entry.terminal) and terminal
    return ExecutionObservation(
        "terminal" if terminal else "unknown",
        {"receipt": receipt.model_dump(mode="json"), "executions": rows},
    )


class ExecutionRecordStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...
    def write_deployment_record(self, record: DeploymentRecord) -> object: ...


@dataclass
class ExecutionRecorder:
    store: ExecutionRecordStore
    record_id: str
    receipt: ExecutionReceipt = field(default_factory=ExecutionReceipt)
    expected_record: DeploymentRecord | None = field(default=None, repr=False)

    def evidence(self) -> dict[str, str]:
        return {EXECUTION_EVIDENCE_KEY: self.receipt.model_dump_json()}

    def write_pending(self, record: DeploymentRecord) -> None:
        record = record.model_copy(
            update={
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={"evidence": {**record.post_deploy_update.evidence, **self.evidence()}}
                )
            }
        )
        publisher = getattr(self.store, "publish_generic_web_testing_result", None)
        if publisher is not None:
            try:
                inventory = getattr(self.store, "read_environment_inventory")(
                    context_name=record.context, instance_name=record.instance
                )
            except FileNotFoundError:
                inventory = None
            publisher(
                expected_deployment=self.expected_record,
                expected_inventory=inventory,
                deployment=record,
                inventory=None,
            )
        else:
            self.store.write_deployment_record(record)
        self.expected_record = record

    def save(self) -> None:
        record = self.store.read_deployment_record(self.record_id)
        previous = execution_receipt(record)
        if previous is not None:
            require_receipt_extension(previous, self.receipt)
        update = record.post_deploy_update.model_copy(
            update={"evidence": {**record.post_deploy_update.evidence, **self.evidence()}}
        )
        merge = getattr(self.store, "advance_generic_web_execution_receipt", None)
        if merge is not None:
            merge(record_id=self.record_id, receipt_json=self.receipt.model_dump_json())
        else:
            self.store.write_deployment_record(
                record.model_copy(update={"post_deploy_update": update})
            )
        if self.expected_record is not None:
            expected = self.expected_record
            self.expected_record = expected.model_copy(
                update={
                    "post_deploy_update": expected.post_deploy_update.model_copy(
                        update={
                            "evidence": {**expected.post_deploy_update.evidence, **self.evidence()}
                        }
                    )
                }
            )

    def begin(
        self,
        kind: Literal["schedule_run", "schedule_write", "schedule_delete", "product_executor"],
        name: str,
        target_id: str,
        payload: object,
        baseline_ids: tuple[str, ...] = (),
    ) -> int:
        index = len(self.receipt.entries)
        entry = ExecutionEntry(
            kind=kind,
            name=name,
            target_id=target_id,
            payload_digest=evidence_digest(payload),
            baseline_ids=tuple(sorted(baseline_ids)),
        )
        self.receipt = self.receipt.model_copy(update={"entries": (*self.receipt.entries, entry)})
        self.save()
        return index

    def update(self, index: int, **changes: object) -> None:
        entries = list(self.receipt.entries)
        entries[index] = ExecutionEntry.model_validate({**entries[index].model_dump(), **changes})
        updated = self.receipt.model_copy(update={"entries": tuple(entries)})
        require_receipt_extension(self.receipt, updated)
        self.receipt = updated
        self.save()


@dataclass
class RecordedScheduleExecution:
    recorder: ExecutionRecorder
    name: str
    schedule_id: str
    payload: object
    index: int = -1

    def before_dispatch(self, baseline_ids: tuple[str, ...]) -> None:
        if len(set(baseline_ids)) != len(baseline_ids) or any(
            not key or len(key) > 256 for key in baseline_ids
        ):
            raise ValueError("Original schedule baseline is incomplete.")
        self.index = self.recorder.begin(
            "schedule_run", self.name, self.schedule_id, self.payload, baseline_ids
        )

    def acknowledged(self, response: api.JsonValue) -> None:
        payload = api.as_json_object(response)
        intended = api.as_json_object(self.payload) if isinstance(self.payload, dict) else None
        if payload is not None:
            self._require_binding((payload,), intended)
        if (
            payload is not None
            and isinstance(payload.get("deploymentId"), str)
            and payload["deploymentId"]
        ):
            status = payload.get("status")
            self.recorder.update(
                self.index,
                acknowledgement="identified",
                deployment_id=payload["deploymentId"],
                status=status
                if isinstance(status, str)
                and status in {"done", "running", "pending", "queued", "error"}
                else "unknown",
            )
        elif response is True:
            self.recorder.update(self.index, acknowledgement="synchronous")

    def _require_binding(
        self, history: tuple[api.JsonObject, ...], intended: api.JsonObject | None
    ) -> None:
        try:
            require_schedule_history_binding(
                history,
                schedule_id=self.schedule_id,
                application_id=intended.get("applicationId") if intended is not None else None,
            )
        except ScheduleHistoryBindingRefused as error:
            raise api.DokployScheduleExecutionFailed(
                schedule_id=self.schedule_id,
                deployment_id="",
                deployment_status="unknown",
                cause="execution_binding_unproved",
            ) from error

    def rejected(self) -> None:
        self.recorder.update(self.index, acknowledgement="rejected", terminal="not_started")

    def completed(self, history: tuple[api.JsonObject, ...]) -> bool:
        intended = api.as_json_object(self.payload) if isinstance(self.payload, dict) else None
        self._require_binding(history, intended)
        if schedule_completed(self.recorder.receipt.entries[self.index], history):
            self.recorder.update(self.index, terminal="done")
            return True
        return False


def require_recorded_execution(receipt: ExecutionReceipt) -> None:
    if any(not entry.terminal for entry in receipt.entries):
        raise ProviderMutationUnknownError(
            "Original application execution has no attributable terminal proof."
        )
