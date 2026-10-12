from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
import re
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pydantic import ValidationError

from tests.support.ingress import _FakeNpmplusIngressClient, _npmplus_ingress_route_payload
from tests.test_production_backup_authority import _dry_run_envelope
from control_plane.action_effects import ACTION_EFFECTS, AGENT_READ_ROLE_ACTION
from control_plane.authz_scope import exclusively_instance_scoped_authz_actions
from control_plane.contracts.agent_write_intent import _INTENT_AUTHZ_ACTIONS
from control_plane.drivers.registry import list_driver_descriptors
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
    TerminalAgentIdentity,
    TerminalAgentPolicyRule,
)
from tests.support.http import lifespan_client
from control_plane.service_github_delivery_controls import (
    SERVICE_GITHUB_DELIVERY_ROUTE,
    SERVICE_TOKEN_RETIREMENT_ROUTE,
)
from tests.test_service_github_delivery_controls import (
    retirement_request,
    seed_metadata,
)
from tests.test_authz_administration_read import _database_app


def reader_policy(*, product: str = "") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        schema_version=2,
        local_operators=tuple(
            LocalOperatorPolicyRule(
                subjects=("record-reader",),
                token_labels=("record-reader-label",),
                products=(product,) if product else ("sample", "launchplane", "example-product"),
                contexts=("*",),
                actions=(AGENT_READ_ROLE_ACTION,),
                instances=instances,
            )
            for instances in ((), ("*",))
        ),
    )


class ActionEffectTests(unittest.TestCase):
    def test_role_requires_exact_agent_and_product_scope(self) -> None:
        rule = reader_policy(product="sample").local_operators[0].model_dump()
        for field in ("products", "subjects", "token_labels"):
            for invalid in ((), ("*",), ("sample-*",)):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValidationError):
                    LaunchplaneAuthzPolicy.model_validate(
                        {"schema_version": 2, "local_operators": [{**rule, field: invalid}]}
                    )

    def test_every_source_authorization_action_declares_an_effect(self) -> None:
        """Enforce one rule over all action callsites, constants and descriptors."""
        references: dict[str, str] = {}
        root = Path(__file__).resolve().parents[1] / "control_plane"
        paths = tuple(
            path
            for path in root.rglob("*.py")
            if "migrations" not in path.parts and path.name != "action_effects.py"
        )
        positions = {name: {0} for name in ("action_allowed", "allows", "evaluate")}
        for path in paths:
            for definition in ast.walk(ast.parse(path.read_text())):
                if not isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                arguments = [*definition.args.posonlyargs, *definition.args.args]
                bound = bool(arguments and arguments[0].arg in {"self", "cls"})
                for index, argument in enumerate(arguments):
                    if "action" in argument.arg:
                        slots = positions.setdefault(definition.name, set())
                        slots.add(index)
                        if bound and index:
                            slots.add(index - 1)
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text())):
                value = None
                if isinstance(node, ast.keyword) and "action" in (node.arg or ""):
                    value = node.value
                elif isinstance(node, ast.Call) and node.args:
                    name = (
                        node.func.id
                        if isinstance(node.func, ast.Name)
                        else node.func.attr
                        if isinstance(node.func, ast.Attribute)
                        else ""
                    )
                    slots = positions.get(name, set())
                    value = ast.Tuple(
                        elts=[
                            argument for index, argument in enumerate(node.args) if index in slots
                        ],
                        ctx=ast.Load(),
                    )
                elif isinstance(node, ast.Assign | ast.AnnAssign):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if any(
                        isinstance(target, ast.Name)
                        and (
                            ("authz" in target.id.lower() and "action" in target.id.lower())
                            or target.id.lower()
                            in {
                                "action",
                                "required_action",
                                "authorization_action",
                                "controller_action",
                                "binding_action",
                                "onboarding_action",
                            }
                            or target.id.endswith("_ACTION")
                        )
                        for target in targets
                    ):
                        value = node.value
                if value is not None:
                    for literal in ast.walk(value):
                        if (
                            isinstance(literal, ast.Constant)
                            and isinstance(literal.value, str)
                            and re.fullmatch(
                                r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", literal.value
                            )
                        ):
                            references[literal.value] = f"{path.relative_to(root)}:{literal.lineno}"
        for descriptor in list_driver_descriptors():
            for action in descriptor.actions:
                for name in (action.authz_action, *action.alternate_authz_actions):
                    if name:
                        references[name] = descriptor.driver_id
        for name in _INTENT_AUTHZ_ACTIONS.values():
            references[f"{name}.secret"] = "agent secret-backed intent"
        missing = {name: site for name, site in references.items() if name not in ACTION_EFFECTS}
        self.assertFalse(missing, f"Undeclared authorization actions: {missing}")

    def test_gate_rejects_undeclared_actions_in_later_helper_arguments(self) -> None:
        unknown_actions = ("missing_authorize.read", "missing_selection.plan")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "control_plane"
            source.mkdir()
            (source / "runtime_reader.py").write_text(
                "def authorize(identity, action, context): pass\n"
                "def select_runtime(store, identity, context, action): pass\n"
                f"authorize(None, {unknown_actions[0]!r}, 'sample')\n"
                f"select_runtime(None, None, 'sample', {unknown_actions[1]!r})\n"
            )
            with (
                patch("tests.test_action_effects.__file__", str(root / "tests" / "gate.py")),
                self.assertRaisesRegex(
                    AssertionError, "Undeclared authorization actions"
                ) as result,
            ):
                self.test_every_source_authorization_action_declares_an_effect()
            for action in unknown_actions:
                self.assertIn(action, str(result.exception))

    def test_driver_read_effects_agree_with_driver_execution_contract(self) -> None:
        for descriptor in list_driver_descriptors():
            for action in descriptor.actions:
                if ACTION_EFFECTS[action.authz_action] in {"read", "plan"}:
                    with self.subTest(action=action.authz_action):
                        self.assertEqual(action.safety, "read")
                        self.assertFalse(action.writes_records)

    def test_role_covers_all_declared_reads_and_plans_but_no_writes(self) -> None:
        identities = (
            LocalOperatorIdentity("record-reader", "record-reader-label"),
            TerminalAgentIdentity("record-reader", "record-reader-label"),
        )
        for identity in identities:
            policy = reader_policy()
            if isinstance(identity, TerminalAgentIdentity):
                policy = LaunchplaneAuthzPolicy(
                    schema_version=2,
                    terminal_agents=tuple(
                        TerminalAgentPolicyRule.model_validate(
                            {**rule.model_dump(), "contexts": ("sample",)}
                        )
                        for rule in policy.local_operators
                    ),
                )
            for name, effect in ACTION_EFFECTS.items():
                for target in (
                    AuthorizationTarget(scope="context"),
                    AuthorizationTarget(scope="global"),
                    AuthorizationTarget(scope="instance", instances=("testing",)),
                ):
                    with self.subTest(
                        identity=type(identity).__name__, action=name, scope=target.scope
                    ):
                        allowed = policy.allows(
                            identity=identity,
                            action=name,
                            product="sample",
                            context="sample",
                            target=target,
                        )
                        expected = effect in {"read", "plan"} and (
                            target.scope == "instance"
                            or name not in exclusively_instance_scoped_authz_actions()
                        )
                        self.assertEqual(allowed, expected)
            self.assertFalse(
                policy.allows(
                    identity=identity,
                    action="new_undeclared.read",
                    product="sample",
                    context="sample",
                )
            )
            self.assertFalse(
                policy.allows(
                    identity=replace(identity, token_label="other"),
                    action="product_config.plan",
                    product="sample",
                    context="sample",
                )
            )

    def test_existing_grants_scope_and_revocation_remain_authoritative(self) -> None:
        identity = LocalOperatorIdentity("record-reader", "record-reader-label")
        scoped = reader_policy(product="sample")
        self.assertFalse(
            scoped.allows(
                identity=identity,
                action="product_config.plan",
                product="other",
                context="sample",
            )
        )
        for action in ("launchplane_service.read", "product_profile.plan"):
            self.assertFalse(
                scoped.allows(
                    identity=identity,
                    action=action,
                    product="launchplane",
                    context="launchplane",
                    target=AuthorizationTarget(scope="global"),
                )
            )
        self.assertFalse(
            LaunchplaneAuthzPolicy().allows(
                identity=identity,
                action="product_config.plan",
                product="sample",
                context="sample",
            )
        )
        existing = LaunchplaneAuthzPolicy(
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=(identity.subject,),
                    token_labels=(identity.token_label,),
                    actions=("product_config.apply",),
                ),
            )
        )
        self.assertTrue(
            existing.allows(
                identity=identity,
                action="product_config.apply",
                product="sample",
                context="sample",
            )
        )


class StandingReaderHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_dokploy_plan_uses_the_context_owner_and_keeps_apply_authority(self) -> None:
        from tests.test_product_review import _profile

        profile = _profile()
        lane = profile.lanes[0]
        with _database_app(reader_policy(product=profile.product)) as (store, _, app):
            store.write_product_profile_record(profile)
            headers = {"Authorization": "Bearer reader-token"}
            request = {
                "mode": "dry-run",
                "operation": "reconcile-compose-domain",
                "context": lane.context,
                "instance": lane.instance,
                "domains": ["testing.example.invalid"],
                "runtime_port": 3000,
                "reason": "Inspect lane",
            }
            with patch(
                "control_plane.http_app.execute_dokploy_target_setup",
                return_value={"planned": True},
            ) as planner:
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/dokploy-targets/setup", headers=headers, json=request
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    for changes in ({"context": "other-context"}, {"mode": "apply"}):
                        denied = await client.post(
                            "/v1/dokploy-targets/setup",
                            headers=headers,
                            json={**request, **changes},
                        )
                        self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(planner.call_count, 1)
            self.assertEqual(store.read_product_profile_record(profile.product), profile)

    async def test_product_scope_covers_image_plan_and_review_without_cross_product_or_apply(
        self,
    ) -> None:
        from tests.test_product_review import _profile, _decision
        from tests.support.profiles import product_profile_payload
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord

        profile = _profile()
        with _database_app(reader_policy(product=profile.product)) as (store, _, app):
            store.write_product_profile_record(profile)
            other = LaunchplaneProductProfileRecord.model_validate(
                product_profile_payload("other-site")
            )
            store.write_product_profile_record(other)
            decision = _decision(record_id="saved-review", decided_at="2026-10-01T10:00:00Z")
            store.write_product_review_decision_record(decision)
            headers = {"Authorization": "Bearer reader-token"}
            async with lifespan_client(app) as client:
                for product in (profile.product, other.product):
                    result = await client.post(
                        f"/v1/product-profiles/{product}/image-repository",
                        headers=headers,
                        json={
                            "image_repository": f"ghcr.io/{profile.repository if product == profile.product else other.repository}",
                            "reason": "Inspect image destination",
                        },
                    )
                    self.assertEqual(
                        result.status_code, 202 if product == profile.product else 403, result.text
                    )
                result = await client.post(
                    f"/v1/product-profiles/{profile.product}/image-repository",
                    headers=headers,
                    json={
                        "mode": "apply",
                        "image_repository": f"ghcr.io/{profile.repository}",
                        "expected_image_repository": profile.image.repository,
                        "reason": "Apply forbidden",
                    },
                )
                self.assertEqual(result.status_code, 403, result.text)
                for repository in (profile.repository, other.repository):
                    result = await client.get(
                        "/v1/product-review",
                        headers=headers,
                        params={
                            "repository": repository,
                            "pull_request": decision.pull_request_number,
                        },
                    )
                    self.assertEqual(
                        result.status_code,
                        200 if repository == profile.repository else 403,
                        result.text,
                    )
                    if result.status_code == 200:
                        self.assertEqual(
                            result.json()["latest_decision"]["record_id"], decision.record_id
                        )
                        self.assertFalse(result.json()["can_decide"])
                result = await client.get(SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers)
                self.assertEqual(result.status_code, 403, result.text)
                result = await client.get("/v1/product-profiles", headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(
                    [item["product"] for item in result.json()["profiles"]], [profile.product]
                )
            self.assertEqual(store.read_product_profile_record(profile.product), profile)
            self.assertEqual(store.read_product_profile_record(other.product), other)

    async def test_service_selector_and_dry_run_preflight_use_role_without_write_power(
        self,
    ) -> None:
        with _database_app(reader_policy()) as (store, _, app):
            seed_metadata(store)
            headers = {"Authorization": "Bearer reader-token"}
            before = store.list_secret_records()
            with patch.object(
                store, "read_secret_version", side_effect=AssertionError("No values")
            ):
                async with lifespan_client(app) as client:
                    result = await client.get(SERVICE_GITHUB_DELIVERY_ROUTE, headers=headers)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(result.json()["app_id"], "76")
                    denied = await client.post(
                        SERVICE_TOKEN_RETIREMENT_ROUTE,
                        headers=headers,
                        json={
                            **retirement_request().model_dump(mode="json"),
                            "mode": "apply",
                            "director_confirmed": True,
                            "expected_plan_digest": "a" * 64,
                        },
                    )
                    self.assertEqual(denied.status_code, 403, denied.text)
                    for mode, expected in (("dry_run", "allowed"), ("apply", "denied")):
                        result = await client.post(
                            "/v1/agent/write-intents/evaluate",
                            headers=headers,
                            json={
                                "intent": "product_config_apply",
                                "mode": mode,
                                "product": "sample",
                                "context": "sample",
                                "source_url": "https://github.com/example/sample/issues/1",
                                "reason": "Inspect configuration intent",
                            },
                        )
                        self.assertEqual(result.status_code, 202, result.text)
                        self.assertEqual(result.json()["result"]["intent"]["status"], expected)
                        self.assertFalse(result.json()["result"]["intent"]["safe_to_execute"])
                        if mode == "dry_run":
                            self.assertFalse(
                                result.json()["result"]["intent"]["audit"]["subject"][
                                    "approval_capable"
                                ]
                            )
            self.assertEqual(store.list_secret_records(), before)

    async def test_read_role_reaches_redacted_diagnostics_and_privileged_records(self) -> None:
        with _database_app(reader_policy()) as (_, _, app):
            async with lifespan_client(app) as client:
                for path in (
                    "/v1/privileged-operations/plans?descriptor_id=managed-authz-policy-set",
                    "/v1/authz-policies/active",
                    "/v1/authz-policies/administration",
                    "/v1/authz-diagnostics/active-policy/health",
                ):
                    result = await client.get(
                        path, headers={"Authorization": "Bearer reader-token"}
                    )
                    self.assertEqual(result.status_code, 200, f"{path}: {result.text}")
                    self.assertNotIn("record-reader-label", result.text)

    async def test_ingress_and_backup_planners_accept_role_but_refuse_apply(self) -> None:
        with _database_app(reader_policy()) as (store, _, app):
            headers = {"Authorization": "Bearer reader-token"}
            from control_plane.http_app import create_launchplane_fastapi_app
            from control_plane.service_auth import BearerIdentityConfig
            from tests.http_app_test_support import _RejectingVerifier
            from tests.support.profiles import product_profile_payload
            from control_plane.contracts.product_profile_record import (
                LaunchplaneProductProfileRecord,
            )
            from tests.support.ingress import _npmplus_proxy_host

            profile = LaunchplaneProductProfileRecord.model_validate(
                product_profile_payload("sample")
            )
            lane = profile.lanes[0].model_copy(
                update={
                    "base_url": "https://ingress-canary.example.test",
                    "health_url": "https://ingress-canary.example.test/health",
                }
            )
            profile = profile.model_copy(update={"lanes": (lane,)})
            store.write_product_profile_record(profile)
            ingress = _FakeNpmplusIngressClient(
                (
                    _npmplus_proxy_host(
                        advanced_config="proxy_set_header Authorization secret-value;",
                        meta={"password": "secret-value"},
                    ),
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=reader_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="reader-token",
                    local_operator_subject="record-reader",
                    local_operator_token_label="record-reader-label",
                ),
                npmplus_ingress_client_factory=lambda: ingress,
            )
            async with lifespan_client(app) as client:
                for mode, expected in (("dry-run", 202), ("apply", 403)):
                    response = await client.post(
                        "/v1/drivers/ingress/route-apply",
                        headers=headers,
                        json={
                            **_npmplus_ingress_route_payload(mode=mode, context=lane.context),
                            "product": profile.product,
                        },
                    )
                    self.assertEqual(response.status_code, expected, response.text)
                    self.assertNotIn("secret-value", response.text)
                denied = await client.post(
                    "/v1/drivers/ingress/route-apply",
                    headers=headers,
                    json={
                        **_npmplus_ingress_route_payload(
                            context=lane.context, domain_names=["other-product.example.test"]
                        ),
                        "product": profile.product,
                    },
                )
                self.assertEqual(denied.status_code, 400, denied.text)
                self.assertEqual(ingress.calls, ["list"])
                envelope = _dry_run_envelope()
                response = await client.post(
                    "/v1/production-backup-authority/apply",
                    headers=headers,
                    json=envelope.model_dump(mode="json"),
                )
                self.assertEqual(response.status_code, 200, response.text)
                response = await client.post(
                    "/v1/production-backup-authority/apply",
                    headers=headers,
                    json={
                        **envelope.model_dump(mode="json"),
                        "mode": "apply",
                        "reviewed_authority_digest": response.json()["result"]["authority_digest"],
                    },
                )
                self.assertEqual(response.status_code, 403, response.text)
            self.assertFalse(store.list_production_backup_policy_records())

    async def test_train_plans_cannot_resolve_credentials_or_queue_execution(self) -> None:
        from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record

        with _database_app(reader_policy()) as (store, _, app):
            store.write_merge_train_policy_record(
                build_test_merge_train_policy_record(repository="example/sample")
            )
            with patch(
                "control_plane.http_app.resolve_merge_train_github_token",
                side_effect=AssertionError("Read role must not resolve train credentials"),
            ):
                async with lifespan_client(app) as client:
                    requests = (
                        ("batch-candidate", {"mode": "plan"}),
                        ("batch-landing", {"mode": "plan", "candidate_record_id": "candidate"}),
                        ("controller", {"mutate": False}),
                        ("", {"mutate": False}),
                    )
                    for operation, fields in requests:
                        path = "/v1/work-graph/merge-train/"
                        path += f"{operation}/run-once" if operation else "run-once"
                        response = await client.post(
                            path,
                            headers={"Authorization": "Bearer reader-token"},
                            json={
                                "repository": "example/sample",
                                "github_api_base_url": "https://untrusted.example",
                                **fields,
                            },
                        )
                        self.assertEqual(response.status_code, 403, response.text)
            self.assertFalse(store.list_merge_train_batch_candidate_records())
            self.assertFalse(store.list_merge_train_batch_landing_plan_records())

    async def test_missing_role_cannot_read_service_selector(self) -> None:
        with _database_app(LaunchplaneAuthzPolicy(schema_version=2)) as (_, _, app):
            async with lifespan_client(app) as client:
                result = await client.get(
                    SERVICE_GITHUB_DELIVERY_ROUTE, headers={"Authorization": "Bearer reader-token"}
                )
                self.assertEqual(result.status_code, 403, result.text)
