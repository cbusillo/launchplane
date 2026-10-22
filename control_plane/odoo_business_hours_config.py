import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from control_plane.contracts.odoo_business_hours import (
    OdooBusinessHoursConfigRequest,
    OdooBusinessHoursConfigResult,
    OdooBusinessHoursPolicy,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_business_hours import (
    calendar_runtime_connection,
    calendar_window,
    read_calendar_sources,
    require_calendar_authority_current,
)
from control_plane.storage.postgres import PostgresRecordStore


def plan_business_hours_config(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    request: OdooBusinessHoursConfigRequest,
    root: Path,
    reason: str,
    source_label: str,
    observed_at: datetime | None = None,
) -> tuple[OdooBusinessHoursConfigResult, LaunchplaneProductProfileRecord]:
    if profile.driver_id != "odoo" or profile.lifecycle_state != "active":
        raise ValueError("Calendar policy requires an active Odoo product.")
    policy = profile.odoo_business_hours
    result = OdooBusinessHoursConfigResult(
        operation=request.operation, policy_before=policy, policy_after=policy, changed=False
    )
    after = None
    if request.operation != "clear":
        connection, origin, endpoint_digest = calendar_runtime_connection(
            store=store,
            profile=profile,
            private_endpoint_key=request.private_endpoint_key,
            control_plane_root=root,
        )
        now = observed_at or datetime.now(UTC)
        observation = read_calendar_sources(
            connection,
            company_id=request.company_id,
            observed_at=now,
            private_origin=bool(request.private_endpoint_key),
            transport_origin=origin,
            inspect_only=request.operation == "inspect",
            expected_calendar_id=request.expected_calendar_id,
        )
        window = (
            None
            if request.operation == "inspect"
            else calendar_window(
                observation.calendar, observation.attendance, observation.leaves, observed_at=now
            )
        )
        binding = require_calendar_authority_current(
            store=store,
            profile=profile,
            connection=connection,
            private_endpoint_key=request.private_endpoint_key,
            endpoint_digest=endpoint_digest,
        )
        result = result.model_copy(
            update={
                "permitted_company_ids": observation.permitted_company_ids,
                "companies": observation.companies if request.operation == "inspect" else (),
                "truncated": observation.truncated,
                "runtime_identity": connection.selection.identity,
                "company_id": request.company_id,
                "calendar_id": request.expected_calendar_id,
                "timezone": window.timezone if window else "",
                "window": window,
                "authority_digest": binding,
            }
        )
        if request.operation == "inspect":
            return result, profile
        if observation.truncated:
            raise ValueError("Calendar qualification is truncated.")
        if request.company_id is None:
            raise ValueError("A selected company is required.")
        after = OdooBusinessHoursPolicy(
            company_id=request.company_id, private_endpoint_key=request.private_endpoint_key
        )
    digest = hashlib.sha256(
        json.dumps(
            {
                "profile": profile.model_dump(mode="json"),
                "request": request.model_dump(
                    mode="json", exclude_none=True, exclude_defaults=True
                ),
                "reason": reason,
                "source_label": source_label,
                "authority_digest": result.authority_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    replacement = LaunchplaneProductProfileRecord.model_validate(
        profile.model_copy(update={"odoo_business_hours": after}).model_dump(mode="json")
    )
    return result.model_copy(
        update={
            "policy_after": after,
            "changed": policy != after,
            "apply_eligible": True,
            "plan_digest": digest,
        }
    ), replacement
