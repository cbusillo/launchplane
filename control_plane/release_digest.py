"""Calm daily release lists, independent of incidents and release authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import logging
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord, build_outbox_delivery_id
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.preview_lifecycle_scheduler import _PreviewScheduler, _timestamp
from control_plane.product_reconcile import resolve_build_provenance_transport
from control_plane.release_review import (
    current_release_review,
    release_version,
    ReleaseEvidenceUnavailable,
)
from control_plane.contracts.release_review import ReleaseVersion
from control_plane.notifications import DiscordNotificationError, public_discord_url_error
from control_plane.source_control_change import read_branch_revision
from control_plane.storage.postgres import PostgresRecordStore, OutboxWithIdempotencyRequest
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    managed_notification_secret_value,
)

_LOGGER = logging.getLogger(__name__)


class DigestAction(StrEnum):
    PROMOTION = "promotion"
    INVITATION = "invitation"


class DigestBlocker(StrEnum):
    TESTING_BEHIND = "testing_behind"
    TESTING_UNHEALTHY = "testing_unhealthy"
    TESTING_UNAVAILABLE = "testing_unavailable"
    REVIEW_UNAVAILABLE = "review_unavailable"
    ACCEPTANCE_PENDING = "acceptance_pending"
    RELEASE_HELD = "release_held"


_BLOCKER_TEXT = {
    DigestBlocker.TESTING_BEHIND: "Latest changes are not in testing",
    DigestBlocker.TESTING_UNHEALTHY: "Testing is not green",
    DigestBlocker.TESTING_UNAVAILABLE: "Testing evidence unavailable",
    DigestBlocker.REVIEW_UNAVAILABLE: "Release review unavailable",
    DigestBlocker.ACCEPTANCE_PENDING: "Client acceptance pending",
    DigestBlocker.RELEASE_HELD: "Releases are on hold",
}


@dataclass(frozen=True)
class WaitingReleaseList:
    content: str
    production: ReleaseVersion
    head_commit: str
    waiting_hours: int | None
    blockers: tuple[DigestBlocker, ...]
    action: DigestAction


def _landing_age(commits: list[object], base: str, head: str, now: datetime) -> int | None:
    # Only first-parent commits measure time on the default branch. Feature
    # branch dates can precede their merge by months. Truncation stays unknown.
    by_sha = {item.get("sha"): item for item in commits if isinstance(item, dict)}
    dates = []
    seen: set[str] = set()
    while head != base:
        item = by_sha.get(head)
        if not isinstance(item, dict) or head in seen:
            return None
        seen.add(head)
        parents = item.get("parents")
        metadata = item.get("commit")
        if not isinstance(parents, list) or not parents or not isinstance(parents[0], dict):
            return None
        if not isinstance(metadata, dict) or not isinstance(metadata.get("committer"), dict):
            return None
        try:
            dates.append(
                datetime.fromisoformat(str(metadata["committer"]["date"]).replace("Z", "+00:00"))
            )
            head = str(parents[0]["sha"])
        except (KeyError, ValueError):
            return None
    try:
        return max(0, int((now - min(dates)).total_seconds() // 3600))
    except (ValueError, TypeError):
        return None


def waiting_release_list(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    *,
    read: Callable[[str], object],
    now: datetime,
    public_url: str,
    control_plane_root: Path,
) -> WaitingReleaseList | None:
    """List default-branch commits and testing-only release components without effects."""
    production = release_version(store=store, profile=profile, instance="prod")
    try:
        candidate = release_version(store=store, profile=profile, instance="testing")
    except ReleaseEvidenceUnavailable:
        candidate = None
    head = read_branch_revision(
        repository=profile.repository, branch=profile.default_branch, read=read
    )
    if head is None:
        raise ValueError("Release source unavailable")
    components: list[str] = []
    if candidate is not None and candidate.source_commit in {production.source_commit, head.commit}:
        if candidate.shared_addons_digest != production.shared_addons_digest:
            components.append("Shared addon changes in testing")
        if candidate.website_settings_sha256 != production.website_settings_sha256:
            components.append("Website settings changes in testing")
        if (
            candidate.artifact_id != production.artifact_id
            and candidate.source_commit == production.source_commit
            and not components
        ):
            components.append("Updated artifact in testing")
    if head.commit == production.source_commit and not components:
        return None
    repository = "/".join(quote(part, safe="") for part in profile.repository.split("/"))
    total = 0
    lines: list[str] = []
    age: int | None = None
    if head.commit != production.source_commit:
        comparison = read(f"/repos/{repository}/compare/{production.source_commit}...{head.commit}")
        if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
            raise ValueError("Release ancestry unavailable")
        commits, count = comparison.get("commits"), comparison.get("total_commits")
        if (
            not isinstance(commits, list)
            or not commits
            or not isinstance(count, int)
            or count < len(commits)
        ):
            raise ValueError("Release changes unavailable")
        total = count
        age = _landing_age(commits, production.source_commit, head.commit, now)
        for commit in commits[:5]:
            if not isinstance(commit, dict) or not isinstance(commit.get("commit"), dict):
                raise ValueError("Release change metadata unavailable")
            title = str(commit["commit"].get("message", "")).splitlines()[0:1]
            lines.append("  - " + _plain(title[0] if title else "Change", 120))
    lines.extend("  - " + component for component in components)
    review = current_release_review(
        control_plane_root=control_plane_root, record_store=store, profile=profile
    )
    blockers: list[DigestBlocker] = []
    testing = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
    if candidate is None or testing is None:
        blockers.append(DigestBlocker.TESTING_UNAVAILABLE)
    else:
        if candidate.source_commit != head.commit:
            blockers.append(DigestBlocker.TESTING_BEHIND)
        try:
            summary = store.read_lane_summary(context_name=testing.context, instance_name="testing")
            evidence = summary.inventory or summary.latest_deployment
            if evidence is None:
                blockers.append(DigestBlocker.TESTING_UNAVAILABLE)
            elif evidence.destination_health.status != "pass" or evidence.deploy.status != "pass":
                blockers.append(DigestBlocker.TESTING_UNHEALTHY)
        except FileNotFoundError:
            blockers.append(DigestBlocker.TESTING_UNAVAILABLE)
    if review.unavailable_reason is not None:
        blockers.append(DigestBlocker.REVIEW_UNAVAILABLE)
    elif not review.approved:
        blockers.append(DigestBlocker.ACCEPTANCE_PENDING)
    if profile.release_on_acceptance == "held":
        blockers.append(DigestBlocker.RELEASE_HELD)
    action = (
        DigestAction.PROMOTION
        if profile.release_on_acceptance == "director_standing"
        else DigestAction.INVITATION
    )
    product_path = f"{public_url.rstrip('/')}/ui/products/{quote(profile.product, safe='')}"
    action_line = (
        f"Review the promotion step: {product_path}/environments/prod/actions"
        if action == DigestAction.PROMOTION
        else f"Send the Client a release invitation: {public_url.rstrip('/')}/ui/owner-review?product={quote(profile.product, safe='')}"
    )
    compare_url = (
        f"https://github.com/{repository}/compare/{production.source_commit}...{head.commit}"
    )
    remaining = f"\n  - {total - 5} more commits in the full list" if total > 5 else ""
    descriptions = [_BLOCKER_TEXT[blocker] for blocker in blockers]
    descriptions.extend(_plain(blocker, 160) for blocker in review.blockers[:3])
    content = (
        f"- {_plain(profile.display_name, 100)}: {total} source commits"
        + (f"; waiting at least {age} hours" if age is not None else "; waiting age unavailable")
        + "\n"
        + "\n".join(lines)
        + remaining
        + (f"\n  Full list: {compare_url}" if total else "")
        + "\n  Release status: "
        + ("; ".join(descriptions) or "Ready for the supported release path")
        + f"\n  {action_line}"
    )
    return WaitingReleaseList(content, production, head.commit, age, tuple(blockers), action)


def _plain(value: str, limit: int) -> str:
    # Keep source titles from injecting extra entries or Markdown links/mentions.
    return " ".join(
        value.replace("@", "＠").translate(str.maketrans("[]()<>`*_", "         ")).split()
    )[:limit]


class ReleaseDigestScheduler(_PreviewScheduler):
    scope = "launchplane:release-digest:v1"
    route = "service:release-digest"
    fingerprint = "release-digest-v1"
    offset = 0

    @classmethod
    def slot_at(cls, now: float) -> int:
        return int((now - cls.offset) // cls.interval)

    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        control_plane_root: Path,
        public_url: str,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(store, control_plane_root=control_plane_root, clock=clock or time.time)
        self.public_url = public_url

    def _run_sweep(self) -> dict[str, object]:
        parsed = urlsplit(self.public_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            return {"status": "unavailable", "apply": False}
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        slot = self.slot_at(now.timestamp())
        failed = False
        for profile in self.store.list_product_profile_records():
            if self._stop.is_set():
                from control_plane.preview_lifecycle_cleanup_routes import (
                    PreviewLifecycleSweepCancelled,
                )

                raise PreviewLifecycleSweepCancelled
            if not profile.is_active:
                continue
            lane = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
            if lane is None:
                continue
            policies = tuple(
                policy
                for policy in self.store.list_public_ingress_notification_policy_records(
                    product=profile.product,
                    context_name=lane.context,
                    instance_name="prod",
                    status="enabled",
                )
                if policy.release_digest_enabled and not policy.check_name and not policy.check_kind
            )
            if not policies:
                continue
            try:
                transport = resolve_build_provenance_transport(self.store, profile)
                waiting = waiting_release_list(
                    self.store,
                    profile,
                    read=transport.get_json,
                    now=now,
                    public_url=self.public_url,
                    control_plane_root=self.control_plane_root,
                )
                if waiting is None:
                    continue
                # Discord's content limit is 2000; split only at complete lines.
                chunks = ["Changes waiting to release\n"]
                for line in waiting.content.splitlines():
                    if len(chunks[-1]) + len(line) + 1 > 1900:
                        chunks.append("Changes waiting to release (continued)\n")
                    chunks[-1] += line + "\n"
                for policy in policies:
                    for destination in policy.destinations:
                        if destination.kind != "discord":
                            continue
                        for index, chunk in enumerate(chunks):
                            key = f"release-digest:{slot}:{profile.product}:{policy.policy_id}:{destination.destination_id}:{index}"
                            self.store.enqueue_outbox_delivery_with_idempotency(
                                OutboxWithIdempotencyRequest(
                                    delivery=OutboxDeliveryRecord(
                                        delivery_id=build_outbox_delivery_id(
                                            kind="release_digest", dedupe_key=key
                                        ),
                                        kind="release_digest",
                                        aggregate_type="release_digest",
                                        aggregate_id=profile.product,
                                        dedupe_key=key,
                                        created_at=_timestamp(self.clock()),
                                        updated_at=_timestamp(self.clock()),
                                        next_attempt_at=_timestamp(self.clock()),
                                        payload={
                                            "policy_id": policy.policy_id,
                                            "destination_id": destination.destination_id,
                                            "context": lane.context,
                                            "repository": profile.repository,
                                            "production": waiting.production.model_dump(
                                                mode="json"
                                            ),
                                            "head_commit": waiting.head_commit,
                                            "content": chunk,
                                            "slot": slot,
                                        },
                                    )
                                )
                            )
            except Exception:
                failed = True
                _LOGGER.exception("Release digest evidence unavailable for %s", profile.product)
        return {"status": "partial" if failed else "pass", "apply": False}


def deliver_release_digest(
    *,
    store: PostgresRecordStore,
    record: OutboxDeliveryRecord,
    drivers: PublicIngressNotificationDriverSet,
    mark_provider_started: Callable[[str, str], None],
    clock: Callable[[], float] = time.time,
) -> OutboxDeliveryRecord:
    started = bool(record.provider_operation_key)

    def result(state: str, *, action: str = "", error: str = "") -> OutboxDeliveryRecord:
        return record.model_copy(
            update={
                "state": state,
                "action": action,
                "error_code": error,
                "provider_operation_key": record.provider_operation_key
                or (record.delivery_id if started else ""),
                "provider_id": "discord" if started else "",
                "lease_owner": "",
                "lease_expires_at": "",
            }
        )

    if record.provider_operation_key:
        return result("failed", error="delivery_outcome_unknown")
    try:
        profile = store.read_product_profile_record(record.aggregate_id)
        context = str(record.payload["context"])
        if not profile.is_active or not any(
            lane.context == context and lane.instance == "prod" for lane in profile.lanes
        ):
            return result("delivered", action="skipped_retired_product")
        if profile.repository != record.payload.get("repository") or release_version(
            store=store, profile=profile, instance="prod"
        ) != ReleaseVersion.model_validate(record.payload.get("production")):
            return result("delivered", action="skipped_changed_production")
        policies = store.list_public_ingress_notification_policy_records(
            product=profile.product, context_name=context, instance_name="prod", status="enabled"
        )
        destination = next(
            (
                destination
                for policy in policies
                if policy.policy_id == record.payload["policy_id"]
                and policy.release_digest_enabled
                and not policy.check_name
                and not policy.check_kind
                for destination in policy.destinations
                if destination.kind == "discord"
                and destination.destination_id == record.payload["destination_id"]
            ),
            None,
        )
        if destination is None:
            return result("delivered", action="skipped_disabled_digest")
        if int(str(record.payload["slot"])) != ReleaseDigestScheduler.slot_at(clock()):
            return result("delivered", action="skipped_expired_digest")
        sender = drivers.secret_resolver(
            destination.discord_webhook_secret
        ) or managed_notification_secret_value(
            store, destination.discord_webhook_secret, context, "prod"
        )
        if not sender:
            return result("pending", error="digest_destination_unavailable")
        if public_discord_url_error(sender):
            return result("failed", error="digest_destination_invalid")
        mark_provider_started(record.delivery_id, "discord")
        started = True
        try:
            drivers.discord_sender(
                sender,
                {"content": str(record.payload["content"]), "allowed_mentions": {"parse": []}},
            )
        except DiscordNotificationError as error:
            if error.status_code == 429:
                started = False  # The provider explicitly refused this send.
                return result("pending", error="discord_rate_limited")
            if 400 <= error.status_code < 500:
                started = False
                return result("failed", error="discord_send_rejected")
            return result("failed", error="delivery_outcome_unknown")
        except Exception:
            return result("failed", error="delivery_outcome_unknown")
        return result("delivered", action="sent_release_digest")
    except Exception:
        return result("pending", error="digest_evidence_unavailable")
