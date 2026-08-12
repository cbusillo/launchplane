from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.contracts.governance_projection import GovernanceMergeReadinessFacet
from control_plane.http_routes.governance_projection import (
    GOVERNANCE_PROJECTION_ROUTE,
    GovernanceProjectionRouteDependencies,
    register_governance_projection_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from tests.support.http import lifespan_client
from tests.test_owner_acceptance import _EvidenceProvider, _human, _repository_evidence, _store


def _http_error(**kwargs: object) -> HTTPException:
    return HTTPException(status_code=int(str(kwargs["status_code"])), detail=kwargs)


def _readiness(**_: object) -> GovernanceMergeReadinessFacet:
    return GovernanceMergeReadinessFacet(
        availability="not_active",
        reason_code="no_active_merge_lineage",
    )


class GovernanceProjectionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_one_bounded_non_authoritative_projection(self) -> None:
        with TemporaryDirectory() as directory:
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: _store(Path(directory)),
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
        self.assertFalse(projection["merge_admission"]["admitted"])
        self.assertEqual(projection["landing_outcome"]["status"], "not_observed")

    async def test_denies_without_read_authorization(self) -> None:
        with TemporaryDirectory() as directory:
            common = ReadRouteDependencies(
                read_identity=_human,
                get_record_store=lambda: _store(Path(directory)),
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


if __name__ == "__main__":
    unittest.main()
