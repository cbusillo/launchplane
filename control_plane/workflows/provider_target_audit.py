from __future__ import annotations

from collections import Counter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


ProviderTargetAuditStatus = Literal[
    "complete",
    "missing_provider_target",
    "missing_dokploy_pair",
    "missing_dokploy_target",
    "missing_target_id",
    "no_matching_routes",
    "identity_mismatch",
    "category_mismatch",
    "display_name_mismatch",
    "provider_target_type_mismatch",
    "provider_evidence_mismatch",
    "future_provider",
]
ProviderTargetAuditSeverity = Literal["ok", "warning", "blocked"]


class ProviderTargetAuditRecordStore(Protocol):
    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]: ...

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...


class ProviderTargetAuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    status: ProviderTargetAuditStatus
    severity: ProviderTargetAuditSeverity
    detail: str
    physical_record: ProviderTargetRecord | None = None
    projected_record: ProviderTargetRecord | None = None

    @model_validator(mode="after")
    def _validate_finding(self) -> "ProviderTargetAuditFinding":
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.detail = self.detail.strip()
        if not self.context:
            raise ValueError("provider target audit finding requires context")
        if not self.instance:
            raise ValueError("provider target audit finding requires instance")
        if not self.detail:
            raise ValueError("provider target audit finding requires detail")
        return self


class ProviderTargetAuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: tuple[ProviderTargetAuditFinding, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)
    inspected_route_count: int = 0
    blocked_count: int = 0
    warning_count: int = 0

    @property
    def ok(self) -> bool:
        return self.blocked_count == 0

    def to_report_payload(self) -> dict[str, object]:
        return {
            "status": "ok" if self.ok else "blocked",
            "inspected_route_count": self.inspected_route_count,
            "blocked_count": self.blocked_count,
            "warning_count": self.warning_count,
            "counts": self.counts,
            "findings": [
                finding.model_dump(mode="json", exclude_none=True)
                for finding in self.findings
            ],
        }


def audit_provider_targets(
    record_store: ProviderTargetAuditRecordStore,
    *,
    provider_id: str = "",
    context_name: str = "",
    instance_name: str = "",
) -> ProviderTargetAuditResult:
    normalized_provider_id = provider_id.strip().lower()
    normalized_context = context_name.strip()
    normalized_instance = instance_name.strip()
    physical_records = {
        _route_key(record.context, record.instance): record
        for record in record_store.list_physical_provider_target_records()
    }
    dokploy_target_records = {
        _route_key(record.context, record.instance): record
        for record in record_store.list_dokploy_target_records()
    }
    dokploy_target_id_records = {
        _route_key(record.context, record.instance): record
        for record in record_store.list_dokploy_target_id_records()
    }
    route_keys = sorted(
        physical_records.keys()
        | dokploy_target_records.keys()
        | dokploy_target_id_records.keys()
    )
    findings: list[ProviderTargetAuditFinding] = []
    inspected_route_count = 0
    for context, instance in route_keys:
        if normalized_context and context != normalized_context:
            continue
        if normalized_instance and instance != normalized_instance:
            continue
        physical_record = physical_records.get((context, instance))
        target_record = dokploy_target_records.get((context, instance))
        target_id_record = dokploy_target_id_records.get((context, instance))
        if not _matches_provider_filter(
            normalized_provider_id=normalized_provider_id,
            physical_record=physical_record,
            target_record=target_record,
            target_id_record=target_id_record,
        ):
            continue
        inspected_route_count += 1
        findings.extend(
            _audit_route(
                context=context,
                instance=instance,
                physical_record=physical_record,
                target_record=target_record,
                target_id_record=target_id_record,
            )
        )
    if inspected_route_count == 0 and (
        normalized_provider_id or normalized_context or normalized_instance
    ):
        findings.append(
            _finding(
                context=normalized_context or "*",
                instance=normalized_instance or "*",
                status="no_matching_routes",
                severity="blocked",
                detail="provider-target audit filters did not match any stored route",
            )
        )
    counts = Counter(finding.status for finding in findings)
    return ProviderTargetAuditResult(
        findings=tuple(findings),
        counts=dict(sorted(counts.items())),
        inspected_route_count=inspected_route_count,
        blocked_count=sum(1 for finding in findings if finding.severity == "blocked"),
        warning_count=sum(1 for finding in findings if finding.severity == "warning"),
    )


def _audit_route(
    *,
    context: str,
    instance: str,
    physical_record: ProviderTargetRecord | None,
    target_record: DokployTargetRecord | None,
    target_id_record: DokployTargetIdRecord | None,
) -> tuple[ProviderTargetAuditFinding, ...]:
    findings: list[ProviderTargetAuditFinding] = []
    if physical_record is not None and physical_record.provider_id != "dokploy":
        return (
            _finding(
                context=context,
                instance=instance,
                status="future_provider",
                severity="warning",
                detail="explicit provider-target row is not managed by the Dokploy projection audit",
                physical_record=physical_record,
            ),
        )
    if target_record is None and target_id_record is None:
        if physical_record is None or physical_record.provider_id != "dokploy":
            return tuple(findings)
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="missing_dokploy_pair",
                severity="blocked",
                detail="explicit Dokploy provider-target row has no Dokploy target or target-id records",
                physical_record=physical_record,
            )
        )
        return tuple(findings)
    if target_record is None:
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="missing_dokploy_target",
                severity="blocked",
                detail="Dokploy target metadata record is missing",
                physical_record=physical_record,
            )
        )
        return tuple(findings)
    if target_id_record is None:
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="missing_target_id",
                severity="blocked",
                detail="Dokploy target-id record is missing",
                physical_record=physical_record,
            )
        )
        return tuple(findings)
    projected_record = ProviderTargetRecord.from_dokploy_records(
        target_record=target_record,
        target_id_record=target_id_record,
    )
    if physical_record is None:
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="missing_provider_target",
                severity="blocked",
                detail="Dokploy target/id pair has no explicit provider-target row",
                projected_record=projected_record,
            )
        )
        return tuple(findings)
    findings.extend(
        _compare_records(
            context=context,
            instance=instance,
            physical_record=physical_record,
            projected_record=projected_record,
        )
    )
    if len(findings) == 0 or all(
        finding.status == "future_provider" for finding in findings
    ):
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="complete",
                severity="ok",
                detail="explicit provider-target row matches the Dokploy projection",
                physical_record=physical_record,
                projected_record=projected_record,
            )
        )
    return tuple(findings)


def _compare_records(
    *,
    context: str,
    instance: str,
    physical_record: ProviderTargetRecord,
    projected_record: ProviderTargetRecord,
) -> tuple[ProviderTargetAuditFinding, ...]:
    findings: list[ProviderTargetAuditFinding] = []
    identity_differences = _identity_differences(
        physical_record=physical_record,
        projected_record=projected_record,
    )
    if identity_differences:
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="identity_mismatch",
                severity="blocked",
                detail="provider-target identity mismatch: "
                + ", ".join(identity_differences),
                physical_record=physical_record,
                projected_record=projected_record,
            )
        )
    if physical_record.target_category != projected_record.target_category:
        findings.append(
            _field_mismatch_finding(
                context=context,
                instance=instance,
                status="category_mismatch",
                field_name="target_category",
                physical_value=physical_record.target_category,
                projected_value=projected_record.target_category,
                physical_record=physical_record,
                projected_record=projected_record,
            )
        )
    if physical_record.display_name != projected_record.display_name:
        findings.append(
            _field_mismatch_finding(
                context=context,
                instance=instance,
                status="display_name_mismatch",
                field_name="display_name",
                physical_value=physical_record.display_name,
                projected_value=projected_record.display_name,
                physical_record=physical_record,
                projected_record=projected_record,
            )
        )
    if physical_record.provider_target_type != projected_record.provider_target_type:
        findings.append(
            _field_mismatch_finding(
                context=context,
                instance=instance,
                status="provider_target_type_mismatch",
                field_name="provider_target_type",
                physical_value=physical_record.provider_target_type,
                projected_value=projected_record.provider_target_type,
                physical_record=physical_record,
                projected_record=projected_record,
            )
        )
    if physical_record.provider_evidence != projected_record.provider_evidence:
        findings.append(
            _finding(
                context=context,
                instance=instance,
                status="provider_evidence_mismatch",
                severity="blocked",
                detail="provider_evidence differs between explicit row and Dokploy projection",
                physical_record=physical_record,
                projected_record=projected_record,
            )
        )
    return tuple(findings)


def _identity_differences(
    *,
    physical_record: ProviderTargetRecord,
    projected_record: ProviderTargetRecord,
) -> tuple[str, ...]:
    differences: list[str] = []
    if physical_record.provider_id != projected_record.provider_id:
        differences.append(
            f"provider_id physical={physical_record.provider_id!r} projected={projected_record.provider_id!r}"
        )
    if physical_record.target_id != projected_record.target_id:
        differences.append(
            f"target_id physical={physical_record.target_id!r} projected={projected_record.target_id!r}"
        )
    return tuple(differences)


def _field_mismatch_finding(
    *,
    context: str,
    instance: str,
    status: ProviderTargetAuditStatus,
    field_name: str,
    physical_value: str,
    projected_value: str,
    physical_record: ProviderTargetRecord,
    projected_record: ProviderTargetRecord,
) -> ProviderTargetAuditFinding:
    return _finding(
        context=context,
        instance=instance,
        status=status,
        severity="blocked",
        detail=f"{field_name} physical={physical_value!r} projected={projected_value!r}",
        physical_record=physical_record,
        projected_record=projected_record,
    )


def _finding(
    *,
    context: str,
    instance: str,
    status: ProviderTargetAuditStatus,
    severity: ProviderTargetAuditSeverity,
    detail: str,
    physical_record: ProviderTargetRecord | None = None,
    projected_record: ProviderTargetRecord | None = None,
) -> ProviderTargetAuditFinding:
    return ProviderTargetAuditFinding(
        context=context,
        instance=instance,
        status=status,
        severity=severity,
        detail=detail,
        physical_record=physical_record,
        projected_record=projected_record,
    )


def _matches_provider_filter(
    *,
    normalized_provider_id: str,
    physical_record: ProviderTargetRecord | None,
    target_record: DokployTargetRecord | None,
    target_id_record: DokployTargetIdRecord | None,
) -> bool:
    if not normalized_provider_id:
        return True
    if physical_record is not None and physical_record.provider_id == normalized_provider_id:
        return True
    if normalized_provider_id == "dokploy" and (
        target_record is not None or target_id_record is not None
    ):
        return True
    return False


def _route_key(context: str, instance: str) -> tuple[str, str]:
    return (context.strip(), instance.strip())
