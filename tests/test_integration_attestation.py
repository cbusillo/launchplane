from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.integration_attestation import (
    INTEGRATION_ATTESTATION_ROUTE,
    IntegrationAttestationEvidence,
    IntegrationModelReview,
)
from control_plane.http_app import _launchplane_http_error, LaunchplaneErrorResponse
from control_plane.http_routes.integration_attestation import register_integration_attestation_route
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LocalOperatorIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import request
from tests.support.stores import sqlite_database_url
from tests.test_product_review import _profile, _decision


class IntegrationAttestationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_binding_authority_record_only_apply_and_historical_readback(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary.name) / "state.db")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        profile = _profile()
        store.write_product_profile_record(profile)
        evidence = IntegrationAttestationEvidence(
            product=profile.product,
            repository=profile.repository,
            pull_request_number=42,
            base_branch="main",
            owner_github_id=profile.owner.github_id,
            source_decision_id="source-acceptance",
            merge_sha="a" * 40,
            tree_sha="b" * 40,
            parent_shas=("c" * 40, "d" * 40),
            integrated_changes=(),
        )
        accepted = _decision(
            record_id=evidence.source_decision_id, decided_at="2026-10-27T00:00:00Z"
        ).model_copy(
            update={"head_sha": evidence.parent_shas[0], "base_branch": evidence.base_branch}
        )
        store.write_product_review_decision_record(accepted)
        actor = LocalOperatorIdentity("director", "bootstrap", director_agent=True)
        policy = LaunchplaneAuthzPolicy(schema_version=3)
        app = FastAPI()
        register_integration_attestation_route(
            cast(ApiRouteRegistrar, app),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: actor,
                get_record_store=lambda: store,
                next_trace_id=lambda: "fixture-trace",
                authorization_allows=policy.allows,
                http_error=_launchplane_http_error,
                error_response_model=LaunchplaneErrorResponse,
            ),
            control_plane_root=Path(temporary.name),
        )
        payload = {
            "product": profile.product,
            "repository": profile.repository,
            "pull_request_number": 42,
            "expected_head_sha": evidence.merge_sha,
            "merge_sha": evidence.merge_sha,
            "source_decision_id": evidence.source_decision_id,
        }

        @contextmanager
        def credential(**kwargs: object) -> Iterator[str]:
            yield "fixture-token"

        with (
            patch(
                "control_plane.http_routes.integration_attestation.launchplane_github_token",
                credential,
            ),
            patch(
                "control_plane.http_routes.integration_attestation.plan_integration_attestation",
                return_value=evidence,
            ) as plan,
        ):
            dry_run = await request(app, "POST", INTEGRATION_ATTESTATION_ROUTE, payload=payload)
            self.assertEqual(dry_run.status_code, 200, dry_run.text)
            self.assertEqual(
                store.list_integration_attestation_records(
                    repository=profile.repository, pull_request_number=42
                ),
                (),
            )
            apply = payload | {
                "mode": "apply",
                "reviewed_evidence_sha256": evidence.digest(),
                "statement": "The exact merge adds no Client-visible work.",
                "review": IntegrationModelReview(
                    evidence_sha256=evidence.digest(),
                    author_provider="openai",
                    reviewer_provider="anthropic",
                    reviewer_model="fixture-model",
                    conclusion="no_new_client_visible_work",
                    report="Reviewed the immutable merge and accepted parents.",
                ).model_dump(),
            }
            for invalid in (
                apply | {"review": None},
                apply | {"reviewed_evidence_sha256": "0" * 64},
                apply | {"statement": ""},
            ):
                refused = await request(app, "POST", INTEGRATION_ATTESTATION_ROUTE, payload=invalid)
                self.assertEqual(refused.status_code, 409, refused.text)
            actor = LocalOperatorIdentity("ordinary", "other")
            denied = await request(app, "POST", INTEGRATION_ATTESTATION_ROUTE, payload=apply)
            self.assertEqual(denied.status_code, 403, denied.text)
            actor = LocalOperatorIdentity("director", "bootstrap", director_agent=True)
            result = await request(app, "POST", INTEGRATION_ATTESTATION_ROUTE, payload=apply)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(
                store.list_product_review_decision_records(
                    repository=profile.repository, pull_request_number=42
                ),
                (accepted,),
            )
            plan.reset_mock()
            replay = await request(app, "POST", INTEGRATION_ATTESTATION_ROUTE, payload=apply)
            self.assertEqual(replay.json()["record"], result.json()["record"])
            readback = await request(
                app, "POST", INTEGRATION_ATTESTATION_ROUTE, payload=apply | {"mode": "readback"}
            )
            self.assertEqual(readback.json()["record"], result.json()["record"])
            plan.assert_not_called()
            replaced = await request(
                app,
                "POST",
                INTEGRATION_ATTESTATION_ROUTE,
                payload=apply | {"statement": "Different"},
            )
            self.assertEqual(replaced.status_code, 409, replaced.text)
