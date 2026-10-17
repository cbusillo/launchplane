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
        self.old = "http://172.20.0.3:8069"
        self.new = "http://lp-testing-" + "a" * 32 + ":8069"
        self.raw: dict[str, Any] = {
            "routers": {},
            "services": {
                "old@docker": {
                    "status": "enabled",
                    "loadBalancer": {"servers": [{"url": self.old}], "passHostHeader": True},
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
