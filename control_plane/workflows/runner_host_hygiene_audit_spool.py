from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
import os
from pathlib import Path
import tempfile

import click
from pydantic import ValidationError

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAuditDeliveryEnvelope
from control_plane.workflows.ship import utc_now_timestamp


class RunnerHostHygieneAuditSpool:
    def __init__(self, *, root: Path, artifact_file: Path | None = None) -> None:
        if not root.is_absolute():
            raise click.ClickException("runner host hygiene audit spool root must be absolute")
        self._root = root
        self._artifact_file = artifact_file
        self._ensure_directory(root)

    def create(
        self,
        *,
        planned_audit: RunnerHostHygieneApplyAuditRecord,
        planned_idempotency_key: str,
        executor_intent_sha256: str = "",
    ) -> RunnerHostHygieneAuditDeliveryEnvelope:
        existing = self.read(
            host_name=planned_audit.request.host_name,
            action=planned_audit.request.action,
            audit_record_key=planned_audit.audit_record_key,
        )
        if existing is not None:
            if (
                existing.planned_audit != planned_audit
                or existing.planned_idempotency_key != planned_idempotency_key
                or existing.executor_intent_sha256 != executor_intent_sha256
            ):
                raise click.ClickException(
                    "runner host hygiene audit spool key already exists with different intent"
                )
            return existing
        timestamp = utc_now_timestamp()
        envelope = RunnerHostHygieneAuditDeliveryEnvelope(
            schema_version=2 if executor_intent_sha256 else 1,
            host_name=planned_audit.request.host_name,
            action=planned_audit.request.action,
            audit_record_key=planned_audit.audit_record_key,
            planned_audit=planned_audit,
            planned_idempotency_key=planned_idempotency_key,
            executor_intent_sha256=executor_intent_sha256,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.write(envelope)
        return envelope

    def read(
        self,
        *,
        host_name: str,
        action: RunnerHostHygieneApplyAction,
        audit_record_key: str,
    ) -> RunnerHostHygieneAuditDeliveryEnvelope | None:
        path = self._record_path(
            host_name=host_name,
            action=action,
            audit_record_key=audit_record_key,
        )
        if not path.exists():
            return None
        return self._read_path(path)

    def list_for(
        self, *, host_name: str, action: RunnerHostHygieneApplyAction
    ) -> tuple[RunnerHostHygieneAuditDeliveryEnvelope, ...]:
        directory = self._record_directory(host_name=host_name, action=action)
        if not directory.exists():
            return ()
        return tuple(self._read_path(path) for path in sorted(directory.glob("*.json")))

    def write(
        self, envelope: RunnerHostHygieneAuditDeliveryEnvelope
    ) -> RunnerHostHygieneAuditDeliveryEnvelope:
        updated = envelope.model_copy(update={"updated_at": utc_now_timestamp()})
        path = self._record_path(
            host_name=updated.host_name,
            action=updated.action,
            audit_record_key=updated.audit_record_key,
        )
        self._ensure_directory(path.parent)
        self._write_model(path, updated)
        if self._artifact_file is not None:
            self._write_model(self._artifact_file, updated)
        return updated

    @staticmethod
    def is_unresolved(envelope: RunnerHostHygieneAuditDeliveryEnvelope) -> bool:
        if envelope.execution_state in {"planned", "action_started"}:
            return True
        return (
            envelope.execution_state == "terminal_recorded"
            and envelope.terminal_delivery_state != "delivered"
        )

    @staticmethod
    def pending_delivery_phases(
        envelope: RunnerHostHygieneAuditDeliveryEnvelope,
    ) -> tuple[tuple[RunnerHostHygieneApplyAuditRecord, str, str], ...]:
        pending: list[tuple[RunnerHostHygieneApplyAuditRecord, str, str]] = []
        if envelope.planned_delivery_state == "pending":
            pending.append(
                (
                    envelope.planned_audit,
                    envelope.planned_idempotency_key,
                    "planned",
                )
            )
        if envelope.terminal_audit is not None and envelope.terminal_delivery_state == "pending":
            pending.append(
                (
                    envelope.terminal_audit,
                    envelope.terminal_idempotency_key,
                    "terminal",
                )
            )
        return tuple(pending)

    def _record_path(
        self,
        *,
        host_name: str,
        action: RunnerHostHygieneApplyAction,
        audit_record_key: str,
    ) -> Path:
        digest = sha256(audit_record_key.encode()).hexdigest()
        return self._record_directory(host_name=host_name, action=action) / f"{digest}.json"

    def _record_directory(self, *, host_name: str, action: RunnerHostHygieneApplyAction) -> Path:
        return self._root / _safe_segment(host_name) / _safe_segment(action)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise click.ClickException(
                "runner host hygiene audit spool directory must be a real directory"
            )
        path.chmod(0o700)

    @staticmethod
    def _read_path(path: Path) -> RunnerHostHygieneAuditDeliveryEnvelope:
        if path.is_symlink() or not path.is_file():
            raise click.ClickException(
                "runner host hygiene audit spool record must be a regular file"
            )
        try:
            return RunnerHostHygieneAuditDeliveryEnvelope.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as error:
            raise click.ClickException(
                "runner host hygiene audit spool contains an unreadable record"
            ) from error

    @staticmethod
    def _write_model(path: Path, model: RunnerHostHygieneAuditDeliveryEnvelope) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(model.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


def unresolved_audit_keys(
    envelopes: Iterable[RunnerHostHygieneAuditDeliveryEnvelope],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            envelope.audit_record_key
            for envelope in envelopes
            if RunnerHostHygieneAuditSpool.is_unresolved(envelope)
        )
    )


def _safe_segment(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in value.strip().lower()
    ).strip("-.")
    if not normalized:
        raise click.ClickException("runner host hygiene audit spool segment was empty")
    return normalized[:128]
