"""Calm daily release lists, independent of incidents and release authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord, build_outbox_delivery_id
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.preview_lifecycle_scheduler import _PreviewScheduler, _timestamp
from control_plane.product_reconcile import resolve_build_provenance_transport
from control_plane.release_review import current_release_review, release_version
from control_plane.source_control_change import read_branch_revision
from control_plane.storage.postgres import PostgresRecordStore, OutboxWithIdempotencyRequest
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    managed_notification_secret_value,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitingReleaseList:
    content: str
    production_commit: str
    head_commit: str


def waiting_release_list(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    *,
    read: Callable[[str], object],
    now: datetime,
    public_url: str,
    control_plane_root: Path,
) -> WaitingReleaseList | None:
    """Read actual source ancestry; an older timestamp alone proves no pending change."""
    production = release_version(store=store, profile=profile, instance="prod")
    head = read_branch_revision(
        repository=profile.repository, branch=profile.default_branch, read=read
    )
    if head is None:
        raise ValueError("Release source unavailable")
    if head.commit == production.source_commit:
        return None
    repository = "/".join(quote(part, safe="") for part in profile.repository.split("/"))
    comparison = read(f"/repos/{repository}/compare/{production.source_commit}...{head.commit}")
    if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
        raise ValueError("Release ancestry unavailable")
    commits = comparison.get("commits")
    total = comparison.get("total_commits")
    if not isinstance(commits, list) or not commits or not isinstance(total, int) or total < 1:
        raise ValueError("Release changes unavailable")
    # The provider's unpaginated comparison contains at most 250 earliest commits.
    # Link the complete immutable comparison, and label the displayed subset.
    lines: list[str] = []
    dates: list[datetime] = []
    for commit in commits:
        if not isinstance(commit, dict) or not isinstance(commit.get("commit"), dict):
            raise ValueError("Release change metadata unavailable")
        metadata = commit["commit"]
        committer = metadata.get("committer")
        if not isinstance(committer, dict) or not isinstance(committer.get("date"), str):
            raise ValueError("Release change time unavailable")
        dates.append(datetime.fromisoformat(committer["date"].replace("Z", "+00:00")))
        title = str(metadata.get("message", "")).splitlines()[0:1]
        if len(lines) < 5:
            lines.append("  - " + _plain(title[0] if title else "Change", 120))
    age = max(0, int((now - min(dates)).total_seconds() // 3600))
    review = current_release_review(
        control_plane_root=control_plane_root, record_store=store, profile=profile
    )
    blockers: list[str] = []
    testing = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
    try:
        if testing is None:
            raise FileNotFoundError
        inventory = store.read_environment_inventory(
            context_name=testing.context, instance_name="testing"
        )
        if inventory.source_git_ref != head.commit:
            blockers.append("Latest changes are not in testing")
        if inventory.destination_health.status != "pass" or inventory.deploy.status != "pass":
            blockers.append("Testing is not green")
    except FileNotFoundError:
        blockers.append("Testing evidence unavailable")
    if review.unavailable_reason is not None:
        blockers.append("Release review unavailable")
    elif not review.approved:
        blockers.extend(_plain(blocker, 160) for blocker in review.blockers[:3])
        if not review.blockers:
            blockers.append("Client acceptance pending")
    if profile.release_on_acceptance == "held":
        blockers.append("Releases are on hold")
    own = profile.release_on_acceptance == "director_standing"
    product_path = f"{public_url.rstrip('/')}/ui/products/{quote(profile.product, safe='')}"
    action = (
        f"Review the promotion step: {product_path}/environments/prod/actions"
        if own
        else f"Send the Client a release invitation: {public_url.rstrip('/')}/ui/owner-review?product={quote(profile.product, safe='')}"
    )
    compare_url = (
        f"https://github.com/{repository}/compare/{production.source_commit}...{head.commit}"
    )
    remaining = f"\n  - {total - len(lines)} more in the full list" if total > len(lines) else ""
    content = (
        f"- {_plain(profile.display_name, 100)}: {total} changes; oldest source commit {age} hours old\n"
        + "\n".join(lines)
        + remaining
        + f"\n  Full list: {compare_url}\n"
        + "  Release status: "
        + ("; ".join(blockers) or "Ready for the supported release path")
        + f"\n  {action}"
    )
    return WaitingReleaseList(content, production.source_commit, head.commit)


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
                            key = f"release-digest:{self.slot()}:{profile.product}:{policy.policy_id}:{destination.destination_id}:{index}"
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
                                            "production_commit": waiting.production_commit,
                                            "head_commit": waiting.head_commit,
                                            "content": chunk,
                                            "slot": self.slot(),
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
        ).source_commit != record.payload.get("production_commit"):
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
        if int(str(record.payload["slot"])) != int(
            datetime.now(timezone.utc).timestamp() // (24 * 60 * 60)
        ):
            return result("delivered", action="skipped_expired_digest")
        sender = drivers.secret_resolver(
            destination.discord_webhook_secret
        ) or managed_notification_secret_value(
            store, destination.discord_webhook_secret, context, "prod"
        )
        if not sender:
            return result("failed", error="digest_destination_unavailable")
        mark_provider_started(record.delivery_id, "discord")
        started = True
        try:
            drivers.discord_sender(
                sender,
                {"content": str(record.payload["content"]), "allowed_mentions": {"parse": []}},
            )
        except Exception:
            return result("failed", error="delivery_outcome_unknown")
        return result("delivered", action="sent_release_digest")
    except Exception:
        return result("failed", error="digest_evidence_unavailable")
