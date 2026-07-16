import hashlib
import json
from collections.abc import Mapping
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.generic_web_promotion_http import (
    GENERIC_WEB_PROD_PROMOTION_ROUTE,
    GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.verireel_nonprod_http import VERIREEL_TESTING_DEPLOY_ROUTE
from control_plane.verireel_prod_http import (
    VERIREEL_PROD_DEPLOY_ROUTE,
    VERIREEL_PROD_PROMOTION_ROUTE,
)


class AcceptedEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, object]
    result: dict[str, object] | None = None
    replayed: bool | None = None
    original_trace_id: str | None = None


class IdempotencyCapableStore(Protocol):
    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None: ...

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


def accepted_evidence_response(
    *,
    trace_id: str,
    records: Mapping[str, object],
    result: dict[str, object] | None = None,
    replayed: bool = False,
    original_trace_id: str = "",
) -> AcceptedEvidenceResponse:
    return AcceptedEvidenceResponse(
        trace_id=trace_id,
        records=dict(records),
        result=result,
        replayed=True if replayed else None,
        original_trace_id=original_trace_id or None,
    )


def provider_operation_response_payload(
    *,
    trace_id: str,
    records: Mapping[str, object],
    result: dict[str, object],
) -> dict[str, object]:
    return accepted_evidence_response(
        trace_id=trace_id,
        records=records,
        result=result,
    ).model_dump(mode="json", exclude_none=True)


def idempotency_capable_store(record_store: object) -> IdempotencyCapableStore | None:
    if callable(getattr(record_store, "read_idempotency_record", None)) and callable(
        getattr(record_store, "write_idempotency_record", None)
    ):
        return cast(IdempotencyCapableStore, record_store)
    return None


def idempotency_scope(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return "|".join(("github-human", identity.login, str(identity.github_id)))
    if isinstance(identity, LocalOperatorIdentity):
        return "|".join(("local-operator", identity.subject, identity.token_label))
    if isinstance(identity, LocalAdminIdentity):
        return "|".join(("local-admin", identity.subject, identity.token_label))
    if isinstance(identity, TerminalAgentIdentity):
        return "|".join(("terminal-agent", identity.subject, identity.token_label))
    if isinstance(identity, GitHubActionsIdentity):
        workflow_ref = identity.workflow_ref or identity.job_workflow_ref or ""
        return "|".join(
            (
                str(identity.repository).strip(),
                str(workflow_ref).strip(),
                str(identity.subject).strip(),
            )
        )
    raise TypeError(f"Unsupported Launchplane identity type: {type(identity).__name__}")


def replay_idempotent_response(
    *,
    trace_id: str,
    stored_record: LaunchplaneIdempotencyRecord,
    route_path: str = "",
) -> AcceptedEvidenceResponse:
    stored_records = {
        str(key): value
        if str(key).endswith("_preview_verification") and isinstance(value, dict)
        else str(value)
        for key, value in dict(stored_record.response_payload.get("records") or {}).items()
    }
    stored_result = stored_record.response_payload.get("result")
    if route_path in {
        GENERIC_WEB_DEPLOY_ROUTE,
        GENERIC_WEB_PROD_PROMOTION_ROUTE,
        GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
        VERIREEL_PROD_DEPLOY_ROUTE,
        VERIREEL_PROD_PROMOTION_ROUTE,
        VERIREEL_TESTING_DEPLOY_ROUTE,
    } and isinstance(stored_result, dict):
        stored_records.pop("target_type", None)
        stored_result = {str(key): value for key, value in stored_result.items()}
        stored_result.pop("target_type", None)
    return accepted_evidence_response(
        trace_id=trace_id,
        records=stored_records,
        result=stored_result if isinstance(stored_result, dict) else None,
        replayed=True,
        original_trace_id=stored_record.response_trace_id,
    )


def request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
