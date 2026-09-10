from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any, Callable, cast
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException

from control_plane.http_app import _BOUNDED_REQUEST_BODY_CONTRACTS
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
    OwnerAcceptanceDecision,
    OwnerAcceptanceTransitionError,
)
from control_plane.contracts.change_impact import (
    ChangeImpactAuthorshipEvidence,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.product_owner import ProductOwnerGrant, ProductOwnerIdentity
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.http_routes.owner_acceptance import (
    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
    OWNER_ACCEPTANCE_EVENT_ROUTE,
    OWNER_ACCEPTANCE_EVENTS_ROUTE,
    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
    OWNER_ACCEPTANCE_PROJECT_ROUTE,
    OwnerAcceptanceRouteDependencies,
    _owner_review_status,
    register_owner_acceptance_routes,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.owner_acceptance_projection import OwnerAcceptanceProjectionService
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client
from tests.test_owner_acceptance import (
    PRODUCT,
    REPOSITORY,
    REPOSITORY_OWNER_ID,
    SECOND_PRODUCT,
    _EvidenceProvider,
    REPOSITORY_ID,
    SYSTEM,
    _human,
    _owner_policy,
    _repository_evidence,
    _store,
    _write_preview_evidence,
)


def _http_error(**kwargs: object) -> HTTPException:
    return HTTPException(status_code=int(str(kwargs["status_code"])), detail=kwargs)


def _installation_token(
    _repository: str,
    _repository_id: str,
) -> GitHubAppInstallationToken:
    return GitHubAppInstallationToken(
        token="installation-token",
        app_id=42,
        installation_id=77,
        repository_id=int(REPOSITORY_ID),
        repository=REPOSITORY,
        expires_at="2026-08-07T15:00:00Z",
    )


class _GitHubCheckApi:
    def __init__(self, *, fail_read_numbers: tuple[int, ...] = ()) -> None:
        self.calls: list[dict[str, Any]] = []
        self.check_run: dict[str, Any] | None = None
        self.successful_write_bodies: list[dict[str, Any]] = []
        self.read_count = 0
        self.fail_read_numbers = set(fail_read_numbers)

    def __call__(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        method = str(kwargs.get("method") or "GET")
        if method == "DELETE":
            return None
        if method == "GET":
            self.read_count += 1
            if self.read_count in self.fail_read_numbers:
                raise RuntimeError("GitHub is unavailable")
            return {"check_runs": [self.check_run] if self.check_run is not None else []}
        body = dict(kwargs["body"])
        self.successful_write_bodies.append(body)
        if method == "POST":
            self.check_run = {
                "id": 91,
                **body,
                "conclusion": body.get("conclusion"),
                "app": {"id": 42},
            }
        else:
            assert self.check_run is not None
            self.check_run = {
                **self.check_run,
                **body,
            }
        return self.check_run


class _BlockingAcceptedProjectionApi(_GitHubCheckApi):
    def __init__(self) -> None:
        super().__init__()
        self.block_accepted_projection = True
        self.accepted_projection_entered = Event()
        self.release_accepted_projection = Event()

    def __call__(self, **kwargs: Any) -> object:
        body = kwargs.get("body")
        if (
            isinstance(body, dict)
            and isinstance(body.get("output"), dict)
            and body["output"].get("title") == "Owner acceptance: accepted"
            and self.block_accepted_projection
        ):
            self.accepted_projection_entered.set()
            if not self.release_accepted_projection.wait(timeout=10):
                raise RuntimeError("timed out waiting to release accepted projection")
        return super().__call__(**kwargs)


class _FailingDeleteApi(_GitHubCheckApi):
    def __init__(self, *, fail_delete_numbers: tuple[int, ...]) -> None:
        super().__init__()
        self.delete_count = 0
        self.fail_delete_numbers = set(fail_delete_numbers)

    def __call__(self, **kwargs: Any) -> object:
        if kwargs.get("method") == "DELETE":
            self.delete_count += 1
            if self.delete_count in self.fail_delete_numbers:
                raise RuntimeError("GitHub token revocation is unavailable")
        return super().__call__(**kwargs)


def _app(
    *,
    store: object,
    identity: LaunchplaneIdentity | None = None,
    browser_identity: LaunchplaneIdentity | None = None,
    repository_evidence_provider: _EvidenceProvider | None = None,
    authorization_allows: Callable[..., bool] | None = None,
    github_app_token: Callable[[str, str], GitHubAppInstallationToken] | None = None,
    github_api: Callable[..., object] | None = None,
    public_origin: str | None = None,
    projection_service: OwnerAcceptanceProjectionService | None = None,
) -> FastAPI:
    resolved_identity = identity or _human()
    resolved_browser_identity = browser_identity or resolved_identity
    resolved_github_api = github_api or _GitHubCheckApi()
    common = ReadRouteDependencies(
        read_identity=lambda: resolved_identity,
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-owner-acceptance",
        authorization_allows=authorization_allows or (lambda **_: True),
        http_error=_http_error,
        error_response_model=dict,  # type: ignore[arg-type]
    )
    app = FastAPI()
    register_owner_acceptance_routes(
        cast(ApiRouteRegistrar, app),
        dependencies=OwnerAcceptanceRouteDependencies(
            common=common,
            read_write_identity=lambda: resolved_identity,
            read_browser_mutation_identity=lambda: resolved_browser_identity,
            repository_evidence_provider=(
                repository_evidence_provider or _EvidenceProvider(_repository_evidence())
            ),
            github_app_token=github_app_token or _installation_token,
            public_origin=public_origin or "https://ops.example.test",
            github_api=resolved_github_api,
            projection_service=projection_service,
        ),
    )
    return app


def _postgres_store(root: Path) -> PostgresRecordStore:
    source_store = _store(root / "filesystem")
    store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'records.sqlite3'}")
    store.ensure_schema()
    store.import_core_records_from_filesystem(source_store)
    return store


def _write_repository_inventory(store: object) -> None:
    store.write_repository_inventory_record(  # type: ignore[attr-defined]
        RepositoryInventoryRecord(
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            inventory_state="tracked",
            inventory_revision=1,
            recorded_at="2026-08-07T00:00:00Z",
            source="test",
            reason="Owner review repository identity.",
        )
    )


class OwnerAcceptanceHttpTests(unittest.IsolatedAsyncioTestCase):
    def test_owner_review_status_maps_every_engineering_state_to_owner_semantics(self) -> None:
        self.assertEqual(
            {
                status: _owner_review_status(status)
                for status in (
                    "not_required",
                    "pending",
                    "accepted",
                    "changes_requested",
                    "revoked",
                    "stale",
                    "unavailable",
                )
            },
            {
                "not_required": "not_required",
                "pending": "review_required",
                "accepted": "accepted",
                "changes_requested": "changes_requested",
                "revoked": "review_required",
                "stale": "review_required",
                "unavailable": "unavailable",
            },
        )

    async def test_owner_evaluation_projects_only_owned_preview_review_fields(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            _write_preview_evidence(store)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(
                store=store,
                repository_evidence_provider=provider,
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )
            read_only_app = _app(
                store=store,
                repository_evidence_provider=provider,
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_READ_ACTION
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
            async with lifespan_client(read_only_app) as client:
                read_only_response = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(
            set(payload),
            {"status", "trace_id", "review_status", "evaluated_at", "products"},
        )
        self.assertEqual(payload["review_status"], "review_required")
        self.assertEqual(len(payload["products"]), 1)
        product = payload["products"][0]
        self.assertEqual(product["product"], PRODUCT)
        self.assertEqual(product["system"], SYSTEM)
        self.assertEqual(product["action"], "pull_request.owner_acceptance")
        self.assertEqual(product["preview_url"], "https://pr-2022.example.test")
        self.assertIs(product["can_accept"], True)
        self.assertIs(product["can_request_changes"], True)
        self.assertIs(product["can_revoke"], True)
        self.assertNotIn("decision", payload)
        self.assertNotIn("repository", product)
        self.assertNotIn("current_event", product)
        read_only_product = read_only_response.json()["products"][0]
        self.assertIs(read_only_product["can_accept"], False)
        self.assertIs(read_only_product["can_request_changes"], False)
        self.assertIs(read_only_product["can_revoke"], False)

    async def test_self_review_denied_owner_retains_non_accept_actions(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            _write_preview_evidence(store)
            owner = _human()
            provider = _EvidenceProvider(
                _repository_evidence(
                    authorship=ChangeImpactAuthorshipEvidence(
                        resolution="resolved",
                        contributor_github_ids=(owner.github_id,),
                        commit_count=1,
                    )
                )
            )
            app = _app(
                store=store,
                identity=owner,
                repository_evidence_provider=provider,
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["products"]), 1)
        product = response.json()["products"][0]
        self.assertEqual(product["product"], PRODUCT)
        self.assertIs(product["can_accept"], False)
        self.assertIs(product["can_request_changes"], True)
        self.assertIs(product["can_revoke"], True)

    async def test_owner_evaluation_withholds_membership_revoked_during_provider_read(
        self,
    ) -> None:
        class _RevokingProvider(_EvidenceProvider):
            def __init__(self, *, store: Any) -> None:
                super().__init__(_repository_evidence())
                self.store = store
                self.revoked = False

            def resolve(
                self,
                target: ChangeImpactTargetReference,
            ) -> ChangeImpactRepositoryEvidence:
                if not self.revoked:
                    current = self.store.list_product_owner_policy_records(
                        product=PRODUCT,
                        system=SYSTEM,
                    )[0]
                    self.store.write_product_owner_policy_record(
                        _owner_policy(
                            revision=2,
                            supersedes_record_id=current.record_id,
                            owners=(
                                ProductOwnerGrant(
                                    identity=ProductOwnerIdentity(
                                        provider="github",
                                        provider_subject_id="999999",
                                    ),
                                    repository_ids=(REPOSITORY_ID,),
                                    environments=("pull_request",),
                                ),
                            ),
                        )
                    )
                    self.revoked = True
                return super().resolve(target)

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            provider = _RevokingProvider(store=store)
            app = _app(store=store, repository_evidence_provider=provider)

            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertTrue(provider.revoked)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"]["code"], "owner_review_unavailable")
        self.assertNotIn(PRODUCT, response.text)

    async def test_owner_resolution_round_trip_uses_returned_preview_handles(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            _write_preview_evidence(store)
            app = _app(
                store=store,
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }

            async with lifespan_client(app) as client:
                initial = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params=target,
                )
                self.assertEqual(initial.status_code, 200, initial.text)
                binding_sha256 = initial.json()["products"][0]["binding_sha256"]
                changes_requested = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "changes_requested",
                        "expected_binding_sha256": binding_sha256,
                        "reason": "Clarify the product behavior.",
                    },
                    headers={"Idempotency-Key": "owner-resolution-request"},
                )
                pending_resolution = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params=target,
                )
                self.assertEqual(
                    pending_resolution.status_code,
                    200,
                    pending_resolution.text,
                )
                product = pending_resolution.json()["products"][0]
                references = product["resolution_evidence_references"]
                resolved = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": product["binding_sha256"],
                        "resolution": {
                            "schema_version": 1,
                            "summary": "The requested behavior is implemented.",
                            "resolved_evidence_references": references,
                        },
                    },
                    headers={"Idempotency-Key": "owner-resolution-accept"},
                )
                final = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params=target,
                )

        self.assertEqual(changes_requested.status_code, 202, changes_requested.text)
        self.assertEqual(changes_requested.json()["response_kind"], "receipt")
        self.assertEqual(product["review_status"], "changes_requested")
        self.assertIs(product["resolution_required"], True)
        self.assertGreater(len(references), 0)
        self.assertEqual(resolved.status_code, 202, resolved.text)
        self.assertEqual(resolved.json()["response_kind"], "receipt")
        self.assertEqual(final.status_code, 200, final.text)
        self.assertEqual(final.json()["products"][0]["review_status"], "accepted")

    async def test_owner_evaluation_prefilters_nonowners_before_provider_read(self) -> None:
        class _CountingProvider(_EvidenceProvider):
            calls = 0

            def resolve(self, target):  # type: ignore[no-untyped-def]
                self.calls += 1
                return super().resolve(target)

        class _UnavailableProvider(_EvidenceProvider):
            def resolve(self, target):  # type: ignore[no-untyped-def]
                raise ValueError("provider sentinel must not reach the response")

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            provider = _CountingProvider(_repository_evidence())
            app = _app(store=store, identity=_human(999999), repository_evidence_provider=provider)
            nonhuman_app = _app(
                store=store,
                identity=TerminalAgentIdentity(subject="agent", token_label="local"),
                repository_evidence_provider=provider,
            )
            unavailable_app = _app(
                store=store,
                repository_evidence_provider=_UnavailableProvider(_repository_evidence()),
            )
            async with lifespan_client(app) as client:
                unowned = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                nonexistent = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": "example/not-present", "pull_request_number": 2022},
                )
            async with lifespan_client(nonhuman_app) as client:
                nonhuman = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
            async with lifespan_client(unavailable_app) as client:
                unavailable = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertEqual(unowned.status_code, 404, unowned.text)
        self.assertEqual(nonexistent.status_code, 404, nonexistent.text)
        for response in (unowned, nonexistent, unavailable):
            self.assertEqual(response.json()["detail"]["code"], "owner_review_unavailable")
            self.assertEqual(
                response.json()["detail"]["message"],
                "This product review is unavailable.",
            )
        self.assertEqual(nonhuman.status_code, 403, nonhuman.text)
        self.assertEqual(nonhuman.json()["detail"]["code"], "github_human_required")
        self.assertEqual(provider.calls, 0)

    async def test_owner_evaluation_filters_foreign_product_and_broad_reads_stay_denied(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = _store(
                Path(directory),
                shared_dependency_evidence=True,
                include_second_product_authority=False,
            )
            _write_repository_inventory(store)
            app = _app(
                store=store,
                repository_evidence_provider=_EvidenceProvider(
                    _repository_evidence(path="src/shared/app.py")
                ),
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )
            async with lifespan_client(app) as client:
                owner_response = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                broad_responses = (
                    await client.get(
                        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                        params={"repository": REPOSITORY, "pull_request_number": 2022},
                    ),
                    await client.get("/v1/owner-acceptance/current-items"),
                    await client.get("/v1/owner-acceptance/queue"),
                    await client.get(
                        OWNER_ACCEPTANCE_EVENT_ROUTE.format(event_id="foreign-event-sentinel")
                    ),
                )

        self.assertEqual(owner_response.status_code, 200, owner_response.text)
        self.assertEqual(
            [product["product"] for product in owner_response.json()["products"]],
            [PRODUCT],
        )
        self.assertNotIn(SECOND_PRODUCT, owner_response.text)
        for response in broad_responses:
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["detail"]["code"], "authorization_denied")

    async def test_event_write_only_owner_receives_receipt_for_write_and_replay(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(
                Path(directory),
                shared_dependency_evidence=True,
                include_second_product_authority=False,
            )
            _write_repository_inventory(store)
            app = _app(
                store=store,
                repository_evidence_provider=_EvidenceProvider(
                    _repository_evidence(path="src/shared/app.py")
                ),
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(app) as client:
                owner_evaluation = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params=target,
                )
                binding_sha256 = owner_evaluation.json()["products"][0]["binding_sha256"]
                request = {
                    "target": target,
                    "action": "accepted",
                    "expected_binding_sha256": binding_sha256,
                }
                written = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json=request,
                    headers={"Idempotency-Key": "limited-owner-write"},
                )
                replayed = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json=request,
                    headers={"Idempotency-Key": "limited-owner-write"},
                )

        self.assertEqual(owner_evaluation.status_code, 200, owner_evaluation.text)
        self.assertEqual(
            [product["product"] for product in owner_evaluation.json()["products"]],
            [PRODUCT],
        )
        for response, write_status in ((written, "written"), (replayed, "replayed")):
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(
                response.json(),
                {
                    "response_kind": "receipt",
                    "status": "ok",
                    "trace_id": "trace-owner-acceptance",
                    "write_status": write_status,
                },
            )
            self.assertNotIn(SECOND_PRODUCT, response.text)

    async def test_limited_event_write_prefilters_unowned_targets_before_provider_read(
        self,
    ) -> None:
        class _CountingProvider(_EvidenceProvider):
            calls = 0

            def resolve(self, target):  # type: ignore[no-untyped-def]
                self.calls += 1
                return super().resolve(target)

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            provider = _CountingProvider(_repository_evidence())
            app = _app(
                store=store,
                browser_identity=_human(999999),
                repository_evidence_provider=provider,
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )
            async with lifespan_client(app) as client:
                responses = (
                    await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": "0" * 64,
                        },
                        headers={"Idempotency-Key": "foreign-owner-write"},
                    ),
                    await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": "example/not-present",
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": "0" * 64,
                        },
                        headers={"Idempotency-Key": "missing-owner-write"},
                    ),
                )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(responses[0].status_code, 404)
        self.assertEqual(responses[0].json()["detail"]["code"], "owner_review_unavailable")

    async def test_limited_event_write_rejects_malformed_owner_policy_history(
        self,
    ) -> None:
        class _CountingProvider(_EvidenceProvider):
            calls = 0

            def resolve(self, target):  # type: ignore[no-untyped-def]
                self.calls += 1
                return super().resolve(target)

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            current_policy = store.list_product_owner_policy_records(
                product=PRODUCT,
                system=SYSTEM,
            )[0]
            broken_revision = _owner_policy(
                revision=3,
                supersedes_record_id=current_policy.record_id,
            ).model_copy(update={"status": "superseded"})
            provider = _CountingProvider(_repository_evidence())
            app = _app(
                store=store,
                repository_evidence_provider=provider,
                authorization_allows=lambda **kwargs: (
                    kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION
                ),
            )
            with patch.object(
                store,
                "list_product_owner_policy_records",
                return_value=(current_policy, broken_revision),
            ):
                async with lifespan_client(app) as client:
                    response = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": "0" * 64,
                        },
                        headers={"Idempotency-Key": "malformed-owner-policy"},
                    )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"]["code"], "owner_review_unavailable")
        self.assertEqual(provider.calls, 0)

    async def test_limited_event_write_bounds_provider_and_binding_errors(self) -> None:
        class _UnavailableProvider(_EvidenceProvider):
            def resolve(self, target):  # type: ignore[no-untyped-def]
                raise ValueError("secret foreign-product provider sentinel")

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)

            def authorization_allows(**kwargs: object) -> bool:
                return kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION

            unavailable_app = _app(
                store=store,
                repository_evidence_provider=_UnavailableProvider(_repository_evidence()),
                authorization_allows=authorization_allows,
            )
            provider = _EvidenceProvider(_repository_evidence())
            drift_app = _app(
                store=store,
                repository_evidence_provider=provider,
                authorization_allows=authorization_allows,
            )
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(unavailable_app) as client:
                unavailable = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": "0" * 64,
                    },
                    headers={"Idempotency-Key": "limited-provider-error"},
                )
            async with lifespan_client(drift_app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params=target,
                )
                provider.evidence = _repository_evidence(head="c" * 40)
                drift = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": evaluated.json()["products"][0][
                            "binding_sha256"
                        ],
                    },
                    headers={"Idempotency-Key": "limited-binding-drift"},
                )

        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(
            unavailable.json()["detail"],
            {
                "status_code": 503,
                "trace_id": "trace-owner-acceptance",
                "code": "owner_acceptance_projection_unavailable",
                "message": "Owner acceptance projection is unavailable.",
            },
        )
        self.assertNotIn("sentinel", unavailable.text)
        self.assertEqual(drift.status_code, 409, drift.text)
        self.assertEqual(drift.json()["detail"]["code"], "owner_acceptance_binding_changed")
        self.assertEqual(
            drift.json()["detail"]["message"],
            "The reviewed Owner acceptance binding changed.",
        )

    async def test_limited_event_write_preserves_uncertain_reconciliation_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_repository_inventory(store)
            github_api = _GitHubCheckApi(fail_read_numbers=(2,))

            def authorization_allows(**kwargs: object) -> bool:
                return kwargs.get("action") == OWNER_ACCEPTANCE_EVENT_WRITE_ACTION

            app = _app(
                store=store,
                github_api=github_api,
                authorization_allows=authorization_allows,
            )
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
                    params=target,
                )
                request = {
                    "target": target,
                    "action": "accepted",
                    "expected_binding_sha256": evaluated.json()["products"][0]["binding_sha256"],
                }
                failed = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json=request,
                    headers={"Idempotency-Key": "limited-reconciliation"},
                )
                replayed = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json=request,
                    headers={"Idempotency-Key": "limited-reconciliation"},
                )

        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertEqual(
            failed.json()["detail"],
            {
                "status_code": 503,
                "trace_id": "trace-owner-acceptance",
                "code": "owner_acceptance_projection_reconciliation_required",
                "message": (
                    "Owner acceptance event was persisted, but the final GitHub status "
                    "projection requires reconciliation. Retry with the same "
                    "Idempotency-Key only."
                ),
            },
        )
        self.assertNotIn("projection endpoint", failed.text)
        self.assertEqual(
            replayed.json(),
            {
                "response_kind": "receipt",
                "status": "ok",
                "trace_id": "trace-owner-acceptance",
                "write_status": "replayed",
            },
        )

    async def test_preview_projection_and_negative_event_share_projection_lock(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _BlockingAcceptedProjectionApi()
            github_api.block_accepted_projection = False
            provider = _EvidenceProvider(_repository_evidence())
            projection_service = OwnerAcceptanceProjectionService(
                repository_evidence_provider=provider,
                github_app_token=_installation_token,
                public_origin="https://ops.example.test",
                api_request=github_api,
            )
            app = _app(
                store=store,
                repository_evidence_provider=provider,
                github_api=github_api,
                projection_service=projection_service,
            )

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                accepted = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2022,
                        },
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "preview-lock-accepted"},
                )
                self.assertEqual(accepted.status_code, 202, accepted.text)

                assert github_api.check_run is not None
                github_api.check_run["external_id"] = "0" * 64
                github_api.block_accepted_projection = True
                github_api.accepted_projection_entered.clear()
                github_api.release_accepted_projection.clear()
                preview_task = asyncio.create_task(
                    asyncio.to_thread(
                        projection_service.reconcile_if_required,
                        store=store,
                        target=ChangeImpactTargetReference(
                            repository=REPOSITORY,
                            pull_request_number=2022,
                        ),
                        source_event_id="preview-ready-feedback",
                    )
                )
                entered = await asyncio.to_thread(
                    github_api.accepted_projection_entered.wait,
                    10,
                )
                self.assertIs(entered, True)
                changes_task = asyncio.create_task(
                    client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "changes_requested",
                            "expected_binding_sha256": binding_sha256,
                            "reason": "Preview validation found a blocking correction.",
                        },
                        headers={"Idempotency-Key": "preview-lock-changes-requested"},
                    )
                )
                await asyncio.sleep(0.1)
                self.assertIs(changes_task.done(), False)
                github_api.release_accepted_projection.set()
                _, changes_requested = await asyncio.gather(preview_task, changes_task)

        self.assertEqual(changes_requested.status_code, 202, changes_requested.text)
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["conclusion"], "action_required")
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: changes requested",
        )

    async def test_preview_projection_revokes_its_installation_token(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            service = OwnerAcceptanceProjectionService(
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                github_app_token=_installation_token,
                public_origin="https://ops.example.test",
                api_request=github_api,
            )

            outcome = service.reconcile_if_required(
                store=store,
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                source_event_id="preview-ready-token-revocation",
            )

        self.assertIsNotNone(outcome.result)
        self.assertEqual(
            [call["method"] for call in github_api.calls if call.get("method") == "DELETE"],
            ["DELETE"],
        )

    async def test_bindingless_negative_decisions_project_exact_target(self) -> None:
        cases = (
            (
                OwnerAcceptanceDecision(
                    status="stale",
                    reason_code="change_impact_stale",
                    evaluated_at="2026-08-17T01:30:00Z",
                ),
                "action_required",
            ),
            (
                OwnerAcceptanceDecision(
                    status="unavailable",
                    reason_code="change_impact_unavailable",
                    evaluated_at="2026-08-17T01:30:00Z",
                ),
                "failure",
            ),
        )
        exact_target = _repository_evidence().target
        for decision, expected_conclusion in cases:
            with self.subTest(status=decision.status), TemporaryDirectory() as directory:
                store = _store(Path(directory))
                github_api = _GitHubCheckApi()
                service = OwnerAcceptanceProjectionService(
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    github_app_token=_installation_token,
                    public_origin="https://ops.example.test",
                    api_request=github_api,
                )

                with patch.object(
                    OwnerAcceptanceProjectionService,
                    "resolve_current",
                    return_value=(decision, exact_target),
                ):
                    outcome = service.reconcile_if_required(
                        store=store,
                        target=ChangeImpactTargetReference(
                            repository=REPOSITORY,
                            pull_request_number=2022,
                        ),
                        source_event_id=f"bindingless-{decision.status}",
                    )

                self.assertIsNone(outcome.decision.binding)
                self.assertEqual(outcome.target, exact_target)
                self.assertIsNotNone(outcome.result)
                assert github_api.check_run is not None
                self.assertEqual(github_api.check_run["head_sha"], exact_target.head_sha)
                self.assertEqual(github_api.check_run["conclusion"], expected_conclusion)
                self.assertIn(
                    "No product-specific Owner decision is available",
                    github_api.check_run["output"]["summary"],
                )

    async def test_not_required_decision_intentionally_skips_projection(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            service = OwnerAcceptanceProjectionService(
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                github_app_token=_installation_token,
                public_origin="https://ops.example.test",
                api_request=github_api,
            )
            decision = OwnerAcceptanceDecision(
                status="not_required",
                reason_code="engineering_only",
                evaluated_at="2026-08-17T01:30:00Z",
            )
            exact_target = _repository_evidence().target

            with patch.object(
                OwnerAcceptanceProjectionService,
                "resolve_current",
                return_value=(decision, exact_target),
            ):
                outcome = service.reconcile_if_required(
                    store=store,
                    target=ChangeImpactTargetReference(
                        repository=REPOSITORY,
                        pull_request_number=2022,
                    ),
                    source_event_id="not-required-no-projection",
                )

        self.assertIsNone(outcome.result)
        self.assertEqual(github_api.calls, [])

    async def test_restoration_marks_exact_target_before_reresolution(self) -> None:
        exact_target = _repository_evidence().target
        service = OwnerAcceptanceProjectionService(
            repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
            github_app_token=_installation_token,
            public_origin="https://ops.example.test",
        )
        operations: list[str] = []

        def project_reconciliation_required(*_args: object, **_kwargs: object) -> MagicMock:
            operations.append("mark-required")
            return MagicMock()

        def fail_resolution(*_args: object, **_kwargs: object) -> object:
            operations.append("resolve")
            raise RuntimeError("repository evidence is unavailable")

        with (
            patch.object(
                OwnerAcceptanceProjectionService,
                "project_reconciliation_required_locked",
                side_effect=project_reconciliation_required,
            ),
            patch.object(
                OwnerAcceptanceProjectionService,
                "resolve_current",
                side_effect=fail_resolution,
            ),
            self.assertRaisesRegex(RuntimeError, "repository evidence is unavailable"),
        ):
            service.restore_reconciliation_required_locked(
                store=MagicMock(),
                target=ChangeImpactTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                ),
                lock_target=exact_target,
                exact_target=exact_target,
                source_event_id="restore-exact-first",
            )

        self.assertEqual(operations, ["mark-required", "resolve"])

    async def test_projects_pending_owner_decision_as_in_progress_check(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            calls: list[dict[str, Any]] = []

            def github_api(**kwargs):  # type: ignore[no-untyped-def]
                calls.append(kwargs)
                if kwargs.get("method") == "DELETE":
                    return None
                if kwargs.get("method") == "POST":
                    body = kwargs["body"]
                    return {
                        "id": 91,
                        "name": body["name"],
                        "head_sha": body["head_sha"],
                        "status": body["status"],
                        "conclusion": body.get("conclusion"),
                        "external_id": body["external_id"],
                        "details_url": body["details_url"],
                        "output": body["output"],
                        "app": {"id": 42},
                    }
                return {"check_runs": []}

            app = _app(
                store=store,
                github_app_token=lambda _repository, _repository_id: GitHubAppInstallationToken(
                    token="installation-token",
                    app_id=42,
                    installation_id=77,
                    repository_id=int(REPOSITORY_ID),
                    repository=REPOSITORY,
                    expires_at="2026-08-07T15:00:00Z",
                ),
                github_api=github_api,
                public_origin="https://ops.example.test",
            )

            async with lifespan_client(app) as client:
                response = await client.post(
                    OWNER_ACCEPTANCE_PROJECT_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2022,
                        }
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["decision"]["status"], "pending")
        self.assertEqual(payload["result"]["name"], "launchplane/owner-acceptance")
        self.assertEqual(payload["result"]["check_status"], "in_progress")
        self.assertIsNone(payload["result"]["conclusion"])
        self.assertEqual(
            calls[-2]["body"]["details_url"],
            "https://ops.example.test/ui/owner-review?repository=example%2Fweb&pull_request=2022",
        )
        self.assertEqual(calls[-2]["body"]["status"], "in_progress")
        self.assertNotIn("conclusion", calls[-2]["body"])
        self.assertEqual(calls[-1]["method"], "DELETE")

    async def test_successful_event_projects_conservative_then_final_decision(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()

            app = _app(
                store=store,
                github_api=github_api,
            )

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                        "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                            "binding_sha256"
                        ],
                    },
                    headers={"Idempotency-Key": "accept-and-project"},
                )
                event_count = len(store.list_owner_acceptance_event_records())

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(event_count, 1)
        self.assertEqual(
            [body.get("conclusion") for body in github_api.successful_write_bodies],
            [None, "success"],
        )
        self.assertEqual(
            [body["status"] for body in github_api.successful_write_bodies],
            ["in_progress", "completed"],
        )
        self.assertEqual(
            [body["output"]["title"] for body in github_api.successful_write_bodies],
            ["Owner acceptance: updating decision", "Owner acceptance: accepted"],
        )
        projection_body = github_api.successful_write_bodies[-1]
        self.assertEqual(
            projection_body["details_url"],
            "https://ops.example.test/ui/owner-review?repository=example%2Fweb&pull_request=2022",
        )

    async def test_prewrite_projection_failure_blocks_event_append(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi(fail_read_numbers=(1,))
            app = _app(
                store=store,
                github_api=github_api,
            )

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                        "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                            "binding_sha256"
                        ],
                    },
                    headers={"Idempotency-Key": "accept-with-projection-failure"},
                )
                event_count = len(store.list_owner_acceptance_event_records())

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_projection_unavailable",
        )
        self.assertEqual(event_count, 0)

    async def test_event_write_failure_confirms_absence_and_projects_failure(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                with patch.object(
                    store,
                    "write_owner_acceptance_event_record",
                    side_effect=RuntimeError("storage write failed"),
                ):
                    response = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                                "binding_sha256"
                            ],
                        },
                        headers={"Idempotency-Key": "accept-with-storage-failure"},
                    )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_event_write_failed",
        )
        self.assertIn("was not persisted", response.json()["detail"]["message"])
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["status"], "completed")
        self.assertEqual(github_api.check_run["conclusion"], "failure")
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: update failed",
        )
        self.assertEqual(
            [body.get("conclusion") for body in github_api.successful_write_bodies],
            [None, "failure"],
        )

    async def test_event_write_failure_reports_unrestored_github_projection(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi(fail_read_numbers=(2,))
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                with patch.object(
                    store,
                    "write_owner_acceptance_event_record",
                    side_effect=RuntimeError("storage write failed"),
                ):
                    response = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                                "binding_sha256"
                            ],
                        },
                        headers={"Idempotency-Key": "accept-with-unrestored-failure"},
                    )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_event_write_failed_projection_unknown",
        )
        self.assertIn("could not confirm", response.json()["detail"]["message"])
        self.assertNotIn("GitHub shows", response.json()["detail"]["message"])
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["status"], "in_progress")
        self.assertIsNone(github_api.check_run["conclusion"])

    async def test_concurrent_transition_error_preserves_domain_response(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                with patch.object(
                    store,
                    "write_owner_acceptance_event_record",
                    side_effect=OwnerAcceptanceTransitionError("concurrent transition"),
                ):
                    response = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                                "binding_sha256"
                            ],
                        },
                        headers={"Idempotency-Key": "accept-with-transition-race"},
                    )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_transition_invalid",
        )
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["status"], "in_progress")
        self.assertIsNone(github_api.check_run["conclusion"])
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: pending",
        )

    async def test_event_write_failure_detects_confirmed_persistence(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            app = _app(store=store, github_api=github_api)
            persist_event = store.write_owner_acceptance_event_record

            def persist_then_fail(record):  # type: ignore[no-untyped-def]
                persist_event(record)
                raise RuntimeError("storage acknowledgement failed")

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                with patch.object(
                    store,
                    "write_owner_acceptance_event_record",
                    side_effect=persist_then_fail,
                ):
                    response = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                                "binding_sha256"
                            ],
                        },
                        headers={"Idempotency-Key": "accept-with-unknown-ack"},
                    )
                event_count = len(store.list_owner_acceptance_event_records())

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_projection_reconciliation_required",
        )
        self.assertIn("was persisted", response.json()["detail"]["message"])
        self.assertEqual(event_count, 1)
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["conclusion"], "action_required")
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: reconciliation required",
        )

    async def test_event_write_failure_projects_unknown_persistence_outcome(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                with (
                    patch.object(
                        store,
                        "write_owner_acceptance_event_record",
                        side_effect=RuntimeError("storage write failed"),
                    ),
                    patch.object(
                        store,
                        "read_owner_acceptance_event_record",
                        side_effect=RuntimeError("storage read failed"),
                    ),
                ):
                    response = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "accepted",
                            "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                                "binding_sha256"
                            ],
                        },
                        headers={"Idempotency-Key": "accept-with-unknown-write"},
                    )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_event_write_outcome_unknown",
        )
        self.assertIn("could not determine", response.json()["detail"]["message"])
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["conclusion"], "failure")
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: write outcome unknown",
        )

    async def test_final_projection_failure_requires_idempotent_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi(fail_read_numbers=(2,))
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                request = {
                    "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                    "action": "accepted",
                    "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                        "binding_sha256"
                    ],
                }
                failed = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json=request,
                    headers={"Idempotency-Key": "accept-with-projection-failure"},
                )
                event_count_after_failure = len(store.list_owner_acceptance_event_records())
                assert github_api.check_run is not None
                conclusion_after_failure = github_api.check_run.get("conclusion")
                reconciled = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json=request,
                    headers={"Idempotency-Key": "accept-with-projection-failure"},
                )
                event_count_after_replay = len(store.list_owner_acceptance_event_records())

        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertEqual(
            failed.json()["detail"]["code"],
            "owner_acceptance_projection_reconciliation_required",
        )
        self.assertEqual(event_count_after_failure, 1)
        self.assertEqual(conclusion_after_failure, "action_required")
        self.assertEqual(reconciled.status_code, 202, reconciled.text)
        self.assertEqual(reconciled.json()["write_status"], "replayed")
        self.assertEqual(event_count_after_replay, 1)
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["conclusion"], "success")
        self.assertEqual(
            [body.get("conclusion") for body in github_api.successful_write_bodies],
            [None, "action_required", None, "success"],
        )
        self.assertEqual(
            [body["status"] for body in github_api.successful_write_bodies],
            ["in_progress", "completed", "in_progress", "completed"],
        )

    async def test_final_token_revoke_failure_restores_conservative_projection(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _FailingDeleteApi(fail_delete_numbers=(2,))
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2022,
                        },
                        "action": "accepted",
                        "expected_binding_sha256": evaluated.json()["decision"]["binding"][
                            "binding_sha256"
                        ],
                    },
                    headers={"Idempotency-Key": "accept-with-revoke-failure"},
                )
                event_count = len(store.list_owner_acceptance_event_records())

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "owner_acceptance_projection_reconciliation_required",
        )
        self.assertEqual(event_count, 1)
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["conclusion"], "action_required")
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: reconciliation required",
        )
        self.assertEqual(
            [body.get("conclusion") for body in github_api.successful_write_bodies],
            [None, "success", "action_required"],
        )
        self.assertEqual(
            [body["status"] for body in github_api.successful_write_bodies],
            ["in_progress", "completed", "completed"],
        )

    async def test_concurrent_negative_event_cannot_restore_stale_success(self) -> None:
        for store_kind in ("filesystem", "sqlite"):
            with self.subTest(store=store_kind), TemporaryDirectory() as directory:
                root = Path(directory)
                store = _store(root) if store_kind == "filesystem" else _postgres_store(root)
                github_api = _BlockingAcceptedProjectionApi()
                app = _app(store=store, github_api=github_api)

                async with lifespan_client(app) as client:
                    evaluated = await client.get(
                        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                        params={"repository": REPOSITORY, "pull_request_number": 2022},
                    )
                    binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                    accepted_task = asyncio.create_task(
                        client.post(
                            OWNER_ACCEPTANCE_EVENTS_ROUTE,
                            json={
                                "target": {
                                    "repository": REPOSITORY,
                                    "pull_request_number": 2022,
                                },
                                "action": "accepted",
                                "expected_binding_sha256": binding_sha256,
                            },
                            headers={"Idempotency-Key": "concurrent-accepted"},
                        )
                    )
                    entered = await asyncio.to_thread(
                        github_api.accepted_projection_entered.wait,
                        10,
                    )
                    self.assertIs(entered, True)
                    changes_task = asyncio.create_task(
                        client.post(
                            OWNER_ACCEPTANCE_EVENTS_ROUTE,
                            json={
                                "target": {
                                    "repository": REPOSITORY,
                                    "pull_request_number": 2022,
                                },
                                "action": "changes_requested",
                                "expected_binding_sha256": binding_sha256,
                                "reason": "A concurrent product correction is required.",
                            },
                            headers={"Idempotency-Key": "concurrent-changes-requested"},
                        )
                    )
                    await asyncio.sleep(0.1)
                    self.assertIs(changes_task.done(), False)
                    github_api.release_accepted_projection.set()
                    accepted, changes_requested = await asyncio.gather(
                        accepted_task,
                        changes_task,
                    )
                    events = sorted(
                        store.list_owner_acceptance_event_records(),
                        key=lambda event: event.subject_sequence,
                    )

                self.assertEqual(accepted.status_code, 202, accepted.text)
                self.assertEqual(changes_requested.status_code, 202, changes_requested.text)
                self.assertEqual(
                    [event.action for event in events],
                    ["accepted", "changes_requested"],
                )
                assert github_api.check_run is not None
                self.assertEqual(github_api.check_run["conclusion"], "action_required")
                self.assertEqual(
                    github_api.check_run["output"]["title"],
                    "Owner acceptance: changes requested",
                )

    async def test_projection_endpoint_cannot_overwrite_concurrent_negative_event(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _BlockingAcceptedProjectionApi()
            github_api.block_accepted_projection = False
            app = _app(store=store, github_api=github_api)

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                accepted = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2022,
                        },
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "projection-race-accepted"},
                )
                self.assertEqual(accepted.status_code, 202, accepted.text)
                assert github_api.check_run is not None
                github_api.check_run["external_id"] = "0" * 64
                github_api.block_accepted_projection = True
                github_api.accepted_projection_entered.clear()
                github_api.release_accepted_projection.clear()
                projection_task = asyncio.create_task(
                    client.post(
                        OWNER_ACCEPTANCE_PROJECT_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            }
                        },
                    )
                )
                entered = await asyncio.to_thread(
                    github_api.accepted_projection_entered.wait,
                    10,
                )
                self.assertIs(entered, True)
                changes_task = asyncio.create_task(
                    client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": {
                                "repository": REPOSITORY,
                                "pull_request_number": 2022,
                            },
                            "action": "changes_requested",
                            "expected_binding_sha256": binding_sha256,
                            "reason": "The projected approval is no longer current.",
                        },
                        headers={"Idempotency-Key": "projection-race-changes"},
                    )
                )
                await asyncio.sleep(0.1)
                self.assertIs(changes_task.done(), False)
                github_api.release_accepted_projection.set()
                projected, changes_requested = await asyncio.gather(
                    projection_task,
                    changes_task,
                )

        self.assertEqual(projected.status_code, 200, projected.text)
        self.assertEqual(changes_requested.status_code, 202, changes_requested.text)
        assert github_api.check_run is not None
        self.assertEqual(github_api.check_run["conclusion"], "action_required")
        self.assertEqual(
            github_api.check_run["output"]["title"],
            "Owner acceptance: changes requested",
        )

    async def test_projection_rejects_head_drift_before_github_write(self) -> None:
        class _DriftingProvider(_EvidenceProvider):
            def __init__(self) -> None:
                super().__init__(_repository_evidence())
                self.calls = 0

            def resolve(self, target):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls >= 3:
                    return _repository_evidence(head="c" * 40)
                return super().resolve(target)

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_calls: list[dict[str, Any]] = []
            app = _app(
                store=store,
                repository_evidence_provider=_DriftingProvider(),
                github_app_token=lambda _repository, _repository_id: GitHubAppInstallationToken(
                    token="installation-token",
                    app_id=42,
                    installation_id=77,
                    repository_id=int(REPOSITORY_ID),
                    repository=REPOSITORY,
                    expires_at="2026-08-07T15:00:00Z",
                ),
                github_api=lambda **kwargs: github_calls.append(kwargs),
            )

            async with lifespan_client(app) as client:
                response = await client.post(
                    OWNER_ACCEPTANCE_PROJECT_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2022,
                        }
                    },
                )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(github_calls, [])

    async def test_evaluate_and_human_event_use_server_derived_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }

            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params=target,
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                self.assertEqual(evaluated.json()["decision"]["status"], "pending")
                self.assertIs(
                    evaluated.json()["viewer_capabilities"]["event_write_authorized"],
                    True,
                )
                eligibility = evaluated.json()["viewer_capabilities"]["bindings"]
                self.assertEqual(len(eligibility), 1)
                self.assertIs(eligibility[0]["can_submit_event"], True)
                self.assertIs(eligibility[0]["can_accept"], True)
                self.assertIs(eligibility[0]["can_request_changes"], True)
                self.assertIs(eligibility[0]["can_revoke"], True)
                self.assertEqual(eligibility[0]["reason_code"], "current_product_owner")
                expected_binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                self.assertEqual(eligibility[0]["binding_sha256"], expected_binding_sha256)

                injected = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
                        "head_sha": "c" * 40,
                    },
                    headers={"Idempotency-Key": "accept-1"},
                )
                self.assertEqual(injected.status_code, 422, injected.text)
                self.assertEqual(store.list_owner_acceptance_event_records(), ())

                written = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
                    },
                    headers={"Idempotency-Key": "accept-1"},
                )
                self.assertEqual(written.status_code, 202, written.text)
                payload = written.json()
                self.assertEqual(payload["response_kind"], "full")
                self.assertEqual(payload["write_status"], "written")
                self.assertEqual(payload["decision"]["status"], "accepted")
                self.assertEqual(payload["record"]["subject_sequence"], 1)
                binding = payload["record"]["binding"]
                self.assertEqual(binding["repository"], REPOSITORY)
                self.assertEqual(binding["head_sha"], "a" * 40)
                self.assertIn("owner_policy_digest", binding)
                self.assertIn("owner_requirement_digest", binding)

                replayed = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
                    },
                    headers={"Idempotency-Key": "accept-1"},
                )
                self.assertEqual(replayed.status_code, 202, replayed.text)
                self.assertEqual(replayed.json()["response_kind"], "full")
                self.assertEqual(replayed.json()["write_status"], "replayed")
                self.assertEqual(replayed.json()["record"], payload["record"])

                read = await client.get(
                    OWNER_ACCEPTANCE_EVENT_ROUTE.format(event_id=payload["record"]["event_id"])
                )
                self.assertEqual(read.status_code, 200, read.text)
                self.assertEqual(read.json()["record"], payload["record"])

    async def test_evaluation_reports_read_only_viewer_capability(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            checked_actions: list[object] = []

            def authorization_allows(**kwargs: object) -> bool:
                checked_actions.append(kwargs.get("action"))
                return kwargs.get("action") == OWNER_ACCEPTANCE_READ_ACTION

            app = _app(store=store, authorization_allows=authorization_allows)
            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["decision"]["status"], "pending")
        self.assertIs(payload["viewer_capabilities"]["event_write_authorized"], False)
        self.assertEqual(payload["viewer_capabilities"]["bindings"], [])
        self.assertIn(OWNER_ACCEPTANCE_READ_ACTION, checked_actions)
        self.assertIn(OWNER_ACCEPTANCE_EVENT_WRITE_ACTION, checked_actions)

    async def test_evaluation_reports_non_owner_before_write_and_write_still_denies(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store, identity=_human(999999))
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params=target,
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                payload = evaluated.json()
                eligibility = payload["viewer_capabilities"]["bindings"]
                self.assertEqual(len(eligibility), 1)
                self.assertIs(eligibility[0]["can_submit_event"], False)
                self.assertIs(eligibility[0]["can_accept"], False)
                self.assertIs(eligibility[0]["can_request_changes"], False)
                self.assertIs(eligibility[0]["can_revoke"], False)
                self.assertEqual(
                    eligibility[0]["reason_code"],
                    "not_current_product_owner",
                )
                binding_sha256 = payload["decision"]["binding"]["binding_sha256"]
                self.assertEqual(eligibility[0]["binding_sha256"], binding_sha256)

                denied = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "non-owner-denied"},
                )

        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            denied.json()["detail"]["code"],
            "owner_acceptance_authorization_denied",
        )

    async def test_evaluation_marks_non_human_viewer_unsupported(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(
                store=store,
                identity=TerminalAgentIdentity(subject="agent", token_label="local"),
            )
            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertEqual(response.status_code, 200, response.text)
        eligibility = response.json()["viewer_capabilities"]["bindings"]
        self.assertEqual(len(eligibility), 1)
        self.assertIs(eligibility[0]["can_submit_event"], False)
        self.assertEqual(eligibility[0]["reason_code"], "viewer_identity_unsupported")

    async def test_multi_product_flow_uses_digest_selection_without_product_input(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), shared_dependency_evidence=True)
            provider = _EvidenceProvider(_repository_evidence(path="src/shared/app.py"))
            app = _app(store=store, repository_evidence_provider=provider)
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }

            async with lifespan_client(app) as client:
                evaluated = await client.get(OWNER_ACCEPTANCE_EVALUATION_ROUTE, params=target)
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                decision = evaluated.json()["decision"]
                eligibility = evaluated.json()["viewer_capabilities"]["bindings"]
                self.assertEqual(
                    [product["product"] for product in decision["products"]],
                    [PRODUCT, SECOND_PRODUCT],
                )
                self.assertEqual(len(eligibility), 2)
                self.assertTrue(all(entry["can_submit_event"] for entry in eligibility))
                bindings = {
                    product["product"]: product["binding"]["binding_sha256"]
                    for product in decision["products"]
                }

                injected = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": bindings[SECOND_PRODUCT],
                        "product": SECOND_PRODUCT,
                    },
                    headers={"Idempotency-Key": "accept-multi-product"},
                )
                self.assertEqual(injected.status_code, 422, injected.text)
                self.assertEqual(store.list_owner_acceptance_event_records(), ())

                accepted_second = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": bindings[SECOND_PRODUCT],
                    },
                    headers={"Idempotency-Key": "accept-multi-product"},
                )
                self.assertEqual(accepted_second.status_code, 202, accepted_second.text)
                second_payload = accepted_second.json()
                self.assertEqual(second_payload["record"]["binding"]["product"], SECOND_PRODUCT)
                self.assertEqual(second_payload["decision"]["status"], "pending")

                accepted_first = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": bindings[PRODUCT],
                    },
                    headers={"Idempotency-Key": "accept-multi-product"},
                )
                self.assertEqual(accepted_first.status_code, 202, accepted_first.text)
                first_payload = accepted_first.json()
                self.assertEqual(first_payload["record"]["binding"]["product"], PRODUCT)
                self.assertEqual(first_payload["decision"]["status"], "accepted")
                self.assertEqual(
                    [product["status"] for product in first_payload["decision"]["products"]],
                    ["accepted", "accepted"],
                )
                self.assertEqual(len(store.list_owner_acceptance_event_records()), 2)

    async def test_multi_product_eligibility_is_independent_per_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), shared_dependency_evidence=True)
            current_second_policy = store.list_product_owner_policy_records(
                product=SECOND_PRODUCT,
                system=SYSTEM,
            )[0]
            store.write_product_owner_policy_record(
                _owner_policy(
                    product=SECOND_PRODUCT,
                    revision=2,
                    supersedes_record_id=current_second_policy.record_id,
                    owners=(
                        ProductOwnerGrant(
                            identity=ProductOwnerIdentity(
                                provider="github",
                                provider_subject_id="999999",
                            ),
                            repository_ids=(REPOSITORY_ID,),
                            environments=("pull_request",),
                        ),
                    ),
                )
            )
            app = _app(
                store=store,
                repository_evidence_provider=_EvidenceProvider(
                    _repository_evidence(path="src/shared/app.py")
                ),
            )
            async with lifespan_client(app) as client:
                response = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )

        self.assertEqual(response.status_code, 200, response.text)
        eligibility = {
            entry["product"]: entry for entry in response.json()["viewer_capabilities"]["bindings"]
        }
        self.assertIs(eligibility[PRODUCT]["can_submit_event"], True)
        self.assertEqual(
            eligibility[PRODUCT]["reason_code"],
            "current_product_owner",
        )
        self.assertIs(eligibility[SECOND_PRODUCT]["can_submit_event"], False)
        self.assertEqual(
            eligibility[SECOND_PRODUCT]["reason_code"],
            "not_current_product_owner",
        )

    async def test_changes_requested_and_revoked_require_reasons_and_evaluate(self) -> None:
        target: dict[str, str | int] = {
            "repository": REPOSITORY,
            "pull_request_number": 2022,
        }
        for action, expected_status in (("changes_requested", "changes_requested"),):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                store = _store(Path(directory))
                github_api = _GitHubCheckApi()
                app = _app(store=store, github_api=github_api)
                async with lifespan_client(app) as client:
                    evaluated = await client.get(
                        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                        params=target,
                    )
                    self.assertEqual(evaluated.status_code, 200, evaluated.text)
                    expected_binding_sha256 = evaluated.json()["decision"]["binding"][
                        "binding_sha256"
                    ]
                    missing_reason = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": target,
                            "action": action,
                            "expected_binding_sha256": expected_binding_sha256,
                        },
                        headers={"Idempotency-Key": f"{action}-missing-reason"},
                    )
                    self.assertEqual(missing_reason.status_code, 422, missing_reason.text)
                    self.assertEqual(store.list_owner_acceptance_event_records(), ())

                    written = await client.post(
                        OWNER_ACCEPTANCE_EVENTS_ROUTE,
                        json={
                            "target": target,
                            "action": action,
                            "expected_binding_sha256": expected_binding_sha256,
                            "reason": "Owner provided actionable feedback.",
                        },
                        headers={"Idempotency-Key": action},
                    )
                    self.assertEqual(written.status_code, 202, written.text)
                    self.assertEqual(written.json()["decision"]["status"], expected_status)
                    assert github_api.check_run is not None
                    self.assertEqual(github_api.check_run["conclusion"], "action_required")

                    evaluated = await client.get(
                        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                        params=target,
                    )
                    self.assertEqual(evaluated.status_code, 200, evaluated.text)
                    self.assertEqual(evaluated.json()["decision"]["status"], expected_status)

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            github_api = _GitHubCheckApi()
            app = _app(store=store, github_api=github_api)
            async with lifespan_client(app) as client:
                evaluated = await client.get(OWNER_ACCEPTANCE_EVALUATION_ROUTE, params=target)
                binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                accepted = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "accept-before-revoke"},
                )
                self.assertEqual(accepted.status_code, 202, accepted.text)
                assert github_api.check_run is not None
                self.assertEqual(github_api.check_run["conclusion"], "success")

                missing_reason = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "revoked",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "revoke-missing-reason"},
                )
                self.assertEqual(missing_reason.status_code, 422, missing_reason.text)

                revoked = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "revoked",
                        "expected_binding_sha256": binding_sha256,
                        "reason": "Owner withdrew the product review.",
                    },
                    headers={"Idempotency-Key": "revoke-accepted"},
                )
                self.assertEqual(revoked.status_code, 202, revoked.text)
                self.assertEqual(revoked.json()["decision"]["status"], "revoked")
                assert github_api.check_run is not None
                self.assertEqual(github_api.check_run["conclusion"], "action_required")

    async def test_changes_requested_resolution_requires_structured_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(app) as client:
                evaluated = await client.get(OWNER_ACCEPTANCE_EVALUATION_ROUTE, params=target)
                binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                changes_requested = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "changes_requested",
                        "expected_binding_sha256": binding_sha256,
                        "reason": "Clarify the product behavior.",
                    },
                    headers={"Idempotency-Key": "request-changes"},
                )
                self.assertEqual(changes_requested.status_code, 202, changes_requested.text)

                missing_resolution = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                    },
                    headers={"Idempotency-Key": "resolve-without-evidence"},
                )
                self.assertEqual(missing_resolution.status_code, 409, missing_resolution.text)
                self.assertEqual(
                    missing_resolution.json()["detail"]["code"],
                    "owner_acceptance_transition_invalid",
                )

                resolved = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": binding_sha256,
                        "resolution": {
                            "summary": "The requested behavior is implemented and verified.",
                            "resolved_evidence_references": [
                                "test:owner-flow",
                                "record:product-spec-17",
                            ],
                        },
                    },
                    headers={"Idempotency-Key": "resolve-with-evidence"},
                )
                self.assertEqual(resolved.status_code, 202, resolved.text)
                self.assertEqual(resolved.json()["decision"]["status"], "accepted")
                self.assertEqual(resolved.json()["record"]["subject_sequence"], 2)

    async def test_event_route_rejects_non_human_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(
                store=store,
                browser_identity=TerminalAgentIdentity(subject="agent", token_label="local"),
            )
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                expected_binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
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
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params={"repository": REPOSITORY, "pull_request_number": 2022},
                )
                expected_binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
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
                        "expected_binding_sha256": "0" * 64,
                    },
                )

            self.assertEqual(response.status_code, 422, response.text)

    async def test_event_route_requires_reviewed_binding_digest(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
            async with lifespan_client(app) as client:
                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": {"repository": REPOSITORY, "pull_request_number": 2022},
                        "action": "accepted",
                    },
                    headers={"Idempotency-Key": "accept-missing-binding"},
                )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    async def test_event_route_rejects_binding_changed_after_evaluation(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params=target,
                )
                expected_binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                provider.evidence = _repository_evidence(head="c" * 40)

                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
                    },
                    headers={"Idempotency-Key": "accept-stale-binding"},
                )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "owner_acceptance_binding_changed")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    async def test_event_route_rejects_serving_preview_drift(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            _write_preview_evidence(store)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(store=store, repository_evidence_provider=provider)
            target: dict[str, str | int] = {
                "repository": REPOSITORY,
                "pull_request_number": 2022,
            }
            async with lifespan_client(app) as client:
                evaluated = await client.get(
                    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                    params=target,
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                preview = evaluated.json()["decision"]["binding"]["preview"]
                self.assertEqual(
                    preview["serving_generation_id"],
                    "preview-generic-web-a-pr-2022-generation-0001",
                )
                expected_binding_sha256 = evaluated.json()["decision"]["binding"]["binding_sha256"]
                _write_preview_evidence(
                    store,
                    generation_id="preview-generic-web-a-pr-2022-generation-0002",
                    artifact_id="artifact-generic-web-a-pr-2022-v2",
                    image_digest="b" * 64,
                )

                response = await client.post(
                    OWNER_ACCEPTANCE_EVENTS_ROUTE,
                    json={
                        "target": target,
                        "action": "accepted",
                        "expected_binding_sha256": expected_binding_sha256,
                    },
                    headers={"Idempotency-Key": "accept-preview-drift"},
                )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "owner_acceptance_binding_changed")
            self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_routes_are_bounded(self) -> None:
        self.assertNotIn(OWNER_ACCEPTANCE_EVALUATION_ROUTE, _BOUNDED_REQUEST_BODY_CONTRACTS)
        self.assertEqual(
            _BOUNDED_REQUEST_BODY_CONTRACTS[OWNER_ACCEPTANCE_EVENTS_ROUTE][1],
            16 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
