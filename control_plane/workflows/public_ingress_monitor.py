from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener
import ipaddress
import json
import socket
import ssl

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressFailureCode,
    PublicIngressIncidentRecord,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressTargetKind,
    PublicIngressTargetObservation,
    build_public_ingress_incident_id,
    build_public_ingress_observation_id,
)
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    RuntimeIdentityStatus,
    health_payload_runtime_identity_status,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.ship import utc_now_timestamp


MAX_REDIRECTS = 10
USER_AGENT = "Launchplane public-ingress-monitor/1.0"


class PublicIngressMonitorStore(Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]: ...

    def write_public_ingress_observation_record(
        self, record: PublicIngressObservationRecord
    ) -> object: ...

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentRecord, ...]: ...

    def write_public_ingress_incident_record(
        self, record: PublicIngressIncidentRecord
    ) -> object: ...


@dataclass(frozen=True)
class PublicIngressMonitorTarget:
    product: str
    repository: str
    driver_id: str
    context: str
    instance: str
    base_url: str
    health_url: str
    expected_runtime_identity: RuntimeIdentity | None
    require_runtime_identity: bool
    alert_issue_url: str


@dataclass(frozen=True)
class HttpObservation:
    status_code: int
    final_url: str
    redirect_count: int
    payload: object = None


class PublicIngressMonitorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_at: str
    target_count: int = Field(ge=0)
    pass_count: int = Field(default=0, ge=0)
    fail_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    notification_count: int = Field(default=0, ge=0)
    open_incident_count: int = Field(default=0, ge=0)
    resolved_incident_count: int = Field(default=0, ge=0)
    records: tuple[PublicIngressObservationRecord, ...] = ()
    incidents: tuple[PublicIngressIncidentRecord, ...] = ()


HttpGet = Callable[[str, int], HttpObservation]
Notifier = Callable[[PublicIngressObservationRecord, PublicIngressObservationRecord | None], bool]


class _RedirectTrackingHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        old_url = req.full_url
        if _normalized_url(old_url) == _normalized_url(newurl):
            raise RuntimeError(f"self redirect from {old_url}")
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request is not None:
            redirects = int(getattr(req, "redirect_count", 0)) + 1
            setattr(request, "redirect_count", redirects)
            if redirects > MAX_REDIRECTS:
                raise RuntimeError(f"redirect loop after {MAX_REDIRECTS} redirects")
        return request


def discover_public_ingress_monitor_targets(
    record_store: PublicIngressMonitorStore,
) -> tuple[PublicIngressMonitorTarget, ...]:
    targets: list[PublicIngressMonitorTarget] = []
    for profile in record_store.list_product_profile_records():
        if not _profile_uses_generic_web(profile):
            continue
        for lane in profile.lanes:
            if not lane.public_ingress_monitoring.enabled:
                continue
            base_url = lane.base_url.strip().rstrip("/")
            health_url = (
                lane.health_url.strip() or _health_url(base_url, profile.health_path)
            ).strip()
            if not (base_url or health_url):
                continue
            targets.append(
                PublicIngressMonitorTarget(
                    product=profile.product,
                    repository=profile.repository,
                    driver_id=profile.driver_id,
                    context=lane.context,
                    instance=lane.instance,
                    base_url=base_url,
                    health_url=health_url,
                    expected_runtime_identity=_expected_runtime_identity(
                        record_store=record_store,
                        lane=lane,
                    ),
                    require_runtime_identity=lane.public_ingress_monitoring.require_runtime_identity,
                    alert_issue_url=lane.public_ingress_monitoring.alert_issue_url,
                )
            )
    return tuple(targets)


def run_public_ingress_monitor_once(
    *,
    record_store: PublicIngressMonitorStore,
    checked_at: str = "",
    timeout_seconds: int = 10,
    notify: bool = True,
    http_get: HttpGet | None = None,
    notifier: Notifier | None = None,
) -> PublicIngressMonitorResult:
    observed_at = checked_at.strip() or utc_now_timestamp()
    get = http_get or fetch_public_ingress_url
    records: list[PublicIngressObservationRecord] = []
    incidents: list[PublicIngressIncidentRecord] = []
    notification_count = 0
    for target in discover_public_ingress_monitor_targets(record_store):
        previous_record = _latest_observation(record_store=record_store, target=target)
        record = check_public_ingress_target(
            target=target,
            checked_at=observed_at,
            timeout_seconds=timeout_seconds,
            http_get=get,
        )
        if notify and notifier is not None and _should_notify(record, previous_record):
            notification_sent = notifier(record, previous_record)
            record = record.model_copy(update={"notification_sent": notification_sent})
            if notification_sent:
                notification_count += 1
        record_store.write_public_ingress_observation_record(record)
        records.append(record)
        incident = reconcile_public_ingress_incident(
            record_store=record_store,
            record=record,
            previous_record=previous_record,
        )
        if incident is not None:
            incidents.append(incident)
    return PublicIngressMonitorResult(
        checked_at=observed_at,
        target_count=len(records),
        pass_count=sum(1 for record in records if record.status == "pass"),
        fail_count=sum(1 for record in records if record.status == "fail"),
        skipped_count=sum(1 for record in records if record.status == "skipped"),
        notification_count=notification_count,
        open_incident_count=sum(1 for incident in incidents if incident.status == "open"),
        resolved_incident_count=sum(
            1 for incident in incidents if incident.status == "resolved"
        ),
        records=tuple(records),
        incidents=tuple(incidents),
    )


def reconcile_public_ingress_incident(
    *,
    record_store: PublicIngressMonitorStore,
    record: PublicIngressObservationRecord,
    previous_record: PublicIngressObservationRecord | None,
) -> PublicIngressIncidentRecord | None:
    open_incident = _open_incident(record_store=record_store, record=record)
    if record.status == "fail":
        if open_incident is not None:
            incident = open_incident.model_copy(
                update={
                    "latest_observation_id": record.record_id,
                    "latest_observed_at": record.observed_at,
                    "failure_code": record.failure_code,
                    "summary": record.summary,
                }
            )
        else:
            incident = PublicIngressIncidentRecord(
                incident_id=build_public_ingress_incident_id(
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                    opened_at=record.observed_at,
                ),
                product=record.product,
                repository=record.repository,
                driver_id=record.driver_id,
                context=record.context,
                instance=record.instance,
                status="open",
                opened_at=record.observed_at,
                opened_observation_id=record.record_id,
                latest_observation_id=record.record_id,
                latest_observed_at=record.observed_at,
                failure_code=record.failure_code or "unknown_error",
                summary=record.summary,
            )
        record_store.write_public_ingress_incident_record(incident)
        return incident
    if record.status == "pass" and open_incident is not None:
        incident = open_incident.model_copy(
            update={
                "status": "resolved",
                "latest_observation_id": record.record_id,
                "latest_observed_at": record.observed_at,
                "resolved_at": record.observed_at,
                "resolved_observation_id": record.record_id,
                "summary": record.summary,
            }
        )
        record_store.write_public_ingress_incident_record(incident)
        return incident
    return None


def check_public_ingress_target(
    *,
    target: PublicIngressMonitorTarget,
    checked_at: str,
    timeout_seconds: int,
    http_get: HttpGet,
) -> PublicIngressObservationRecord:
    target_observations: list[PublicIngressTargetObservation] = []
    for target_kind, url in _target_urls(target):
        target_observations.append(
            _check_url(
                target_kind=target_kind,
                url=url,
                timeout_seconds=timeout_seconds,
                target=target,
                http_get=http_get,
            )
        )
    status, failure_code = _record_status(target_observations)
    summary = _record_summary(target=target, status=status, observations=target_observations)
    return PublicIngressObservationRecord(
        record_id=build_public_ingress_observation_id(
            product=target.product,
            context=target.context,
            instance=target.instance,
            observed_at=checked_at,
        ),
        product=target.product,
        repository=target.repository,
        driver_id=target.driver_id,
        context=target.context,
        instance=target.instance,
        observed_at=checked_at,
        status=status,
        failure_code=failure_code,
        base_url=target.base_url,
        health_url=target.health_url,
        expected_runtime_identity=target.expected_runtime_identity,
        targets=tuple(target_observations),
        notification_key=_notification_key(target),
        summary=summary,
    )


def fetch_public_ingress_url(url: str, timeout_seconds: int) -> HttpObservation:
    opener: OpenerDirector = build_opener(_RedirectTrackingHandler)
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator-configured product URLs.
        body = response.read(1024 * 256)
        payload: object = None
        content_type = response.headers.get("content-type", "")
        if body and "json" in content_type.lower():
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
        return HttpObservation(
            status_code=response.status,
            final_url=response.geturl(),
            redirect_count=int(getattr(response, "redirect_count", 0)),
            payload=payload,
        )


def _check_url(
    *,
    target_kind: PublicIngressTargetKind,
    url: str,
    timeout_seconds: int,
    target: PublicIngressMonitorTarget,
    http_get: HttpGet,
) -> PublicIngressTargetObservation:
    public_url_error = _public_url_error(url)
    if public_url_error is not None:
        status: PublicIngressObservationStatus = (
            "skipped" if public_url_error == "private_url" else "fail"
        )
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status=status,
            failure_code=public_url_error,
            summary=_failure_summary(public_url_error),
        )
    try:
        observation = http_get(url, timeout_seconds)
    except HTTPError as error:
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code=("health_status_error" if target_kind == "health_url" else "http_error"),
            http_status=error.code,
            final_url=error.url or "",
            summary=f"HTTP {error.code}",
        )
    except TimeoutError:
        return _failed_target(target_kind=target_kind, url=url, code="connection_timeout")
    except URLError as error:
        return _url_error_observation(target_kind=target_kind, url=url, error=error)
    except ssl.SSLError:
        return _failed_target(target_kind=target_kind, url=url, code="tls_failure")
    except RuntimeError as error:
        message = str(error).lower()
        code: PublicIngressFailureCode = "redirect_loop"
        if "self redirect" in message:
            code = "self_redirect"
        return _failed_target(
            target_kind=target_kind,
            url=url,
            code=code,
            summary=str(error),
        )
    except OSError as error:
        return _failed_target(
            target_kind=target_kind,
            url=url,
            code="unknown_error",
            summary=str(error),
        )
    if not 200 <= observation.status_code < 300:
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code=("health_status_error" if target_kind == "health_url" else "http_error"),
            http_status=observation.status_code,
            final_url=observation.final_url,
            redirect_count=observation.redirect_count,
            summary=f"HTTP {observation.status_code}",
        )
    runtime_status: RuntimeIdentityStatus = "unchecked"
    runtime_detail = ""
    observed_runtime_identity: RuntimeIdentity | None = None
    if target_kind == "health_url" and target.expected_runtime_identity is not None:
        runtime_status, runtime_detail, observed_runtime_identity = (
            health_payload_runtime_identity_status(
                expected=target.expected_runtime_identity,
                payload=observation.payload,
                json_parse_failed=False,
            )
        )
        if target.require_runtime_identity and runtime_status != "match":
            return PublicIngressTargetObservation(
                target=target_kind,
                url=url,
                status="fail",
                failure_code="wrong_runtime_identity",
                http_status=observation.status_code,
                final_url=observation.final_url,
                redirect_count=observation.redirect_count,
                runtime_identity_status=runtime_status,
                runtime_identity_detail=runtime_detail,
                observed_runtime_identity=observed_runtime_identity,
                summary=runtime_detail or "Runtime identity did not match expected deployment.",
            )
    return PublicIngressTargetObservation(
        target=target_kind,
        url=url,
        status="pass",
        http_status=observation.status_code,
        final_url=observation.final_url,
        redirect_count=observation.redirect_count,
        runtime_identity_status=runtime_status,
        runtime_identity_detail=runtime_detail,
        observed_runtime_identity=observed_runtime_identity,
        summary="Public ingress returned a successful response.",
    )


def _profile_uses_generic_web(profile: LaunchplaneProductProfileRecord) -> bool:
    if profile.driver_id == "generic-web":
        return True
    try:
        return read_driver_descriptor(profile.driver_id).base_driver_id == "generic-web"
    except FileNotFoundError:
        return False


def _expected_runtime_identity(
    *, record_store: object, lane: ProductLaneProfile
) -> RuntimeIdentity | None:
    read_lane_summary = getattr(record_store, "read_lane_summary", None)
    if not callable(read_lane_summary):
        return None
    try:
        lane_summary = read_lane_summary(
            context_name=lane.context,
            instance_name=lane.instance,
        )
    except (FileNotFoundError, KeyError):
        return None
    if not isinstance(lane_summary, LaunchplaneLaneSummary):
        return None
    if lane_summary.inventory is not None:
        return lane_summary.inventory.runtime_identity
    if lane_summary.latest_deployment is not None:
        return lane_summary.latest_deployment.runtime_identity
    return None


def _latest_observation(
    *, record_store: PublicIngressMonitorStore, target: PublicIngressMonitorTarget
) -> PublicIngressObservationRecord | None:
    records = record_store.list_public_ingress_observation_records(
        product=target.product,
        context_name=target.context,
        instance_name=target.instance,
        limit=1,
    )
    return next(iter(records), None)


def _open_incident(
    *, record_store: PublicIngressMonitorStore, record: PublicIngressObservationRecord
) -> PublicIngressIncidentRecord | None:
    incidents = record_store.list_public_ingress_incident_records(
        product=record.product,
        context_name=record.context,
        instance_name=record.instance,
        status="open",
        limit=1,
    )
    return next(iter(incidents), None)


def _should_notify(
    record: PublicIngressObservationRecord, previous: PublicIngressObservationRecord | None
) -> bool:
    if not record.notification_key:
        return False
    previous_status = previous.status if previous is not None else "missing"
    if record.status == "fail" and previous_status != "fail":
        return True
    return record.status == "pass" and previous_status == "fail"


def _target_urls(
    target: PublicIngressMonitorTarget,
) -> tuple[tuple[PublicIngressTargetKind, str], ...]:
    urls: list[tuple[PublicIngressTargetKind, str]] = []
    if target.base_url:
        urls.append(("base_url", target.base_url))
    if target.health_url and target.health_url != target.base_url:
        urls.append(("health_url", target.health_url))
    return tuple(urls)


def _record_status(
    observations: list[PublicIngressTargetObservation],
) -> tuple[PublicIngressObservationStatus, PublicIngressFailureCode | None]:
    failing = next(
        (observation for observation in observations if observation.status == "fail"), None
    )
    if failing is not None:
        return "fail", failing.failure_code
    if all(observation.status == "skipped" for observation in observations):
        return "skipped", "private_url"
    return "pass", None


def _record_summary(
    *,
    target: PublicIngressMonitorTarget,
    status: PublicIngressObservationStatus,
    observations: list[PublicIngressTargetObservation],
) -> str:
    if status == "pass":
        return f"Public ingress is reachable for {target.product}/{target.instance}."
    failing = next(
        (observation for observation in observations if observation.status == "fail"), None
    )
    if failing is not None:
        return f"Public ingress failed for {target.product}/{target.instance}: {failing.summary}"
    return f"Public ingress monitoring skipped {target.product}/{target.instance}."


def _public_url_error(url: str) -> PublicIngressFailureCode | None:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "invalid_url"
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "private_url"
    try:
        if ipaddress.ip_address(hostname).is_private:
            return "private_url"
    except ValueError:
        pass
    if (
        hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname.endswith(".invalid")
    ):
        return "private_url"
    return None


def _url_error_observation(
    *, target_kind: PublicIngressTargetKind, url: str, error: URLError
) -> PublicIngressTargetObservation:
    reason = error.reason
    code: PublicIngressFailureCode = "unknown_error"
    if isinstance(reason, TimeoutError):
        code = "connection_timeout"
    elif isinstance(reason, ssl.SSLError):
        code = "tls_failure"
    elif isinstance(reason, socket.gaierror):
        code = "dns_failure"
    return _failed_target(target_kind=target_kind, url=url, code=code, summary=str(reason))


def _failed_target(
    *,
    target_kind: PublicIngressTargetKind,
    url: str,
    code: PublicIngressFailureCode,
    summary: str = "",
) -> PublicIngressTargetObservation:
    return PublicIngressTargetObservation(
        target=target_kind,
        url=url,
        status="fail",
        failure_code=code,
        summary=summary.strip() or _failure_summary(code),
    )


def _failure_summary(code: PublicIngressFailureCode) -> str:
    return code.replace("_", " ")


def _health_url(base_url: str, health_path: str) -> str:
    if not base_url.strip():
        return ""
    return f"{base_url.rstrip('/')}{health_path}"


def _normalized_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    path = parsed.path or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _notification_key(target: PublicIngressMonitorTarget) -> str:
    return target.alert_issue_url or ""


def public_ingress_alert_body(
    record: PublicIngressObservationRecord,
    previous: PublicIngressObservationRecord | None,
) -> str:
    state = "RECOVERED" if record.status == "pass" else "FAILED"
    previous_status = previous.status if previous is not None else "missing"
    lines = [
        f"Launchplane public ingress {state}: {record.product}/{record.instance}",
        "",
        f"- status: {record.status}",
        f"- previous_status: {previous_status}",
        f"- observed_at: {record.observed_at}",
        f"- context: {record.context}",
    ]
    if record.failure_code:
        lines.append(f"- failure_code: {record.failure_code}")
    for target in record.targets:
        lines.append(f"- {target.target}: {target.status} {target.summary}")
    return "\n".join(lines)


def build_github_issue_notifier(
    *, gh_binary: str = "gh", runner: Callable[..., object] | None = None
) -> Notifier:
    import subprocess

    def notify(
        record: PublicIngressObservationRecord,
        previous: PublicIngressObservationRecord | None,
    ) -> bool:
        issue_url = record.notification_key.strip()
        if not issue_url:
            return False
        command = [
            gh_binary,
            "issue",
            "comment",
            issue_url,
            "--body",
            public_ingress_alert_body(record, previous),
        ]
        if runner is not None:
            result = runner(command)
            return getattr(result, "returncode", 0) == 0
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        return completed.returncode == 0

    return notify
