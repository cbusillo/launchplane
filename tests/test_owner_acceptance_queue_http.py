from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.owner_acceptance import OwnerAcceptanceEventRecord
from control_plane.http_routes.owner_acceptance import (
    OWNER_ACCEPTANCE_QUEUE_ROUTE,
    OwnerAcceptanceRouteDependencies,
    register_owner_acceptance_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity
from tests.support.http import lifespan_client
from tests.test_owner_acceptance import (
    PRODUCT,
    REPOSITORY,
    SECOND_PRODUCT,
    _EvidenceProvider,
    _human,
    _repository_evidence,
    _store,
)


def _http_error(**kwargs: object) -> HTTPException:
    return HTTPException(status_code=int(str(kwargs["status_code"])), detail=kwargs)


class _QueueStore:
    """Wraps FilesystemRecordStore and adds list_engineering_review_decision_records."""

    def __init__(self, base_store: object) -> None:
        self._base = base_store
        self._erds: list[EngineeringReviewDecisionRecord] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)

    def add_erd(self, record: EngineeringReviewDecisionRecord) -> None:
        self._erds.append(record)

    def list_engineering_review_decision_records(
        self,
        *,
        repository: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        work_request_id: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewDecisionRecord, ...]:
        results = [
            r
            for r in self._erds
            if (not repository or r.target.repository == repository)
            and (pull_request_number is None or r.target.pull_request_number == pull_request_number)
            and (not head_sha or r.target.head_sha == head_sha)
            and (not work_request_id or r.work_request_id == work_request_id)
        ]
        results.sort(key=lambda r: (r.evaluated_at, r.decision_id), reverse=True)
        if limit is not None:
            results = results[:limit]
        return tuple(results)


def _app(
    *,
    store: object,
    identity: LaunchplaneIdentity | None = None,
    repository_evidence_provider: _EvidenceProvider | None = None,
) -> FastAPI:
    resolved_identity = identity or _human()
    common = ReadRouteDependencies(
        read_identity=lambda: resolved_identity,
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-queue",
        authorization_allows=lambda **_: True,
        http_error=_http_error,
        error_response_model=dict,  # type: ignore[arg-type]
    )
    app = FastAPI()
    register_owner_acceptance_routes(
        cast(ApiRouteRegistrar, app),
        dependencies=OwnerAcceptanceRouteDependencies(
            common=common,
            read_browser_mutation_identity=lambda: resolved_identity,
            repository_evidence_provider=(
                repository_evidence_provider or _EvidenceProvider(_repository_evidence())
            ),
        ),
    )
    return app


class OwnerAcceptanceQueueHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_requires_read_authorization(self) -> None:
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            common = ReadRouteDependencies(
                read_identity=lambda: _human(),
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-queue-denied",
                authorization_allows=lambda **_: False,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_owner_acceptance_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=OwnerAcceptanceRouteDependencies(
                    common=common,
                    read_browser_mutation_identity=lambda: _human(),
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                ),
            )
            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 403, response.text)

    async def test_queue_empty_when_no_erds_or_events(self) -> None:
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            app = _app(store=store)
            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["mode"], "shadow")
            self.assertIs(payload["authoritative"], False)
            self.assertEqual(payload["enforcement_effect"], "none")
            self.assertEqual(payload["entry_count"], 0)
            self.assertEqual(payload["entries"], [])

    async def test_queue_candidates_derived_server_side_from_erds(self) -> None:
        """Browser supplies no target; ERD records are the server-owned source."""
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            _add_minimal_erd(store, repository=REPOSITORY, pull_request_number=42)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)
            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            entries = payload["entries"]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["repository"], REPOSITORY)
            self.assertEqual(entry["pull_request_number"], 42)
            self.assertEqual(entry["mode"], "shadow")
            self.assertIs(entry["authoritative"], False)
            self.assertEqual(entry["enforcement_effect"], "none")
            self.assertIn("next_action", entry)
            self.assertIn("owner_acceptance_decision", entry)
            self.assertIn("engineering_review_decision", entry)
            self.assertIsNotNone(entry["engineering_review_decision"])

    async def test_queue_candidates_derived_server_side_from_event_history(self) -> None:
        """PRs with owner acceptance events but no ERD still appear in the queue."""
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            from control_plane.http_routes.owner_acceptance import (
                OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                OWNER_ACCEPTANCE_EVENTS_ROUTE,
            )
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]

                written = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "queue-event-test"},
                )
                self.assertEqual(written.status_code, 202, written.text)

                queue_response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(queue_response.status_code, 200, queue_response.text)
            entries = queue_response.json()["entries"]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["repository"], REPOSITORY)
            self.assertEqual(entry["pull_request_number"], 2022)
            self.assertIsNone(entry["engineering_review_decision"])
            self.assertEqual(entry["owner_acceptance_decision"]["status"], "accepted")

    async def test_pending_rows_appear_before_any_owner_event(self) -> None:
        """A PR is queued as pending even before any Owner event exists."""
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            _add_minimal_erd(store, repository=REPOSITORY, pull_request_number=99)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)
            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 200, response.text)
            entries = response.json()["entries"]
            self.assertGreater(len(entries), 0)
            pending = [e for e in entries if e["owner_acceptance_decision"]["status"] == "pending"]
            self.assertEqual(len(pending), 1, "ERD-sourced PR should appear as pending")
            self.assertEqual(pending[0]["pull_request_number"], 99)

    async def test_queue_browser_cannot_inject_targets(self) -> None:
        """Browser filters are optional; they do not introduce new queue candidates."""
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            app = _app(store=store)
            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_QUEUE_ROUTE,
                    params={"repository": REPOSITORY},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["entry_count"], 0)

    async def test_queue_status_filter_reduces_results(self) -> None:
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            _add_minimal_erd(store, repository=REPOSITORY, pull_request_number=77)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)
            async with lifespan_client(app) as client:
                all_entries = (await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)).json()["entries"]
                pending_entries = (
                    await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE, params={"status": "pending"})
                ).json()["entries"]
                accepted_entries = (
                    await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE, params={"status": "accepted"})
                ).json()["entries"]
            self.assertGreater(len(all_entries), 0)
            self.assertLessEqual(len(pending_entries), len(all_entries))
            self.assertLessEqual(len(accepted_entries), len(all_entries))
            self.assertEqual(
                len(all_entries),
                len(pending_entries) + len(accepted_entries),
            )

    async def test_queue_union_of_erds_and_events(self) -> None:
        """Candidates from ERDs and from event history are unioned, not duplicated.

        The _EvidenceProvider always maps any target to PR 2022, so the event
        written in this test will have binding.pull_request_number == 2022.
        PR 100 is added via ERD only. The union must include both.
        """
        with TemporaryDirectory() as directory:
            base_store = _store(Path(directory))
            store = _QueueStore(base_store)
            _add_minimal_erd(store, repository=REPOSITORY, pull_request_number=100)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            from control_plane.http_routes.owner_acceptance import (
                OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                OWNER_ACCEPTANCE_EVENTS_ROUTE,
            )
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "union-test"},
                )

                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 200, response.text)
            entries = response.json()["entries"]
            pr_numbers = {e["pull_request_number"] for e in entries}
            self.assertIn(100, pr_numbers, "ERD-only PR must appear")
            self.assertIn(2022, pr_numbers, "event-history PR must appear")
            self.assertEqual(len(entries), len(pr_numbers), "no duplicates")


def _add_minimal_erd(
    store: _QueueStore,
    *,
    repository: str,
    pull_request_number: int,
) -> None:
    from control_plane.contracts.change_impact import ChangeImpactTarget
    from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord

    store.add_erd(
        EngineeringReviewDecisionRecord(
            mode="shadow",
            authoritative=False,
            enforcement_effect="none",
            status="approved",
            reason_code="approved_by_model",
            target=ChangeImpactTarget(
                repository_id="1001",
                repository_owner_id="2001",
                repository=repository,
                pull_request_number=pull_request_number,
                head_sha="a" * 40,
                tree_sha="b" * 40,
            ),
            work_request_id="work-req-001",
            work_request_lifecycle_id="lifecycle-001",
            change_impact_status="success",
            change_impact_policy_record_id="policy-001",
            change_impact_policy_revision=1,
            change_impact_policy_digest="d" * 64,
            engineering_review_tier="routine",
            required_review_count=1,
            qualifying_run_ids=("run-001",),
            qualifying_model_families=("claude",),
            evaluated_at="2026-08-07T12:00:00Z",
        )
    )


if __name__ == "__main__":
    unittest.main()
