"""Provider capability boundary for a measured, persisted writer fence/drain."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from control_plane.contracts.prepared_public_site import (
    PreparedPublicSite,
    PublicPauseRecord,
    PublicRecoveryObservation,
    PublicSiteBinding,
    WriterObservation,
)
from control_plane.prepared_public_site import verify_snapshot


class PublicPauseStore(Protocol):
    def save(self, record: PublicPauseRecord) -> None: ...
    def load(self, pause_id: str) -> PublicPauseRecord: ...


class PublicPauseProvider(Protocol):
    def publish(self, site: PreparedPublicSite, pause_id: str) -> str:
        """Publish prepared serving and block public writes; read back its content digest."""
        ...

    def observed_public_copy(self, pause_id: str) -> str:
        """Read current serving authority without guessing a failed publication succeeded."""
        ...

    def fence_and_drain(
        self, site: PreparedPublicSite, pause_id: str
    ) -> tuple[WriterObservation, ...]:
        """Observe each configured writer stopped/fenced, including asset/filestore GC."""
        ...

    def recover_and_resume(
        self, site: PreparedPublicSite, pause_id: str, expected: PublicSiteBinding
    ) -> PublicRecoveryObservation:
        """Read back recovered authoritative serving and exactly one owner per writer target."""
        ...


class PreparedPublicPause:
    def __init__(
        self,
        site: PreparedPublicSite,
        store: PublicPauseStore,
        provider: PublicPauseProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        verify_snapshot(site)
        self.site = site
        self.store = store
        self.provider = provider
        self.clock = clock

    def begin(self) -> PublicPauseRecord:
        record = PublicPauseRecord(
            pause_id=uuid4().hex,
            binding=self.site.plan.binding,
            content_digest=self.site.content_digest,
            began_at=self.clock(),
        )
        # Record the start before any form block, fence, drain or provider mutation.
        self.store.save(record)
        if self.provider.publish(self.site, record.pause_id) != self.site.content_digest:
            raise ValueError("prepared serving publication was not read back")
        return self.drain(record.pause_id)

    def _record(self, pause_id: str) -> PublicPauseRecord:
        record = self.store.load(pause_id)
        if (
            record.binding != self.site.plan.binding
            or record.content_digest != self.site.content_digest
        ):
            raise ValueError("pause does not belong to this prepared release")
        return record

    def drain(self, pause_id: str) -> PublicPauseRecord:
        record = self._record(pause_id)
        if record.state != "fencing":
            raise ValueError("pause is not awaiting writer drain")
        if self.provider.observed_public_copy(pause_id) != record.content_digest:
            raise ValueError("prepared serving must be observed before writer drain")
        observations = self.provider.fence_and_drain(self.site, pause_id)
        if (
            len(observations) != len(self.site.plan.writers)
            or {item.writer for item in observations} != set(self.site.plan.writers)
            or any(
                item.binding != record.binding
                or item.pause_id != pause_id
                or not item.fenced
                or item.active_jobs != 0
                for item in observations
            )
        ):
            raise ValueError("every authoritative writer must be verifiably fenced and drained")
        record = record.model_copy(update={"state": "drained", "writers": observations})
        self.store.save(record)
        return record

    def finish(self, pause_id: str, *, expected: PublicSiteBinding) -> PublicPauseRecord:
        record = self._record(pause_id)
        for field in ("product_id", "environment_id", "release_id", "database"):
            if getattr(expected, field) != getattr(record.binding, field):
                raise ValueError("recovery binding crosses the release/environment boundary")
        if record.recovery_binding is not None and record.recovery_binding != expected:
            raise ValueError("cannot change an uncertain recovery target")
        if record.state == "complete":
            return record
        if record.state not in {"drained", "resuming"}:
            raise ValueError("cannot recover before a complete writer drain")
        record = record.model_copy(update={"state": "resuming", "recovery_binding": expected})
        self.store.save(record)
        observation = self.provider.recover_and_resume(self.site, pause_id, expected)
        if (
            observation.binding != expected
            or observation.pause_id != pause_id
            or len(observation.writer_owners) != len(self.site.plan.writers)
            or dict(observation.writer_owners)
            != {writer.writer_id: 1 for writer in self.site.plan.writers}
        ):
            raise ValueError("authoritative serving/recovery binding was not read back")
        ended = self.clock()
        if ended < record.began_at:
            raise ValueError("pause clock moved backwards")
        record = record.model_copy(
            update={
                "state": "complete",
                "ended_at": ended,
                "duration_seconds": (ended - record.began_at).total_seconds(),
            }
        )
        self.store.save(record)
        return record
