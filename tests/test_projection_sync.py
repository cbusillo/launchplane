from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch
from typing import cast
import unittest
import click

from control_plane import service_auth
from control_plane.action_effects import ACTION_EFFECTS
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.service_auth import (
    AuthorizationTarget,
    BearerIdentityConfig,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    apply_npmplus_ingress_route,
)
from tests.http_app_test_support import _RejectingVerifier
from tests.support.http import lifespan_client
from tests.support.ingress import (
    _FakeNpmplusIngressClient,
    _edge_endpoint_record,
    _npmplus_proxy_host,
)
from tests.test_action_effects import reader_policy
from tests.test_authz_administration_read import _database_app
from tests.test_http_app_ingress import (
    _instance_ingress_exact_host_payload,
    _instance_ingress_product_profile,
)


class _ReadOnlyIngressClient(_FakeNpmplusIngressClient):
    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
        raise AssertionError("Projection sync attempted provider create")

    def update_proxy_host(
        self, *, host_id: int, payload: NpmplusProxyHostPayload
    ) -> NpmplusProxyHost:
        raise AssertionError("Projection sync attempted provider update")

    def enable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        raise AssertionError("Projection sync attempted provider enable")

    def disable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        raise AssertionError("Projection sync attempted provider disable")


class _RecordedEvaluations(dict[object, service_auth.AuthzEvaluation]):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[service_auth.AuthzEvaluation] = []

    def __setitem__(self, key: object, value: service_auth.AuthzEvaluation) -> None:
        self.events.append(value)
        super().__setitem__(key, value)


class ProjectionSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_projection_sync_action_has_an_executed_invariant_proof(self) -> None:
        proofs = {"ingress_route.audit": self._prove_ingress_audit}
        self.assertEqual(
            {action for action, effect in ACTION_EFFECTS.items() if effect == "projection_sync"},
            proofs.keys(),
            "A projection action needs a provider-write and authority invariant proof",
        )
        for action, proof in proofs.items():
            with self.subTest(action=action):
                await proof()

    async def _prove_ingress_audit(self) -> None:
        profile = _instance_ingress_product_profile()
        endpoint = _edge_endpoint_record()
        cases: tuple[tuple[str, dict[str, object], dict[str, object], int], ...] = (
            ("matching", {}, {}, 202),
            ("missing", {}, {}, 400),
            ("upstream-drift", {"forward_port": 8443}, {}, 409),
            ("disabled", {"enabled": False}, {}, 409),
            (
                "shared-host",
                {"domain_names": ("ingress-canary.example.test", "sibling.example.test")},
                {},
                202,
            ),
            ("drift-after-preflight", {}, {}, 409),
            ("create", {}, {"allow_create": True}, 400),
            ("no-expected-host", {}, {"expected_host_id": None}, 400),
            ("inexact-domains", {}, {"require_exact_expected_host_domains": False}, 400),
            ("context-only", {}, {}, 403),
            ("foreign-domain", {}, {}, 400),
            ("foreign-lane", {}, {}, 400),
            ("missing-edge", {}, {}, 400),
            ("shared-context", {}, {}, 400),
        )
        for name, host_updates, request_updates, expected in cases:
            with (
                self.subTest(case=name),
                _database_app(LaunchplaneAuthzPolicy(schema_version=2), director_agent=True) as (
                    store,
                    policy_record,
                    _,
                ),
            ):
                host = _npmplus_proxy_host(
                    advanced_config="private-provider-config",
                    meta={"private": "provider-meta"},
                    **host_updates,
                )

                class Client(_ReadOnlyIngressClient):
                    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]:
                        if name == "drift-after-preflight" and self.calls:
                            self.proxy_hosts[0] = self.proxy_hosts[0].model_copy(
                                update={"forward_port": 8443}
                            )
                        return super().list_proxy_hosts()

                ingress = Client(() if name == "missing" else (host,))
                store.write_product_profile_record(profile)
                if name == "shared-context":
                    store.write_product_profile_record(
                        profile.model_copy(update={"product": "other-product"})
                    )
                store.write_edge_endpoint_record(endpoint)
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=policy_record.policy,
                    record_store_factory=lambda: store,
                    bearer_identity_config=BearerIdentityConfig(
                        local_operator_token="director-token",
                        local_operator_subject="director",
                        local_operator_token_label="director-label",
                    ),
                    npmplus_ingress_client_factory=lambda: ingress,
                )
                payload = _instance_ingress_exact_host_payload(mode="apply")
                request_payload = dict(cast(dict[str, object], payload["ingress"]))
                request_payload.update(request_updates)
                route = dict(cast(dict[str, object], request_payload["route"]))
                if name == "context-only":
                    payload["instance"] = ""
                elif name == "foreign-domain":
                    route["domain_names"] = ["foreign.example.test"]
                elif name == "foreign-lane":
                    payload["instance"] = "absent"
                elif name == "missing-edge":
                    route["edge_endpoint_key"] = ""
                request_payload["route"] = route
                payload["ingress"] = request_payload
                with ExitStack() as stack:
                    for method in (
                        "compare_and_write_authz_policy_record",
                        "write_route_binding_record",
                        "write_provider_target_record",
                        "write_product_profile_record",
                        "write_edge_endpoint_record",
                    ):
                        stack.enter_context(
                            patch.object(store, method, side_effect=AssertionError(method))
                        )
                    evaluations = _RecordedEvaluations()
                    with patch.object(service_auth, "_AUTHZ_EVALUATION_BY_TASK", evaluations):
                        async with lifespan_client(app) as client:
                            response = await client.post(
                                "/v1/drivers/ingress/route-apply",
                                headers={
                                    "Authorization": "Bearer director-token",
                                    "Idempotency-Key": f"audit-{name}",
                                },
                                json=payload,
                            )
                self.assertEqual(response.status_code, expected, response.text)
                self.assertFalse(set(ingress.calls) - {"list"})
                self.assertNotIn("private-provider-config", response.text)
                self.assertNotIn("provider-meta", response.text)
                if name == "shared-host":
                    sibling_domains = set(host.domain_names) - set(
                        cast(list[str], route["domain_names"])
                    )
                    self.assertTrue(sibling_domains)
                    for domain in sibling_domains:
                        self.assertNotIn(domain, response.text)
                self.assertEqual(store.read_product_profile_record(profile.product), profile)
                self.assertEqual(store.read_edge_endpoint_record(endpoint.endpoint_key), endpoint)
                records = store.list_ingress_route_audit_records(
                    product=profile.product, context_name=profile.lanes[0].context
                )
                if expected == 202:
                    self.assertFalse(
                        any(event.action == "ingress_route.apply" for event in evaluations.events),
                        "An audit must not record a denied provider-apply probe",
                    )
                    self.assertTrue(records)
                    self.assertEqual(response.json()["result"]["status"], "unchanged")
                    self.assertEqual(records[0].status, "unchanged")
                    self.assertEqual(records[0].provider_host_id, host.id)
                    self.assertEqual(records[0].edge_endpoint_key, endpoint.endpoint_key)
                    self.assertEqual(
                        records[0].requested_domains, tuple(cast(list[str], route["domain_names"]))
                    )
                    self.assertTrue(all(not op.requires_apply for op in records[0].operations))
                    self.assertEqual(ingress.proxy_hosts, [host])
                elif name == "drift-after-preflight":
                    self.assertEqual(
                        response.json()["error"]["code"], "instance_scoped_ingress_changed"
                    )
                    self.assertTrue(all(record.status == "pending" for record in records))
                else:
                    self.assertFalse(records)

    def test_mutations_are_unreachable_with_all_provider_write_flags_false(self) -> None:
        matching_host = _npmplus_proxy_host()
        assert matching_host.forward_port is not None
        payload = _instance_ingress_exact_host_payload(mode="apply")
        request = NpmplusIngressApplyRequest.model_validate(payload["ingress"]).model_copy(
            update={"allow_create": False, "allow_update": False, "allow_enable_disable": False}
        )
        request = request.model_copy(
            update={
                "route": request.route.model_copy(
                    update={
                        "forward_host": matching_host.forward_host,
                        "forward_port": matching_host.forward_port,
                    }
                )
            }
        )
        for hosts in (
            (),
            (matching_host,),
            (matching_host.model_copy(update={"forward_port": matching_host.forward_port + 1}),),
            (_npmplus_proxy_host(enabled=False),),
        ):
            ingress = _ReadOnlyIngressClient(hosts)
            with self.subTest(hosts=hosts):
                if hosts == (matching_host,):
                    result = apply_npmplus_ingress_route(client=ingress, request=request)
                    self.assertEqual(result.status, "unchanged")
                else:
                    with self.assertRaises(click.ClickException):
                        apply_npmplus_ingress_route(client=ingress, request=request)
                self.assertEqual(ingress.calls, ["list"])

    async def test_unauthorized_apply_denial_keeps_requested_action(self) -> None:
        policy = reader_policy(product="launchplane")
        policy = policy.model_copy(
            update={
                "local_operators": tuple(
                    rule.model_copy(update={"actions": ("ingress_route.plan",)})
                    for rule in policy.local_operators
                )
            }
        )
        with _database_app(policy) as (store, _, app):
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/drivers/ingress/route-apply",
                    headers={
                        "Authorization": "Bearer reader-token",
                        "Idempotency-Key": "denied-ingress-apply",
                    },
                    json=_instance_ingress_exact_host_payload(mode="apply"),
                )
            self.assertEqual(response.status_code, 403, response.text)
            payload = response.json()
            self.assertEqual(payload["error"]["code"], "authorization_denied")
            denial = store.read_authz_denial_record(
                trace_id=payload["trace_id"], observed_at=datetime.now(timezone.utc).isoformat()
            )
            self.assertIsNotNone(denial)
            assert denial is not None
            self.assertEqual(denial.action, "ingress_route.apply")

    def test_projection_permission_does_not_broaden_readers_or_other_writes(self) -> None:
        policy = reader_policy(product="launchplane")
        for identity in (
            LocalOperatorIdentity("record-reader", "record-reader-label"),
            TerminalAgentIdentity("record-reader", "record-reader-label"),
            GitHubHumanIdentity(
                github_id=101,
                login="reader",
                name="Reader",
                email="",
                organizations=frozenset(),
                teams=frozenset(),
                role="read_only",
            ),
        ):
            with self.subTest(identity=identity):
                self.assertFalse(
                    policy.allows(
                        identity=identity,
                        action="ingress_route.audit",
                        product="launchplane",
                        context="reon-prod",
                        target=AuthorizationTarget(scope="instance", instances=("testing",)),
                    )
                )
