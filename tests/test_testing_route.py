"""Same-address traffic authority across migration, rebuild and provider delays."""

from copy import deepcopy
from typing import Any
import unittest

from control_plane.dokploy.compose import TESTING_CONNECTION_CSP
from control_plane.dokploy.route_rules import host_rule_may_match
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    TestingRoutePending,
    compile_testing_route,
    observe_testing_route,
    route_document,
)


def add_proxy_internal_routes(raw: dict[str, Any]) -> None:
    """Captured provider frame shape, with no deployed host or application names."""
    for name, rule, points, offset, middlewares in (
        ("acme-http", "PathPrefix(`/.well-known/acme-challenge/`)", ["web"], 0, []),
        ("api", "PathPrefix(`/api`)", ["traefik"], 1, []),
        (
            "dashboard",
            "PathPrefix(`/`)",
            ["traefik"],
            2,
            ["dashboard_redirect@internal", "dashboard_stripprefix@internal"],
        ),
    ):
        raw["routers"][name + "@internal"] = {
            "rule": rule,
            "ruleSyntax": "default",
            "entryPoints": points,
            "service": name + "@internal",
            "priority": 2**63 - 1 - offset,
            "middlewares": middlewares,
            "status": "enabled",
        }
        raw["services"][name + "@internal"] = {"status": "enabled"}
    for name in ("dashboard_redirect", "dashboard_stripprefix"):
        raw["middlewares"][name + "@internal"] = {"status": "enabled"}


class RoutingRuleTests(unittest.TestCase):
    def test_boolean_host_ownership_does_not_ignore_broad_or_negated_rules(self) -> None:
        host = "testing.example.invalid"
        for rule, expected in (
            ("Host(`other.invalid`) && PathPrefix(`/`)", False),
            ("Host(`other.invalid`) || PathPrefix(`/`)", True),
            ("!Host(`other.invalid`)", True),
            ("!(Host(`other.invalid`) || Host(`testing.example.invalid`))", False),
            ("Host(`testing.example.invalid`) && Path(`/web`)", True),
            ("HostRegexp(`^foreign.*$`)", True),
            ("Host(`*.example.invalid`)", True),
            ("Host(`TESTING.EXAMPLE.INVALID`)", True),
        ):
            with self.subTest(rule=rule):
                self.assertEqual(host_rule_may_match(rule, host), expected)
        for unsupported in (
            "Host(`other.invalid`",
            "Host(`other.invalid`) garbage",
            "Host(other.invalid)",
        ):
            with self.subTest(unsupported=unsupported), self.assertRaises(ValueError):
                host_rule_may_match(unsupported, host)


class TestingRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = "testing.example.invalid"
        self.old = "http://testing-old-web-1:8069"
        self.old_address = "http://172.20.0.3:8069"
        self.new = "http://lp-testing-" + "a" * 32 + ":8069"
        self.raw: dict[str, Any] = {
            "routers": {},
            "services": {
                "old@docker": {
                    "status": "enabled",
                    "loadBalancer": {
                        "servers": [{"url": self.old_address}],
                        "passHostHeader": True,
                    },
                }
            },
            "middlewares": {
                "old-connections@docker": {
                    "status": "enabled",
                    "headers": {"contentSecurityPolicy": TESTING_CONNECTION_CSP},
                }
            },
        }
        for name, entrypoint in (("old-http", "web"), ("old-https", "websecure")):
            router = {
                "rule": f"Host(`{self.host}`)",
                "entryPoints": [entrypoint],
                "service": "old@docker",
                "status": "enabled",
                "priority": 0,
                "middlewares": ["old-connections@docker"],
            }
            if entrypoint == "websecure":
                router["tls"] = {}
            self.raw["routers"][name + "@docker"] = router

    def plan(self, **extra: Any) -> TestingLaneRoute:
        return compile_testing_route(
            product="example",
            context="example",
            host=self.host,
            old_backend=self.old,
            old_backend_addresses=(self.old_address,),
            new_backend=self.new,
            raw=self.raw,
            public_entrypoints=("web", "websecure"),
            **extra,
        )

    def apply(
        self, plan: TestingLaneRoute, backend: str, *, retire_old: bool = False
    ) -> dict[str, Any]:
        raw = deepcopy(self.raw)
        document = route_document(plan, backend)["http"]
        for section in ("routers", "services", "middlewares"):
            for name, value in document.get(section, {}).items():
                raw[section][name + "@file"] = {**value, "status": "enabled"}
        if retire_old:
            raw["routers"] = {
                key: value for key, value in raw["routers"].items() if not key.endswith("@docker")
            }
            raw["services"].pop("old@docker", None)
            raw["middlewares"].pop("old-connections@docker", None)
        return raw

    def test_application_file_defaults_preserve_behavior_and_refuse_real_drift(self) -> None:
        import hashlib
        import json

        document: dict[str, Any] = {"http": {"routers": {}, "services": {}, "middlewares": {}}}
        for section, values in self.raw.items():
            for name, value in values.items():
                configured = {key: deepcopy(item) for key, item in value.items() if key != "status"}
                if section == "routers":
                    configured.pop("priority", None)
                    configured["service"] = configured["service"].removesuffix("@docker")
                    configured["middlewares"] = [
                        reference.removesuffix("@docker") for reference in configured["middlewares"]
                    ]
                document["http"][section][name.removesuffix("@docker")] = configured
        applied: dict[str, Any] = {"routers": {}, "services": {}, "middlewares": {}}
        for section, values in document["http"].items():
            for name, value in values.items():
                observed = deepcopy(value)
                if section == "routers":
                    observed["priority"] = len(observed["rule"].encode("utf-8"))
                    observed["service"] += "@file"
                    observed["middlewares"] = [
                        reference + "@file" for reference in observed["middlewares"]
                    ]
                elif section == "services":
                    observed["loadBalancer"].update(
                        strategy="wrr", responseForwarding={"flushInterval": "100ms"}
                    )
                applied[section][name + "@file"] = {**observed, "status": "enabled"}

        def plan() -> TestingLaneRoute:
            self.raw = applied
            return self.plan(
                application_document=document,
                application_file_name="old-app.yml",
                application_file_sha256=hashlib.sha256(json.dumps(document).encode()).hexdigest(),
            )

        self.assertEqual(plan().legacy_backend, self.old_address)
        document["http"]["routers"]["old-http"]["priority"] = 123
        applied["routers"]["old-http@file"]["priority"] = 123
        self.assertEqual(plan().legacy_backend, self.old_address)
        for field in ("priority", "backend", "strategy", "forwarding", "tls"):
            original = deepcopy(applied)
            with self.subTest(field=field):
                if field == "priority":
                    applied["routers"]["old-http@file"]["priority"] += 1
                elif field == "tls":
                    applied["routers"]["old-https@file"]["tls"] = {"certResolver": "different"}
                else:
                    balancing = applied["services"]["old@file"]["loadBalancer"]
                    if field == "backend":
                        balancing["servers"] = [{"url": "http://foreign:8069"}]
                    elif field == "strategy":
                        balancing["strategy"] = "p2c"
                    else:
                        balancing["responseForwarding"]["flushInterval"] = "5ms"
                with self.assertRaises(ValueError):
                    plan()
            applied = original

    def test_migration_file_uses_the_verified_container_alias_instead_of_its_ip(self) -> None:
        plan = self.plan()
        document = route_document(plan, plan.old_backend)
        self.assertEqual(
            document["http"]["services"][plan.route_id]["loadBalancer"]["servers"],
            [{"url": self.old}],
        )
        self.assertNotIn(self.old_address, str(document))
        self.assertEqual(observe_testing_route(plan, self.raw, None), self.old)

    def test_internal_acme_and_admin_routes_survive_application_takeover(self) -> None:
        add_proxy_internal_routes(self.raw)
        original = deepcopy(self.raw)
        plan = self.plan()
        self.assertEqual(self.raw, original)
        self.assertFalse(any(name.endswith("@internal") for name in plan.legacy_routers))
        document = route_document(plan, self.new)
        applied = self.apply(plan, self.new)
        self.assertEqual(observe_testing_route(plan, applied, document), self.new)
        applied["routers"]["acme-http@internal"]["rule"] = "PathPrefix(`/`)"
        with self.assertRaises(ValueError):
            observe_testing_route(plan, applied, document)

    def test_equivalent_docker_aliases_are_bound_and_observed_individually(self) -> None:
        self.raw["services"]["alias@docker"] = deepcopy(self.raw["services"]["old@docker"])
        for name, router in list(self.raw["routers"].items()):
            self.raw["routers"]["alias-" + name] = {**deepcopy(router), "service": "alias@docker"}
        plan = self.plan()
        self.assertEqual(set(plan.legacy_routers), set(self.raw["routers"]))
        document = route_document(plan, self.new)
        applied = self.apply(plan, self.new)
        self.assertEqual(observe_testing_route(plan, applied, document), self.new)
        applied["services"]["alias@docker"]["loadBalancer"]["servers"] = [
            {"url": "http://another-copy:8069"}
        ]
        with self.assertRaises(ValueError):
            observe_testing_route(plan, applied, document)

    def test_conflicting_alias_behavior_or_physical_backend_still_refuses(self) -> None:
        original = deepcopy(self.raw)
        changes: tuple[dict[str, Any], ...] = ({"priority": 900}, {"tls": {}}, {"middlewares": []})
        for change in changes:
            with self.subTest(change=change):
                self.raw = deepcopy(original)
                self.raw["routers"]["alias@docker"] = {
                    **deepcopy(self.raw["routers"]["old-http@docker"]),
                    **change,
                }
                with self.assertRaises(ValueError):
                    self.plan()
        for balance in (
            {"servers": [{"url": "http://foreign:8069"}]},
            {
                **original["services"]["old@docker"]["loadBalancer"],
                "responseForwarding": {"flushInterval": "1s"},
            },
        ):
            with self.subTest(balance=balance), self.assertRaises(ValueError):
                self.raw = deepcopy(original)
                self.raw["services"]["foreign@docker"] = {
                    "status": "enabled",
                    "loadBalancer": balance,
                }
                self.raw["routers"]["alias@docker"] = {
                    **deepcopy(original["routers"]["old-http@docker"]),
                    "service": "foreign@docker",
                }
                self.plan()

    def test_only_canonical_internal_challenge_is_exempt_on_application_entrypoints(self) -> None:
        add_proxy_internal_routes(self.raw)
        original = deepcopy(self.raw)
        mutations: tuple[dict[str, Any], ...] = (
            {"rule": "PathPrefix(`/`)"},
            {"service": "old@docker"},
            {"priority": 1},
            {"middlewares": ["foreign@file"]},
            {"tls": {}},
            {"status": "disabled"},
        )
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.raw = deepcopy(original)
                self.raw["routers"]["acme-http@internal"].update(changes)
                self.plan()
        self.raw = deepcopy(original)
        self.raw["routers"]["spoof@docker"] = deepcopy(self.raw["routers"]["acme-http@internal"])
        with self.assertRaises(ValueError):
            self.plan()
        self.raw = deepcopy(original)
        self.raw["routers"]["api@internal"]["entryPoints"] = ["web"]
        with self.assertRaises(ValueError):
            self.plan()

    def test_owned_route_refuses_an_ip_backend_or_another_host_template(self) -> None:
        plan = self.plan()
        for change in ({"old_backend": self.old_address}, {"host": "another.invalid"}):
            with self.subTest(change=change), self.assertRaises(ValueError) as error:
                TestingLaneRoute.model_validate({**plan.model_dump(), **change})
            self.assertNotIsInstance(error.exception, TestingRoutePending)

    def test_https_redirect_and_tls_options_survive_without_adding_csp_to_redirect(self) -> None:
        redirect = "redirect-to-https@file"
        self.raw["middlewares"][redirect] = {
            "status": "enabled",
            "redirectScheme": {"scheme": "https", "permanent": True},
        }
        self.raw["routers"]["old-http@docker"]["middlewares"] = [redirect]
        tls = {"certResolver": "fixture-resolver", "options": "default@file"}
        self.raw["routers"]["old-https@docker"]["tls"] = tls
        plan = self.plan()
        document = route_document(plan, self.new)
        for router in document["http"]["routers"].values():
            if router["entryPoints"] == ["web"]:
                self.assertEqual(router["middlewares"], [redirect])
            else:
                self.assertEqual(router["tls"], tls)
        applied = self.apply(plan, self.new, retire_old=True)
        self.assertEqual(observe_testing_route(plan, applied, document), self.new)
        middleware = next(iter(document["http"]["middlewares"]))
        self.assertEqual(
            applied["middlewares"][middleware + "@file"]["headers"],
            {"contentSecurityPolicy": TESTING_CONNECTION_CSP},
        )

    def test_migration_first_serves_old_then_switches_same_route_after_candidate_proof(
        self,
    ) -> None:
        plan = self.plan()
        self.assertEqual(observe_testing_route(plan, self.raw, None), self.old)
        old_document = route_document(plan, self.old)
        self.assertEqual(
            observe_testing_route(plan, self.apply(plan, self.old), old_document), self.old
        )
        new_document = route_document(plan, self.new)
        self.assertEqual(
            observe_testing_route(plan, self.apply(plan, self.new), new_document), self.new
        )
        self.assertEqual(
            observe_testing_route(plan, self.apply(plan, self.new, retire_old=True), new_document),
            self.new,
        )
        self.assertEqual(
            {router["rule"] for router in old_document["http"]["routers"].values()},
            {router["rule"] for router in new_document["http"]["routers"].values()},
        )
        self.assertEqual(
            {router["service"] for router in old_document["http"]["routers"].values()},
            {router["service"] for router in new_document["http"]["routers"].values()},
        )
        # The public connection policy survives removal of its Docker producer.
        self.assertTrue(new_document["http"]["middlewares"])

    def test_second_rebuild_uses_recorded_same_host_and_route_without_legacy_container(
        self,
    ) -> None:
        first = self.plan()
        document = route_document(first, self.new)
        raw = self.apply(first, self.new, retire_old=True)
        second = compile_testing_route(
            product=first.product,
            context=first.context,
            host=first.host,
            old_backend=self.new,
            new_backend="http://lp-testing-" + "b" * 32 + ":8069",
            raw=raw,
            public_entrypoints=first.public_entrypoints,
            owned_document=document,
            recorded_template=first.template,
        )
        self.assertEqual(second.route_id, first.route_id)
        self.assertEqual(observe_testing_route(second, raw, document), self.new)
        with self.assertRaises(ValueError):
            compile_testing_route(
                product=first.product,
                context=first.context,
                host="another.invalid",
                old_backend=self.new,
                new_backend=second.new_backend,
                raw=raw,
                public_entrypoints=first.public_entrypoints,
                owned_document=document,
                recorded_template=first.template,
            )

    def test_written_but_unapplied_or_partly_applied_group_stays_pending(self) -> None:
        plan = self.plan()
        document = route_document(plan, self.new)
        with self.assertRaises(TestingRoutePending):
            observe_testing_route(plan, self.raw, document)
        raw = self.apply(plan, self.new)
        raw["routers"].pop(next(key for key in raw["routers"] if key.endswith("@file")))
        with self.assertRaises(TestingRoutePending):
            observe_testing_route(plan, raw, document)
        with self.assertRaises(TestingRoutePending):
            observe_testing_route(plan, self.apply(plan, self.old), document)

    def test_foreign_matching_router_or_changed_legacy_never_becomes_authority(self) -> None:
        plan = self.plan()
        for rule in ("Host(`other.invalid`) || PathPrefix(`/`)", "HostRegexp(`.*`)"):
            raw = self.apply(plan, self.new)
            raw["routers"]["foreign@docker"] = {
                "rule": rule,
                "entryPoints": ["web"],
                "service": "old@docker",
                "priority": 999,
                "status": "enabled",
            }
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                observe_testing_route(plan, raw, route_document(plan, self.new))
        raw = self.apply(plan, self.new)
        raw["routers"]["old-http@docker"]["priority"] = 999
        with self.assertRaises(ValueError):
            observe_testing_route(plan, raw, route_document(plan, self.new))

    def test_foreign_host_conjunction_is_excluded_but_source_backend_must_match(self) -> None:
        self.raw["routers"]["foreign@docker"] = {
            "rule": "Host(`other.invalid`) && PathPrefix(`/`)",
            "entryPoints": ["web"],
            "status": "enabled",
        }
        self.plan()
        self.raw["services"]["old@docker"]["loadBalancer"]["servers"] = [
            {"url": "http://production:8069"}
        ]
        with self.assertRaises(ValueError):
            self.plan()

    def test_unrecorded_or_changed_owned_file_refuses(self) -> None:
        plan = self.plan()
        document = route_document(plan, self.old)
        self.raw = self.apply(plan, self.old)
        with self.assertRaises(ValueError):
            self.plan(owned_document=document)
        changed = deepcopy(document)
        changed["http"]["routers"]["unrelated"] = {"rule": "Host(`unrelated.invalid`)"}
        with self.assertRaises(ValueError):
            self.plan(owned_document=changed, recorded_template=plan.template)
