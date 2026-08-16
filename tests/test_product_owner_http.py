from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.contracts.product_owner import (
    ProductOwnerGrant,
    ProductOwnerIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerRequirement,
    ProductOwnerRequirementRecord,
    ProductOwnerRoutingRecord,
)
from control_plane.http_routes.product_owner import (
    PRODUCT_OWNER_POLICY_APPLY_ROUTE,
    PRODUCT_OWNER_POLICY_READ_ROUTE,
    PRODUCT_OWNER_REQUIREMENT_APPLY_ROUTE,
    PRODUCT_OWNER_ROUTING_APPLY_ROUTE,
    PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE,
    ProductOwnerWriteRouteDependencies,
    register_product_owner_read_routes,
    register_product_owner_write_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import GitHubHumanIdentity, action_safety
from control_plane.service_auth import (
    GitHubActionsIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.http import lifespan_client


PRODUCT = "product-alpha"
SYSTEM = "system-web"
REPOSITORY_ID = "101"
ENVIRONMENT = "prod"
ACTION = "production.authorize"


def _human(github_id: int, *, role: str = "read_only") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=f"human-{github_id}",
        github_id=github_id,
        name="Test Human",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role=role,  # type: ignore[arg-type]
    )


def _policy(
    *,
    revision: int = 1,
    subjects: tuple[str, ...] = ("1001", "1002"),
    supersedes_record_id: str | None = None,
) -> ProductOwnerPolicyRecord:
    return ProductOwnerPolicyRecord(
        product=PRODUCT,
        system=SYSTEM,
        policy_revision=revision,
        owners=tuple(
            ProductOwnerGrant(
                identity=ProductOwnerIdentity(provider="github", provider_subject_id=subject),
                repository_ids=(REPOSITORY_ID,),
                environments=(ENVIRONMENT,),
            )
            for subject in subjects
        ),
        effective_at="2026-08-05T00:00:00Z",
        source="test",
        reason="Create two current Owners with quorum one.",
        supersedes_record_id=supersedes_record_id,
    )


def _requirement() -> ProductOwnerRequirementRecord:
    return ProductOwnerRequirementRecord(
        product=PRODUCT,
        system=SYSTEM,
        requirement_revision=1,
        requirements=(
            ProductOwnerRequirement(
                action=ACTION,
                repository_ids=(REPOSITORY_ID,),
                environments=(ENVIRONMENT,),
            ),
        ),
        effective_at="2026-08-05T00:00:00Z",
        source="test",
        reason="Require an Owner only for the explicit production action.",
    )


class ProductOwnerHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_read_and_authority_routes_share_current_records(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            identity_holder: list[LaunchplaneIdentity] = [_human(1001)]

            def identity() -> LaunchplaneIdentity:
                return identity_holder[0]

            def http_error(
                *,
                status_code: int,
                trace_id: str,
                code: str,
                message: str,
                authz: dict[str, object] | None = None,
            ) -> HTTPException:
                return HTTPException(
                    status_code=status_code,
                    detail={"trace_id": trace_id, "code": code, "message": message},
                )

            app = FastAPI()
            read_dependencies = ReadRouteDependencies(
                read_identity=identity,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-owner-read",
                authorization_allows=lambda **_: True,
                http_error=http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            write_dependencies = ProductOwnerWriteRouteDependencies(
                read_write_identity=identity,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace-owner-write",
                authorization_allows=lambda **_: True,
                http_error=http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            registrar = cast(ApiRouteRegistrar, app)
            register_product_owner_read_routes(registrar, dependencies=read_dependencies)
            register_product_owner_write_routes(registrar, dependencies=write_dependencies)

            policy = _policy()
            requirement = _requirement()
            routing = ProductOwnerRoutingRecord(
                product=PRODUCT,
                system=SYSTEM,
                routing_revision=1,
                preferred_owner_identity_ids=(
                    ProductOwnerIdentity(
                        provider="github",
                        provider_subject_id="1001",
                    ).identity_id,
                ),
                effective_at="2026-08-05T00:00:00Z",
                source="test",
                reason="Prefer one Owner without changing authority.",
            )

            async with lifespan_client(app) as client:
                for path, record in (
                    (PRODUCT_OWNER_POLICY_APPLY_ROUTE, policy),
                    (PRODUCT_OWNER_REQUIREMENT_APPLY_ROUTE, requirement),
                    (PRODUCT_OWNER_ROUTING_APPLY_ROUTE, routing),
                ):
                    response = await client.post(
                        path,
                        json={"schema_version": 1, "mode": "apply", "record": record.model_dump()},
                    )
                    self.assertEqual(response.status_code, 202, response.text)

                invalid_owner_payload = policy.model_dump()
                invalid_owner_payload["owners"][0]["identity"]["provider"] = "github-actions"
                invalid_owner = await client.post(
                    PRODUCT_OWNER_POLICY_APPLY_ROUTE,
                    json={
                        "schema_version": 1,
                        "mode": "apply",
                        "record": invalid_owner_payload,
                    },
                )
                self.assertEqual(invalid_owner.status_code, 422, invalid_owner.text)

                read_response = await client.get(
                    PRODUCT_OWNER_POLICY_READ_ROUTE,
                    params={"product": PRODUCT, "system": SYSTEM},
                )
                self.assertEqual(read_response.status_code, 200, read_response.text)
                read_model = read_response.json()["read_model"]
                self.assertNotIn("mode", read_model)
                self.assertNotIn("authoritative", read_model)
                self.assertNotIn("enforcement_effect", read_model)

                evaluation_params: dict[str, str | int] = {
                    "product": PRODUCT,
                    "system": SYSTEM,
                    "repository_id": REPOSITORY_ID,
                    "environment": ENVIRONMENT,
                    "action": ACTION,
                    "claimed_policy_revision": policy.policy_revision,
                    "claimed_policy_digest": policy.policy_digest,
                    "claimed_requirement_revision": requirement.requirement_revision,
                    "claimed_requirement_digest": requirement.requirement_digest,
                }
                preferred = await client.get(
                    PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE,
                    params=evaluation_params,
                )
                self.assertEqual(preferred.status_code, 200, preferred.text)
                preferred_evaluation = preferred.json()["evaluation"]
                self.assertEqual(preferred_evaluation["decision"], "authorized")
                self.assertTrue(preferred_evaluation["actor_is_preferred"])
                self.assertNotIn("authoritative", preferred_evaluation)

                identity_holder[0] = _human(1002)
                non_preferred = await client.get(
                    PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE,
                    params=evaluation_params,
                )
                self.assertEqual(non_preferred.status_code, 200, non_preferred.text)
                self.assertEqual(non_preferred.json()["evaluation"]["decision"], "authorized")
                self.assertFalse(non_preferred.json()["evaluation"]["actor_is_preferred"])

                identity_holder[0] = _human(9999, role="admin")
                admin = await client.get(
                    PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE,
                    params=evaluation_params,
                )
                self.assertEqual(admin.status_code, 200, admin.text)
                self.assertEqual(
                    admin.json()["evaluation"]["reason_code"],
                    "actor_not_current_owner",
                )

                non_human_identities: tuple[LaunchplaneIdentity, ...] = (
                    GitHubActionsIdentity(
                        repository="owner/repository",
                        repository_owner="owner",
                        workflow_ref="owner/repository/.github/workflows/test.yml@refs/heads/main",
                        job_workflow_ref="",
                        ref="refs/heads/main",
                        ref_type="branch",
                        event_name="workflow_dispatch",
                        environment="prod",
                        subject="repo:owner/repository:ref:refs/heads/main",
                        sha="a" * 40,
                        raw_claims={},
                        repository_id=REPOSITORY_ID,
                    ),
                    TerminalAgentIdentity(subject="1001", token_label="terminal"),
                    LocalOperatorIdentity(subject="1001", token_label="operator"),
                    LocalAdminIdentity(subject="1001", token_label="admin"),
                )
                for non_human_identity in non_human_identities:
                    with self.subTest(identity=type(non_human_identity).__name__):
                        identity_holder[0] = non_human_identity
                        rejected = await client.get(
                            PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE,
                            params=evaluation_params,
                        )
                        self.assertEqual(rejected.status_code, 403, rejected.text)
                        self.assertEqual(
                            rejected.json()["detail"]["code"],
                            "product_owner_actor_identity_required",
                        )

                identity_holder[0] = _human(1001, role="admin")
                listed_admin = await client.get(
                    PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE,
                    params=evaluation_params,
                )
                self.assertEqual(listed_admin.status_code, 200, listed_admin.text)
                self.assertEqual(listed_admin.json()["evaluation"]["decision"], "authorized")

                conflicting = await client.post(
                    PRODUCT_OWNER_POLICY_APPLY_ROUTE,
                    json={
                        "schema_version": 1,
                        "mode": "apply",
                        "record": _policy(subjects=("1001",)).model_dump(),
                    },
                )
                self.assertEqual(conflicting.status_code, 409, conflicting.text)
                self.assertEqual(
                    conflicting.json()["detail"]["code"],
                    "product_owner_revision_conflict",
                )

                non_linear = _policy(
                    revision=3,
                    subjects=("1001",),
                    supersedes_record_id=policy.record_id,
                )
                sequence_error = await client.post(
                    PRODUCT_OWNER_POLICY_APPLY_ROUTE,
                    json={
                        "schema_version": 1,
                        "mode": "apply",
                        "expected_current_record_id": policy.record_id,
                        "expected_current_policy_digest": policy.policy_digest,
                        "record": non_linear.model_dump(),
                    },
                )
                self.assertEqual(sequence_error.status_code, 409, sequence_error.text)
                self.assertEqual(
                    sequence_error.json()["detail"]["code"],
                    "product_owner_revision_sequence_error",
                )

            self.assertEqual(len(store.list_product_owner_policy_records()), 1)
            self.assertEqual(len(store.list_product_owner_requirement_records()), 1)
            self.assertEqual(len(store.list_product_owner_routing_records()), 1)

    def test_openapi_and_policy_admin_action_safety(self) -> None:
        app = FastAPI()

        def identity() -> GitHubHumanIdentity:
            return _human(1001)

        def http_error(**kwargs: object) -> HTTPException:
            return HTTPException(
                status_code=int(str(kwargs["status_code"])),
                detail=kwargs,
            )

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            common = ReadRouteDependencies(
                read_identity=identity,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace",
                authorization_allows=lambda **_: True,
                http_error=http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            writes = ProductOwnerWriteRouteDependencies(
                read_write_identity=identity,
                get_record_store=lambda: store,
                next_trace_id=lambda: "trace",
                authorization_allows=lambda **_: True,
                http_error=http_error,
                error_response_model=dict,  # type: ignore[arg-type]
            )
            registrar = cast(ApiRouteRegistrar, app)
            register_product_owner_read_routes(registrar, dependencies=common)
            register_product_owner_write_routes(registrar, dependencies=writes)
            schema = app.openapi()
        self.assertIn(PRODUCT_OWNER_POLICY_READ_ROUTE, schema["paths"])
        self.assertIn(PRODUCT_OWNER_POLICY_APPLY_ROUTE, schema["paths"])
        self.assertIn(PRODUCT_OWNER_AUTHORITY_EVALUATION_ROUTE, schema["paths"])
        self.assertEqual(action_safety("product_owner_policy.write"), "policy_admin")
        self.assertEqual(action_safety("product_owner_requirement.write"), "policy_admin")
        self.assertEqual(action_safety("product_owner_routing.write"), "policy_admin")
        self.assertEqual(action_safety("product_owner_policy.read"), "read")
        self.assertEqual(action_safety("product_owner_requirement.read"), "read")
        self.assertEqual(action_safety("product_owner_routing.read"), "read")


if __name__ == "__main__":
    unittest.main()
