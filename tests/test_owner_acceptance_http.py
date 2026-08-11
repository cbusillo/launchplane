from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.http_app import _BOUNDED_REQUEST_BODY_CONTRACTS
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
)
from control_plane.contracts.product_owner import ProductOwnerGrant, ProductOwnerIdentity
from control_plane.http_routes.owner_acceptance import (
    OWNER_ACCEPTANCE_EVALUATION_ROUTE,
    OWNER_ACCEPTANCE_EVENT_ROUTE,
    OWNER_ACCEPTANCE_EVENTS_ROUTE,
    OWNER_ACCEPTANCE_PROJECT_ROUTE,
    OwnerAcceptanceRouteDependencies,
    register_owner_acceptance_routes,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from tests.support.http import lifespan_client
from tests.test_owner_acceptance import (
    PRODUCT,
    REPOSITORY,
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


def _app(
    *,
    store: object,
    identity: LaunchplaneIdentity | None = None,
    browser_identity: LaunchplaneIdentity | None = None,
    repository_evidence_provider: _EvidenceProvider | None = None,
    authorization_allows: Callable[..., bool] | None = None,
    github_app_token: Callable[[str, str], GitHubAppInstallationToken] | None = None,
    github_api: Callable[..., object] | None = None,
) -> FastAPI:
    resolved_identity = identity or _human()
    resolved_browser_identity = browser_identity or resolved_identity
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
            github_app_token=github_app_token,
            **({"github_api": github_api} if github_api is not None else {}),
        ),
    )
    return app


class OwnerAcceptanceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_projects_current_owner_decision_as_neutral_github_app_check(self) -> None:
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
                        "conclusion": body["conclusion"],
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
        self.assertEqual(payload["result"]["conclusion"], "neutral")
        self.assertEqual(calls[-2]["body"]["conclusion"], "neutral")
        self.assertEqual(calls[-1]["method"], "DELETE")

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
                app = _app(store=store)
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

                    evaluated = await client.get(
                        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
                        params=target,
                    )
                    self.assertEqual(evaluated.status_code, 200, evaluated.text)
                    self.assertEqual(evaluated.json()["decision"]["status"], expected_status)

        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            app = _app(store=store)
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
