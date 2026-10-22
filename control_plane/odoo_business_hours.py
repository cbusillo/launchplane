"""Bounded reads of Odoo's company calendar and release-window evaluation."""

from datetime import UTC, datetime, time, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hashlib
import json
import re
import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.odoo_business_hours import (
    BusinessHoursWindow,
    OdooCalendarCompanyReference,
)
from control_plane.contracts.private_health_endpoint_record import (
    private_health_endpoint_record_sha256,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    _NoRedirects,
    _http_json,
    _rpc,
    assert_selection_current,
    connect_runtime,
    diagnostic_authentication,
    require_diagnostic_user,
    authenticate_diagnostic,
    destroy_diagnostic_session,
    require_runtime_identity,
    select_stable_runtime,
)
from control_plane.contracts.runtime_identity import runtime_identity_from_health_payload
from control_plane.workflows.odoo_preview_runtime import odoo_compose_has_domain
from control_plane.storage.postgres import PostgresRecordStore


class BusinessHoursHeld(ValueError):
    """No forward effects started; retry the same accepted release later."""


def _rows(value: object, *, maximum: int = 128) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Calendar evidence is unavailable or truncated.")
    if any(not isinstance(row, dict) for row in value):
        raise ValueError("Invalid calendar evidence.")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Calendar text evidence is unavailable.")
    return value


def _hour(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("Calendar hour evidence is unavailable.")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError("Calendar hour evidence is unavailable.")


def _id(value: object) -> int:
    if isinstance(value, list) and len(value) == 2 and type(value[0]) is int and value[0] > 0:
        return value[0]
    raise ValueError("Missing Odoo calendar reference.")


@dataclass(frozen=True)
class CalendarSourceObservation:
    companies: tuple[OdooCalendarCompanyReference, ...]
    permitted_company_ids: tuple[int, ...]
    truncated: bool
    calendar: dict[str, object]
    attendance: list[dict[str, object]]
    leaves: list[dict[str, object]]


def _permitted_companies(authenticated: object) -> tuple[int, ...]:
    if not isinstance(authenticated, dict):
        raise ValueError("Company visibility is unavailable.")
    companies = authenticated.get("user_companies")
    allowed = companies.get("allowed_companies") if isinstance(companies, dict) else None
    if not isinstance(allowed, dict) or not allowed or len(allowed) > 128:
        raise ValueError("Company visibility is unavailable or exceeds its bound.")
    identifiers: list[int] = []
    for key, value in allowed.items():
        if not isinstance(value, dict) or type(value.get("id")) is not int:
            raise ValueError("Company visibility is invalid.")
        identifier = value["id"]
        if identifier <= 0 or str(identifier) != str(key) or identifier in identifiers:
            raise ValueError("Company visibility is invalid.")
        identifiers.append(identifier)
    return tuple(sorted(identifiers))


def _company_reference(row: dict[str, object]) -> OdooCalendarCompanyReference:
    identifier = row.get("id")
    if type(identifier) is not int or identifier < 1:
        raise ValueError("Company identity is unavailable.")
    relation = row.get("resource_calendar_id")
    calendar_id = None if relation is False else _id(relation)
    label = row.get("name")
    safe = (
        isinstance(label, str)
        and 0 < len(label) <= 160
        and not any(ord(c) < 32 for c in label)
        and re.search(r"https?://|(?:password|secret|token)\s*[:=]", label, re.IGNORECASE) is None
    )
    return OdooCalendarCompanyReference(
        company_id=identifier,
        calendar_id=calendar_id,
        label=label if safe and isinstance(label, str) else "",
        label_omitted=not safe,
    )


def read_calendar_sources(
    connection: OdooRuntimeConnection,
    *,
    company_id: int | None,
    observed_at: datetime,
    private_origin: bool = False,
    transport_origin: str = "",
    inspect_only: bool = False,
    expected_calendar_id: int | None = None,
) -> CalendarSourceObservation:
    origin = (transport_origin or connection.selection.base_url).rstrip("/")
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path
    ):
        raise ValueError("Calendar reads require a recorded HTTPS origin.")
    if not private_origin and not odoo_compose_has_domain(
        host=connection.host,
        token=connection.token,
        compose_id=connection.evidence.target_id,
        domain_host=url.hostname,
    ):
        raise ValueError("Calendar origin does not match the runtime.")
    authentication = diagnostic_authentication(connection)
    opener = build_opener(_NoRedirects(), HTTPCookieProcessor(CookieJar()))

    def health() -> None:
        payload = _http_json(opener, Request(origin + "/launchplane/health"))
        require_runtime_identity(
            connection.selection.identity, runtime_identity_from_health_payload(payload)
        )

    health()
    authenticated = authenticate_diagnostic(
        connection, lambda: _rpc(opener, origin, "/web/session/authenticate", authentication)
    )
    try:
        require_diagnostic_user(connection, authenticated)
        permitted = _permitted_companies(authenticated)
        if company_id is not None and company_id not in permitted:
            raise ValueError("Selected company is outside the authenticated visibility scope.")

        def search(
            model: str, domain: list[object], fields: list[str], *, allow_truncation: bool = False
        ) -> list[dict[str, object]]:
            value = _rpc(
                opener,
                origin,
                "/web/dataset/call_kw",
                {
                    "model": model,
                    "method": "search_read",
                    "args": [domain],
                    "kwargs": {
                        "fields": fields,
                        "limit": 129,
                        "order": "id",
                        "context": {
                            "allowed_company_ids": [company_id] if company_id else list(permitted)
                        },
                    },
                },
            )
            return _rows(value, maximum=129 if allow_truncation else 128)

        company_fields = ["id", "name", "resource_calendar_id"]
        companies = search(
            "res.company",
            [["id", "=", company_id]] if company_id else [["id", "in", list(permitted)]],
            company_fields,
            allow_truncation=inspect_only,
        )
        references = tuple(_company_reference(row) for row in companies)
        if len({row.company_id for row in references}) != len(references) or any(
            row.company_id not in permitted for row in references
        ):
            raise ValueError("Returned companies contradict the visibility scope.")
        if inspect_only:
            if company_id is not None and (
                len(references) != 1 or references[0].company_id != company_id
            ):
                raise ValueError("Selected company is unavailable.")
            if (
                company_id is None
                and len(references) <= 128
                and {row.company_id for row in references} != set(permitted)
            ):
                raise ValueError("Company visibility is incomplete.")
            result = CalendarSourceObservation(
                references[:128], permitted, len(references) > 128, {}, [], []
            )
        else:
            if company_id is None or len(companies) != 1 or references[0].company_id != company_id:
                raise ValueError("The selected company is unavailable.")
            calendar_id = _id(companies[0].get("resource_calendar_id"))
            if expected_calendar_id is not None and calendar_id != expected_calendar_id:
                raise ValueError("The company's calendar changed; inspect and review again.")
            calendar_fields = ["tz", "two_weeks_calendar", "flexible_hours", "duration_based"]
            calendars = search(
                "resource.calendar",
                [["id", "=", calendar_id]],
                ["id"] + calendar_fields,
            )
            if (
                len(calendars) != 1
                or type(calendars[0].get("id")) is not int
                or calendars[0].get("id") != calendar_id
            ):
                raise ValueError("The company calendar is unavailable.")
            schema = _rpc(
                opener,
                origin,
                "/web/dataset/call_kw",
                {
                    "model": "resource.calendar.attendance",
                    "method": "fields_get",
                    "args": [["resource_id"]],
                    "kwargs": {"attributes": ["type"]},
                },
            )
            if not isinstance(schema, dict):
                raise ValueError("Attendance schema is unavailable.")
            resource = schema.get("resource_id")
            if resource is not None and (
                not isinstance(resource, dict) or resource.get("type") != "many2one"
            ):
                raise ValueError("Resource-specific attendance has an unsupported schema.")
            fields = ["dayofweek", "hour_from", "hour_to", "week_type", "display_type"] + (
                ["resource_id"] if resource is not None else []
            )
            attendance = search(
                "resource.calendar.attendance", [["calendar_id", "=", calendar_id]], fields
            )
            leaves = search(
                "resource.calendar.leaves",
                [
                    ["calendar_id", "in", [False, calendar_id]],
                    ["company_id", "=", company_id],
                    ["resource_id", "=", False],
                    ["time_type", "=", "leave"],
                    ["date_to", ">=", observed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")],
                    [
                        "date_from",
                        "<=",
                        (observed_at + timedelta(days=15))
                        .astimezone(UTC)
                        .strftime("%Y-%m-%d %H:%M:%S"),
                    ],
                ],
                ["date_from", "date_to"],
            )
            result = CalendarSourceObservation(
                references, permitted, False, calendars[0], attendance, leaves
            )
    finally:
        destroy_diagnostic_session(
            connection,
            lambda: _rpc(opener, origin, "/web/session/destroy", {}, expect_result=False),
        )
    health()
    return result


def read_company_calendar(
    connection: OdooRuntimeConnection,
    *,
    company_id: int,
    observed_at: datetime,
    private_origin: bool = False,
    transport_origin: str = "",
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    result = read_calendar_sources(
        connection,
        company_id=company_id,
        observed_at=observed_at,
        private_origin=private_origin,
        transport_origin=transport_origin,
    )
    return result.calendar, result.attendance, result.leaves


def calendar_runtime_connection(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    private_endpoint_key: str,
    control_plane_root: Path,
) -> tuple[OdooRuntimeConnection, str, str]:
    selection = select_stable_runtime(store, profile, "prod")
    origin = ""
    endpoint_digest = ""
    if private_endpoint_key:
        endpoint = store.read_private_health_endpoint_record(private_endpoint_key)
        if (
            endpoint.status != "active"
            or endpoint.product != profile.product
            or endpoint.context != selection.identity.context
            or endpoint.instance != "prod"
        ):
            raise ValueError("Private calendar endpoint is outside the product lane.")
        parsed = urlsplit(endpoint.url)
        if parsed.scheme != "https":
            raise ValueError("Calendar reads require HTTPS.")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        endpoint_digest = private_health_endpoint_record_sha256(endpoint)
    connection = connect_runtime(
        store=store,
        selection=selection,
        control_plane_root=control_plane_root,
        database_url=store.database_url,
        diagnostics=True,
    )
    return connection, origin, endpoint_digest


def require_calendar_authority_current(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    connection: OdooRuntimeConnection,
    private_endpoint_key: str,
    endpoint_digest: str,
) -> str:
    if store.read_product_profile_record(profile.product) != profile:
        raise ValueError("Product profile changed during calendar qualification.")
    assert_selection_current(store, connection.selection)
    if (
        private_endpoint_key
        and private_health_endpoint_record_sha256(
            store.read_private_health_endpoint_record(private_endpoint_key)
        )
        != endpoint_digest
    ):
        raise ValueError("Private calendar transport changed during qualification.")
    return hashlib.sha256(
        json.dumps(
            {
                "identity": connection.selection.identity.model_dump(mode="json"),
                "base_url": connection.selection.base_url,
                "slot_target_id": connection.selection.slot_target_id,
                "route_host": connection.selection.route_host,
                "endpoint_digest": endpoint_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def calendar_window(
    calendar: dict[str, object],
    attendance: list[dict[str, object]],
    leaves: list[dict[str, object]],
    *,
    observed_at: datetime,
) -> BusinessHoursWindow:
    if observed_at.tzinfo is None:
        raise ValueError("Calendar evaluation requires an aware clock.")
    if calendar.get("flexible_hours") is not False or calendar.get("duration_based") is not False:
        raise ValueError("A fixed company calendar is required.")
    if type(calendar.get("two_weeks_calendar")) is not bool:
        raise ValueError("Calendar week mode is unavailable.")
    try:
        zone = ZoneInfo(_text(calendar["tz"]))
    except (KeyError, ZoneInfoNotFoundError) as error:
        raise ValueError("Calendar timezone is unavailable.") from error
    now = observed_at.astimezone(UTC)
    local_day = now.astimezone(zone).date()
    rows = [row for row in attendance if not row.get("display_type") and not row.get("resource_id")]
    if not rows:
        raise ValueError("The company calendar has no working hours.")
    intervals: list[tuple[datetime, datetime]] = []
    for offset in range(15):
        day = local_day + timedelta(days=offset)
        hours: list[tuple[float, float]] = []
        for row in rows:
            weekday = _text(row["dayofweek"])
            if weekday not in {str(value) for value in range(7)}:
                raise ValueError("Invalid weekday.")
            hour_start, hour_end = _hour(row["hour_from"]), _hour(row["hour_to"])
            if not 0 <= hour_start < hour_end <= 24:
                raise ValueError("Invalid attendance hours.")
            if calendar["two_weeks_calendar"] and row.get("week_type") not in {"0", "1"}:
                raise ValueError("Missing alternating-week evidence.")
            if weekday != str(day.weekday()) or (
                calendar["two_weeks_calendar"]
                and row["week_type"] != str(((day.toordinal() - 1) // 7) % 2)
            ):
                continue
            hours.append((hour_start, hour_end))
        if not hours:
            continue
        # Staff's business day includes meal breaks; a normal release waits for closing.
        midnight = datetime.combine(day, time.min)
        start = midnight + timedelta(hours=min(pair[0] for pair in hours))
        end = midnight + timedelta(hours=max(pair[1] for pair in hours))

        # Conservatively span both folds on a DST fallback; nonexistent local times hold.
        def utc_boundary(value: datetime, *, ending: bool) -> datetime:
            candidates = [value.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
            if any(
                candidate.astimezone(zone).replace(tzinfo=None) != value for candidate in candidates
            ):
                raise ValueError("Calendar boundary falls in a DST gap.")
            return max(candidates) if ending else min(candidates)

        intervals.append((utc_boundary(start, ending=False), utc_boundary(end, ending=True)))
    for row in leaves:
        start = datetime.strptime(_text(row["date_from"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        end = datetime.strptime(_text(row["date_to"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if end <= start:
            raise ValueError("Invalid company leave.")
        remaining = []
        for left, right in intervals:
            if end <= left or start >= right:
                remaining.append((left, right))
            else:
                if left < start:
                    remaining.append((left, start))
                if end < right:
                    remaining.append((end, right))
        intervals = remaining
    closing = now
    for start, end in sorted(intervals):
        if start <= closing < end:
            closing = end
        elif start > closing:
            break
    if closing == now:
        return BusinessHoursWindow(applies=True, timezone=zone.key)
    if closing >= datetime.combine(local_day + timedelta(days=14), time.min, zone).astimezone(UTC):
        raise ValueError("No out-of-hours window found in the bounded calendar.")
    return BusinessHoursWindow(
        applies=True,
        timezone=zone.key,
        next_release_at=closing.isoformat(),
        blocked_reason="Odoo business hours are in progress. This accepted version will release automatically after closing.",
    )


def read_business_hours_window(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    control_plane_root: Path | None,
    observed_at: datetime | None = None,
) -> BusinessHoursWindow:
    policy = profile.odoo_business_hours
    if profile.production_use != "live" or policy is None:
        return BusinessHoursWindow()
    try:
        if control_plane_root is None or not isinstance(store, PostgresRecordStore):
            raise ValueError("Calendar read source is unavailable.")
        connection, origin, endpoint_digest = calendar_runtime_connection(
            store=store,
            profile=profile,
            private_endpoint_key=policy.private_endpoint_key,
            control_plane_root=control_plane_root,
        )
        now = observed_at or datetime.now(UTC)
        calendar, rows, leaves = read_company_calendar(
            connection,
            company_id=policy.company_id,
            observed_at=now,
            private_origin=bool(policy.private_endpoint_key),
            transport_origin=origin,
        )
        require_calendar_authority_current(
            store=store,
            profile=profile,
            connection=connection,
            private_endpoint_key=policy.private_endpoint_key,
            endpoint_digest=endpoint_digest,
        )
        return calendar_window(calendar, rows, leaves, observed_at=observed_at or datetime.now(UTC))
    except (OSError, ValueError, KeyError, TypeError, click.ClickException, SQLAlchemyError):
        # Provider errors can contain credentials and private URLs; never surface them.
        return BusinessHoursWindow(
            applies=True,
            blocked_reason="Odoo business hours could not be read safely. This release waits until its company calendar is available, or a human records an urgent release and why.",
        )


def release_window(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    control_plane_root: Path | None,
) -> BusinessHoursWindow:
    if profile.production_use != "live" or profile.odoo_business_hours is None:
        return BusinessHoursWindow()
    if (
        decision.release_start == "rollback"
        or decision.decision == "temporary_hotfix"
        or decision.release_during_business_hours
    ):
        return BusinessHoursWindow()
    # Once the first promotion starts changing production, finish the release/drill;
    # a clock change must never leave staff on a half-finished release.
    from control_plane.client_release import client_release_step_operation_id, client_release_steps
    from control_plane.odoo_release_recovery import odoo_release_wrote_production

    if isinstance(store, PostgresRecordStore) and profile.driver_id == "odoo":
        step = next(
            (
                step
                for step in client_release_steps(decision.release_start)
                if step.kind == "promote"
            ),
            None,
        )
        if step is not None:
            try:
                operation = store.read_odoo_prod_promotion_operation_record(
                    client_release_step_operation_id(profile=profile, decision=decision, step=step)
                )
                if operation.status == "pass" or odoo_release_wrote_production(operation):
                    return BusinessHoursWindow()
            except FileNotFoundError:
                pass
    return read_business_hours_window(
        store=store, profile=profile, control_plane_root=control_plane_root
    )


def require_release_window(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    control_plane_root: Path,
) -> None:
    window = release_window(
        store=store, profile=profile, decision=decision, control_plane_root=control_plane_root
    )
    if window.blocked_reason:
        raise BusinessHoursHeld(window.blocked_reason)
