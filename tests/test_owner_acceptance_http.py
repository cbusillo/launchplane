from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.http_app import _BOUNDED_REQUEST_BODY_CONTRACTS
from control_plane.http_routes.owner_acceptance import (
    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
    OWNER_ACCEPTANCE_EVENT_ROUTE,
    OWNER_ACCEPTANCE_EVENTS_ROUTE,
    OwnerAcceptanceRouteDependencies,
    register_owner_acceptance_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from tests.support.http import lifespan_client
from tests.test_owner_acceptance import (
    REPOSITORY,
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
    browser_identity: LaunchplaneIdentity | None = None,
) -> FastAPI:
    resolved_identity = identity or _human()
    resolved_browser_identity = browser_identity or resolved_identity
    common = ReadRouteDependencies(
        read_identity=lambda: resolved_identity,
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-owner-acceptance",
        authorization_allows=lambda **_: True,
        http_error=_http_error,
        error_response_model=dict,  # type: ignore[arg-type]
    )
    app = FastAPI()
    register_owner_acceptance_routes(
        cast(ApiRouteRegistrar, app),
        dependencies=OwnerAcceptanceRouteDependencies(
            common=common,
            read_browser_mutation_identity=lambda: resolved_browser_identity,
            repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
        ),
    )
    return app


class OwnerAcceptanceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluate_and_human_event_use_server_derived_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }

            async with lifespan_client(app) as client:
                injected = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "head_sha": "c" * 40,
                    },
                    headers={"Idempotency-Key": "accept-1"},
                )
                self.assertEqual(injected.status_code, 422, injected.text)
                self.assertEqual(store.list_owner_acceptance_event_records(), ())

                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params=target,
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                self.assertEqual(evaluated.json()["decision"]["status"], "pending")

                written = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                    },
                    headers={"Idempotency-Key": "accept-1"},
                )
                self.assertEqual(written.status_code, 202, written.text)
                payload = written.json()
                self.assertEqual(payload["write_status"], "written")
                self.assertEqual(payload["decision"]["status"], "accepted")
                binding = payload["record"]["binding"]
                self.assertEqual(binding["repository"], REPOSITORY)
                self.assertEqual(binding["head_sha"], "a" * 40)
                self.assertIn("owner_policy_digest", binding)
                self.assertIn("owner_requirement_digest", binding)

                replayed = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={"target": target, "action": "accepted"},
                    headers={"Idempotency-Key": "accept-1"},
                )
                self.assertEqual(replayed.status_code, 202, replayed.text)
                self.assertEqual(replayed.json()["write_status"], "replayed")
                self.assertEqual(replayed.json()["record"], payload["record"])

                read = await client.get(
                    OWNER_ACCEPTANCE_EVENT_ROUTE.format(event_id=payload["record"]["event_id"])
                )
                self.assertEqual(read.status_code, 200, read.text)
                self.assertEqual(read.json()["record"], payload["record"])

    async def test_changes_requested_and_revoked_require_reasons_and_evaluate(self) -> None:
        target: dict[str, str | int] = {
            "repository": REPOSITORY,
            "pull_request_number": 2022,
        }
        for action, expected_status in (
            ("changes_requested", "changes_requested"),
            ("revoked", "revoked"),
        ):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                store = _store(Path(directory))
                app = _app(store=store)
                async with lifespan_client(app) as client:
                    missing_reason = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={"target": target, "action": action},
                        headers={"Idempotency-Key": f"{action}-missing-reason"},
                    )
                    self.assertEqual(missing_reason.status_code, 422, missing_reason.text)
                    self.assertEqual(store.list_owner_acceptance_event_records(), ())

                    written = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": target,
                            "action": action,
                            "reason": "Owner provided actionable feedback.",
                        },
                        headers={"Idempotency-Key": action},
                    )
                    self.assertEqual(written.status_code, 202, written.text)
                    self.assertEqual(written.json()["decision"]["status"], expected_status)

                    evaluated = await client.get(
                        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                        params=target,
                    )
                    self.assertEqual(evaluated.status_code, 200, evaluated.text)
                    self.assertEqual(evaluated.json()["decision"]["status"], expected_status)

    async def test_event_route_rejects_non_human_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(
                store=store,
                browser_identity=TerminalAgentIdentity(subject="agent", token_label="local"),
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                    },
                    headers={"Idempotency-Key": "accept-agent"},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(store.list_owner_acceptance_event_records(), ())

    async def test_event_route_rejects_non_owner_human(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(
                store=store,
                browser_identity=GitHubHumanIdentity(
                    login="other",
                    github_id=9999,
                    name="Other",
                    email="",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="admin",
                ),
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                    },
                    headers={"Idempotency-Key": "accept-other"},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(store.list_owner_acceptance_event_records(), ())

    async def test_event_route_requires_idempotency_key(self) -> None:
        with TemporaryDirectory() as directory:
            app = _app(store=_store(Path(directory)))
            async with lifespan_client(app) as client:
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                    },
                )

            self.assertEqual(response.status_code, 422, response.text)

    def test_routes_are_bounded(self) -> None:
        self.assertNotIn(OWNER_ACCEPTANCE_EVALUATION_ROUTE, _BOUNDED_REQUEST_BODY_CONTRACTS)
        self.assertEqual(
            _BOUNDED_REQUEST_BODY_CONTRACTS[OWNER_ACCEPTANCE_EVENTS_ROUTE][1],
            16 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
