from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.change_impact_github import ChangeImpactRepositoryEvidenceError
from control_plane.contracts.merge_train_policy import MERGE_TRAIN_POLICY_TARGETS_READ_ACTION
from control_plane.contracts.governance_projection import GovernanceMergeReadinessFacet
from control_plane.http_routes.governance_projection import (
    GOVERNANCE_PROJECTION_ROUTE,
    GovernanceProjectionRouteDependencies,
    register_governance_projection_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from tests.support.http import lifespan_client
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_owner_acceptance import _EvidenceProvider, _human, _repository_evidence, _store


def _http_error(**kwargs: object) -> HTTPException:
    return HTTPException(status_code=int(str(kwargs["status_code"])), detail=kwargs)


def _readiness(**_: object) -> GovernanceMergeReadinessFacet:
    return GovernanceMergeReadinessFacet(
        availability="not_active",
        reason_code="no_active_merge_lineage",
    )


def _configured_store(directory: str) -> object:
    store = _store(Path(directory))
    store.write_merge_train_policy_record(
        build_test_merge_train_policy_record(repository="example/web")
    )
    return store


class GovernanceProjectionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_one_bounded_non_authoritative_projection(self) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **_: True,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        projection = response.json()["projection"]
        self.assertEqual(projection["mode"], "read_only_projection")
        self.assertFalse(projection["authoritative"])
        self.assertEqual(projection["authorizes"], [])
        self.assertEqual(projection["owner_judgment"]["authorizes"], [])
        self.assertEqual(projection["merge_readiness"]["mode"], "ephemeral")
        self.assertEqual(projection["merge_readiness"]["authorizes"], [])
        self.assertEqual(projection["merge_admission"]["status"], "not_recorded")
        self.assertEqual(projection["landing_outcome"]["status"], "not_observed")

    async def test_denies_without_read_authorization(self) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **_: False,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                    },
                )

        self.assertEqual(response.status_code, 403, response.text)

    async def test_denies_without_repository_policy_authorization(self) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **kwargs: (
                    kwargs["action"]
                    not in {"merge_train.run_once", MERGE_TRAIN_POLICY_TARGETS_READ_ACTION}
                ),
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                    },
                )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["code"], "authorization_denied")

    async def test_policy_target_read_authorization_does_not_require_mutation_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **kwargs: kwargs["action"] != "merge_train.run_once",
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)

    async def test_rejects_blank_base_branch_after_normalization(self) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **_: True,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                        "base_branch": " ",
                    },
                )

        self.assertEqual(response.status_code, 400, response.text)

    async def test_returns_503_when_repository_evidence_is_unavailable(self) -> None:
        class _UnavailableProvider:
            def resolve(self, target: object) -> object:
                raise ChangeImpactRepositoryEvidenceError(f"unavailable: {target}")

        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **_: True,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_UnavailableProvider(),  # type: ignore[arg-type]
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                    },
                )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "governance_evidence_unavailable")

    async def test_rejects_requested_base_branch_that_differs_from_pull_request(self) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **_: True,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/web",
                        "pull_request_number": 2022,
                        "base_branch": "release",
                    },
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_request")

    async def test_returns_503_when_merge_train_policy_is_unavailable(self) -> None:
        common = ReadRouteDependencies(
            read_identity=_human,
            get_record_store=object,
            next_trace_id=lambda: "trace-governance",
            authorization_allows=lambda **_: True,
            http_error=_http_error,
            error_response_model=dict,  # type: ignore[arg-type]
        )
        app = FastAPI()
        register_governance_projection_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=GovernanceProjectionRouteDependencies(
                common=common,
                repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                current_readiness_provider=_readiness,
                now=lambda: "2026-08-12T05:00:00Z",
            ),
        )

        async with lifespan_client(app) as client:
            response = await client.get(
                GOVERNANCE_PROJECTION_ROUTE,
                params={
                    "repository": "example/web",
                    "pull_request_number": 2022,
                },
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "governance_evidence_unavailable")

    async def test_returns_400_when_repository_policy_is_not_configured(self) -> None:
        with TemporaryDirectory() as directory:
            store = _configured_store(directory)
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-governance",
                authorization_allows=lambda **_: True,
                http_error=_http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            app = FastAPI()
            register_governance_projection_routes(
                cast(ApiRouteRegistrar, app),
                dependencies=GovernanceProjectionRouteDependencies(
                    common=common,
                    repository_evidence_provider=_EvidenceProvider(_repository_evidence()),
                    current_readiness_provider=_readiness,
                    now=lambda: "2026-08-12T05:00:00Z",
                ),
            )

            async with lifespan_client(app) as client:
                response = await client.get(
                    GOVERNANCE_PROJECTION_ROUTE,
                    params={
                        "repository": "example/missing",
                        "pull_request_number": 2022,
                    },
                )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
