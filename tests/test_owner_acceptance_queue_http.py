from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.contracts.owner_acceptance import OwnerAcceptanceEventRecord
from control_plane.http_routes.owner_acceptance import (
    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
    OWNER_ACCEPTANCE_EVENTS_ROUTE,
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
    REPOSITORY_ID,
    SECOND_PRODUCT,
    _EvidenceProvider,
    _human,
    _repository_evidence,
    _store,
)


def _http_error(**kwargs: object) -> HTTPException:
    return HTTPException(status_code=int(str(kwargs["status_code"])), detail=kwargs)


def _app(
    *,
    store: object,
    identity: LaunchplaneIdentity | None = None,
    repository_evidence_provider: _EvidenceProvider | None = None,
    authorization_allows: bool = True,
) -> FastAPI:
    resolved_identity = identity or _human()
    common = ReadRouteDependencies(
        read_identity=lambda: resolved_identity,
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-queue",
        authorization_allows=lambda **_: authorization_allows,
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
            store = _store(Path(directory))
            app = _app(store=store, authorization_allows=False)
            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 403, response.text)

    async def test_queue_empty_when_no_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["mode"], "shadow")
            self.assertIs(payload["authoritative"], False)
            self.assertEqual(payload["enforcement_effect"], "none")
            self.assertEqual(payload["total"], 0)
            self.assertEqual(payload["candidate"], 0)
            self.assertIs(payload["truncated"], False)
            self.assertIs(payload["has_more"], False)
            self.assertEqual(payload["entry_count"], 0)
            self.assertEqual(payload["entries"], [])

    async def _write_event(
        self,
        client: object,
        *,
        pull_request_number: int = 2022,
        action: str = "accepted",
        idempotency_key: str = "queue-test",
    ) -> str:
        from httpx2 import AsyncClient

        assert isinstance(client, AsyncClient)
        evaluated = await client.get(
            OWNER_ACCEPTANCE_EVALUATION_ROUTE,
            params={"repository": REPOSITORY, "pull_request_number": pull_request_number},
        )
        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
        body: dict[str, object] = {
            "target": {"repository": REPOSITORY, "pull_request_number": pull_request_number},
            "action": action,
            "expected_binding_sha256": binding_sha256,
        }
        if action != "accepted":
            body["reason"] = "Test reason for non-accepted action"
        written = await client.post(
            OWNER_ACCEPTANCE_EVENTS_ROUTE,
            json=body,
            headers={"Idempotency-Key": idempotency_key},
        )
        self.assertEqual(written.status_code, 202, written.text)
        return str(binding_sha256)

    async def test_queue_derives_entries_from_event_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022, action="accepted")
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            entries = payload["entries"]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["repository"], REPOSITORY)
            self.assertEqual(entry["pull_request_number"], 2022)
            self.assertEqual(entry["repository_id"], REPOSITORY_ID)
            self.assertEqual(entry["product"], PRODUCT)
            self.assertEqual(entry["mode"], "shadow")
            self.assertIs(entry["authoritative"], False)
            self.assertEqual(entry["enforcement_effect"], "none")
            self.assertIs(entry["verification_required"], True)
            self.assertEqual(entry["ledger_status"], "accepted")
            self.assertIn("next_action", entry)
            self.assertIn("latest_event", entry)
            self.assertIn("latest_binding", entry)
            self.assertIn("occurred_at", entry)
            self.assertNotIn("owner_acceptance_decision", entry)
            self.assertNotIn("engineering_review_decision", entry)

    async def test_queue_folds_by_full_subject(self) -> None:
        """Multiple events for the same subject appear as one queue entry."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022, action="accepted", idempotency_key="key-1")
                await self._write_event(client, pull_request_number=2022, action="revoked", idempotency_key="key-2")
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["total"], 1, "Both events share a subject; only one entry")
            entries = payload["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["ledger_status"], "revoked", "Latest event wins")

    async def test_queue_browser_cannot_inject_targets(self) -> None:
        """Browser filters are optional; they do not introduce new queue candidates."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_QUEUE_ROUTE,
                    params={"repository": REPOSITORY},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["entry_count"], 0)

    async def test_queue_status_filter_before_pagination(self) -> None:
        """Status filter is applied before the pagination limit."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022, action="accepted")
                all_payload = (await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)).json()
                accepted_payload = (
                    await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE, params={"status": "accepted"})
                ).json()
                revoked_payload = (
                    await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE, params={"status": "revoked"})
                ).json()

            self.assertEqual(all_payload["total"], 1)
            self.assertEqual(all_payload["candidate"], 1)
            self.assertEqual(accepted_payload["candidate"], 1)
            self.assertEqual(revoked_payload["candidate"], 0)
            self.assertEqual(len(revoked_payload["entries"]), 0)

    async def test_queue_repository_substring_filter(self) -> None:
        """Repository filter uses substring matching."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022)
                # Substring match should find it
                match_payload = (
                    await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE, params={"repository": "example"})
                ).json()
                # Non-matching substring should return empty
                nomatch_payload = (
                    await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE, params={"repository": "unrelated"})
                ).json()

            self.assertEqual(match_payload["candidate"], 1)
            self.assertEqual(nomatch_payload["candidate"], 0)

    async def test_queue_response_includes_full_provenance(self) -> None:
        """Each entry includes full persisted binding and event provenance."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022)
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            entry = response.json()["entries"][0]
            self.assertIn("event_id", entry["latest_event"])
            self.assertIn("binding", entry["latest_event"])
            self.assertIn("occurred_at", entry["latest_event"])
            self.assertIn("binding_sha256", entry["latest_binding"])
            self.assertIn("repository_id", entry["latest_binding"])
            self.assertIn("head_sha", entry["latest_binding"])

    async def test_queue_verification_required_always_true(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022)
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            self.assertIs(response.json()["entries"][0]["verification_required"], True)

    async def test_queue_ledger_status_from_accepted_action(self) -> None:
        """Event action 'accepted' maps to ledger_status 'accepted'."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                await self._write_event(client, pull_request_number=2022, action="accepted")
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            entry = response.json()["entries"][0]
            self.assertEqual(entry["ledger_status"], "accepted")

    async def test_queue_total_counts_unique_subjects(self) -> None:
        """total counts unique subjects in the ledger, not raw events."""
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                # Two writes with different idempotency keys produce two events for the same subject
                # because the evidence provider maps all targets to the same binding (PR 2022)
                await self._write_event(client, pull_request_number=2022, action="accepted", idempotency_key="key-1")
                await self._write_event(client, pull_request_number=2022, action="revoked", idempotency_key="key-2")
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            payload = response.json()
            self.assertEqual(payload["total"], 1, "Two events for same subject → one unique subject")
            self.assertEqual(payload["entry_count"], 1)

    async def test_queue_no_erds_no_github_calls(self) -> None:
        """Queue is ledger-only: no ERD dependency, no repository evidence provider calls."""
        from unittest.mock import MagicMock
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            mock_provider = MagicMock(spec=_EvidenceProvider)
            app = _app(store=store, repository_evidence_provider=mock_provider)

            async with lifespan_client(app) as client:
                response = await client.get(OWNER_ACCEPTANCE_QUEUE_ROUTE)

            self.assertEqual(response.status_code, 200, response.text)
            # Provider should never be called for the queue endpoint (no resolve calls)
            mock_provider.resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
