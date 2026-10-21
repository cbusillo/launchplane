"""Opt-in topic reminders and durable Director failure alerts.

The CM sender is an injected service capability. The current Discord Blue
conversation socket requires an agent session and must not be impersonated here.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import logging
import json
from pathlib import Path
import time
from typing import Annotated, Protocol
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord, build_outbox_delivery_id
from control_plane.notifications import DiscordNotificationError, public_discord_url_error
from control_plane.preview_lifecycle_scheduler import _PreviewScheduler, _timestamp
from control_plane.storage.postgres import PostgresRecordStore, OutboxWithIdempotencyRequest
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    managed_notification_secret_value,
)

SETTINGS_KEY = "LAUNCHPLANE_DISCUSSION_REMINDERS"
DELIVERY_WINDOW = timedelta(minutes=15)
_LOGGER = logging.getLogger(__name__)
ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]


class WeeklyReminder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    weekday: int = Field(ge=0, le=6)
    time: ClockTime


class DiscussionReminderSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    product: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    enabled: bool = False
    bridge_released: bool = False
    invitation_confirmed: bool = False
    timezone: str
    times: tuple[WeeklyReminder, ...] = Field(min_length=1, max_length=14)
    overrides: dict[str, ClockTime | None] = Field(default_factory=dict, max_length=52)
    meeting: str = Field(min_length=1, max_length=120)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Reminder timezone must name an IANA timezone") from error
        return value

    @field_validator("meeting")
    @classmethod
    def plain_meeting(cls, value: str) -> str:
        if not value.strip() or any(char in value for char in "\r\n@<>`[]") or "://" in value:
            raise ValueError("Meeting text must be a plain line without mentions or links")
        return value

    @field_validator("overrides")
    @classmethod
    def valid_dates(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        for key in value:
            if date.fromisoformat(key).isoformat() != key:
                raise ValueError("Override dates require YYYY-MM-DD")
        return value

    @property
    def active(self) -> bool:
        return self.enabled and self.bridge_released and self.invitation_confirmed


class DirectorDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)


class DiscussionReminderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    director: DirectorDestination
    schedules: tuple[DiscussionReminderSchedule, ...] = Field(default=(), max_length=100)

    @field_validator("schedules")
    @classmethod
    def unique_products(
        cls, value: tuple[DiscussionReminderSchedule, ...]
    ) -> tuple[DiscussionReminderSchedule, ...]:
        if len({schedule.product for schedule in value}) != len(value):
            raise ValueError("Only one reminder schedule is allowed per product")
        return value


class DiscussionReminderSender(Protocol):
    def __call__(self, *, product: str, delivery_id: str, content: str) -> None: ...


def read_settings(store: PostgresRecordStore) -> DiscussionReminderSettings | None:
    value = _settings_value(store)
    return DiscussionReminderSettings.model_validate_json(value) if value is not None else None


def _settings_value(store: PostgresRecordStore) -> str | None:
    records = store.list_runtime_environment_records(scope="context", context_name="launchplane")
    values = [record.env[SETTINGS_KEY] for record in records if SETTINGS_KEY in record.env]
    if not values:
        return None
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError("Reminder settings require one service-context JSON value")
    return values[0]


def due_reminders(schedule: DiscussionReminderSchedule, now: datetime) -> tuple[datetime, ...]:
    """Bound catch-up; use the first fold and skip nonexistent local DST times."""
    zone = ZoneInfo(schedule.timezone)
    today = now.astimezone(zone).date()
    due: set[datetime] = set()
    for day in (today - timedelta(days=1), today):
        key = day.isoformat()
        times = (
            [schedule.overrides[key]]
            if key in schedule.overrides
            else [slot.time for slot in schedule.times if slot.weekday == day.weekday()]
        )
        for local_time in times:
            if local_time is None:
                continue
            local = datetime.fromisoformat(f"{key}T{local_time}").replace(tzinfo=zone, fold=0)
            utc = local.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            if timedelta(0) <= now - utc < DELIVERY_WINDOW:
                due.add(utc)
    return tuple(sorted(due))


def _enqueue(
    store: PostgresRecordStore,
    *,
    key: str,
    product: str,
    now: float,
    payload: dict[str, object],
) -> None:
    store.enqueue_outbox_delivery_with_idempotency(
        OutboxWithIdempotencyRequest(
            delivery=OutboxDeliveryRecord(
                delivery_id=build_outbox_delivery_id(kind="discussion_reminder", dedupe_key=key),
                kind="discussion_reminder",
                aggregate_type="discussion_reminder",
                aggregate_id=product,
                dedupe_key=key,
                created_at=_timestamp(now),
                updated_at=_timestamp(now),
                next_attempt_at=_timestamp(now),
                payload=payload,
            )
        )
    )


class DiscussionReminderScheduler(_PreviewScheduler):
    route = "service:discussion-reminders"

    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        control_plane_root: Path,
        public_url: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(store, control_plane_root=control_plane_root, clock=clock)
        self.public_url = public_url
        self._configuration_error_day: int | None = None

    def _run_sweep(self) -> dict[str, object]:
        try:
            self._enqueue_due(read_settings(self.store))
        except Exception:
            # Parse the destination independently when delivering this alert,
            # so invalid schedule fields do not prevent failure notification.
            _enqueue(
                self.store,
                key=f"discussion-reminder:configuration:{int(self.clock() // 86400)}",
                product="configuration",
                now=self.clock(),
                payload={"alert": True, "failure_code": "reminder_configuration_unavailable"},
            )
            day = int(self.clock() // 86400)
            if self._configuration_error_day != day:
                _LOGGER.error("Discussion reminder configuration unavailable")
                self._configuration_error_day = day
            return {"status": "error", "apply": False}
        return {"status": "pass", "apply": False}

    def _enqueue_due(self, settings: DiscussionReminderSettings | None) -> None:
        if settings is None:
            return
        active = tuple(schedule for schedule in settings.schedules if schedule.active)
        if not active:
            return
        parsed = urlsplit(self.public_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Reminder links require the service's public HTTPS origin")
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        for schedule in active:
            if self._stop.is_set():
                continue
            for due in due_reminders(schedule, now):
                _enqueue(
                    self.store,
                    key=f"discussion-reminder:{schedule.product}:{due.isoformat()}",
                    product=schedule.product,
                    now=self.clock(),
                    payload={
                        "alert": False,
                        "due_at": due.isoformat(),
                        "public_url": self.public_url,
                    },
                )

    def run_once(self) -> bool:
        # Atomic outbox deduplication is sufficient, including across replicas;
        # avoid persisting one empty scheduler receipt per minute forever.
        self._run_sweep()
        return True


def deliver_discussion_reminder(
    *,
    store: PostgresRecordStore,
    record: OutboxDeliveryRecord,
    drivers: PublicIngressNotificationDriverSet,
    sender: DiscussionReminderSender | None,
    mark_provider_started: Callable[[str, str], None],
    clock: Callable[[], float],
) -> OutboxDeliveryRecord:
    started = bool(record.provider_operation_key)
    marking = False

    def result(state: str, *, action: str = "", error: str = "") -> OutboxDeliveryRecord:
        return record.model_copy(
            update={
                "state": state,
                "action": action,
                "error_code": error,
                "provider_operation_key": record.delivery_id if started else "",
                "provider_id": "discord" if started else "",
                "lease_owner": "",
                "lease_expires_at": "",
            }
        )

    def failed(code: str, *, retry: bool = False) -> OutboxDeliveryRecord:
        _enqueue(
            store,
            key=f"{record.dedupe_key}:failure:{code}",
            product=record.aggregate_id,
            now=clock(),
            payload={"alert": True, "failure_code": code},
        )
        return result("pending" if retry else "failed", error=code)

    is_alert = record.payload.get("alert") is True
    if started:
        if is_alert:
            _LOGGER.critical("Director reminder alert outcome unknown: %s", record.delivery_id)
            return result("failed", error="alert_outcome_unknown")
        return failed("reminder_outcome_unknown")
    try:
        if is_alert:
            raw = _settings_value(store)
            if raw is None:
                raise ValueError("Director reminder destination unavailable")
            destination = DirectorDestination.model_validate(json.loads(raw)["director"])
            policy, target = next(
                (policy, target)
                for policy in store.list_public_ingress_notification_policy_records(
                    product="launchplane",
                    status="enabled",
                )
                if policy.policy_id == destination.policy_id
                and policy.product == "launchplane"
                and policy.purpose == "incident_alerts"
                and not policy.check_name
                and not policy.check_kind
                for target in policy.destinations
                if target.destination_id == destination.destination_id
                and target.status == "enabled"
                and target.kind == "discord"
            )
            webhook = drivers.secret_resolver(
                target.discord_webhook_secret
            ) or managed_notification_secret_value(
                store,
                target.discord_webhook_secret,
                policy.context,
                policy.instance,
            )
            if not webhook or public_discord_url_error(webhook):
                raise ValueError("Director reminder destination unavailable")
            content = (
                f"Discussion reminder needs attention for {record.aggregate_id}. "
                f"Code: {record.payload.get('failure_code')}. "
                "Check Launchplane delivery records before retrying."
            )
            marking = True
            mark_provider_started(record.delivery_id, "discord")
            marking = False
            started = True
            drivers.discord_sender(webhook, {"content": content, "allowed_mentions": {"parse": []}})
            return result("delivered", action="sent_director_reminder_alert")
        settings = read_settings(store)
        schedule = (
            next((item for item in settings.schedules if item.product == record.aggregate_id), None)
            if settings
            else None
        )
        if schedule is None or not schedule.active:
            return result("delivered", action="skipped_changed_schedule")
        due = datetime.fromisoformat(str(record.payload["due_at"]))
        if due not in due_reminders(schedule, due):
            return result("delivered", action="skipped_changed_schedule")
        now = datetime.fromtimestamp(clock(), timezone.utc)
        if not timedelta(0) <= now - due < DELIVERY_WINDOW:
            return failed("reminder_slot_expired")
        profile = store.read_product_profile_record(record.aggregate_id)
        if not profile.is_active:
            return result("delivered", action="skipped_retired_product")
        count = len(store.list_discussion_topics(schedule.product))
        heading = (
            f"{count} topic{'s' if count != 1 else ''} to discuss"
            if count
            else "No topics to discuss"
        )
        link = f"{str(record.payload['public_url']).rstrip('/')}/ui/products/{quote(schedule.product, safe='')}/topics"
        if sender is None:
            return failed("cm_sender_unavailable")
        marking = True
        mark_provider_started(record.delivery_id, "discord")
        marking = False
        started = True
        sender(
            product=schedule.product,
            delivery_id=record.delivery_id,
            content=f"{heading}. Meeting: {schedule.meeting}.\n{link}",
        )
        return result("delivered", action="sent_discussion_reminder")
    except Exception as error:
        if marking:
            # Ownership/marker writes are their own phase. A rejected or
            # ambiguous marker cannot publish an alert or attempt a send.
            raise
        if is_alert:
            if isinstance(error, DiscordNotificationError) and 400 <= error.status_code < 500:
                started = False  # Confirmed provider refusal, safe to clear on owned completion.
                code = "alert_rate_limited" if error.status_code == 429 else "alert_send_rejected"
                retry = error.status_code == 429 and record.attempt < record.max_attempts
            else:
                code = "alert_outcome_unknown" if started else "alert_destination_unavailable"
                retry = not started and record.attempt < record.max_attempts
            if not retry:
                _LOGGER.critical("Director reminder alert failed: %s", record.delivery_id)
            return result(
                "pending" if retry else "failed",
                error=code,
            )
        if started:
            return failed("reminder_outcome_unknown")
        due = datetime.fromisoformat(str(record.payload["due_at"]))
        retry = (
            record.attempt < record.max_attempts
            and timedelta(0)
            <= datetime.fromtimestamp(clock(), timezone.utc) - due
            < DELIVERY_WINDOW
        )
        return failed(
            "reminder_read_retry_pending" if retry else "reminder_read_failed", retry=retry
        )
