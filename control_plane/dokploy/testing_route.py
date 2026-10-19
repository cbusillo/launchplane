"""Lane-owned testing traffic over retained Docker routes and disposable copies."""

from copy import deepcopy
from ipaddress import ip_address
import json
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.dokploy.compose import TESTING_CONNECTION_CSP
from control_plane.dokploy.route_rules import host_rule_may_match
from control_plane.dokploy.traefik_cutover import http_route_payload


class TestingRoutePending(ValueError):
    """The owned file and applied provider view have not converged."""


class TestingLaneRoute(FrozenRecord):
    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    old_backend: str
    new_backend: str
    template: dict[str, Any]
    legacy_routers: dict[str, dict[str, Any]]
    legacy_backend: str = ""
    public_entrypoints: tuple[str, ...] = Field(min_length=1)

    @field_validator("template", "legacy_routers", mode="before")
    @classmethod
    def json_document(cls, value: object) -> object:
        return json.loads(json.dumps(value))

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def validate_lane(self) -> Self:
        if self.route_id != testing_route_id(self.product, self.context):
            raise ValueError("Testing traffic identity belongs to another lane.")
        if self.old_backend == self.new_backend:
            raise ValueError("Testing traffic needs an independent successor.")
        for backend in (self.old_backend, self.new_backend):
            parsed = urlsplit(backend)
            if (
                parsed.scheme != "http"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Testing traffic requires its qualified private HTTP origins.")
            try:
                ip_address(parsed.hostname)
            except ValueError:
                pass
            else:
                raise ValueError("Owned testing traffic requires stable private DNS aliases.")
        http = self.template.get("http", {})
        routers = http.get("routers")
        if not isinstance(routers, dict) or not routers:
            raise ValueError("Testing traffic requires an owned route template.")
        if set(http.get("services", {})) != {self.route_id} or any(
            not isinstance(router, dict)
            or router.get("rule") != f"Host(`{self.host}`)"
            or router.get("service") != self.route_id
            or not router.get("entryPoints")
            or not set(router["entryPoints"]) <= set(self.public_entrypoints)
            for router in routers.values()
        ):
            raise ValueError("Testing route template crosses its admitted host or service.")
        return self


def testing_route_id(product: str, context: str) -> str:
    return (
        "lp-testing-route-" + canonical_json_sha256({"product": product, "context": context})[:32]
    )


def router_configuration(router: dict[str, Any]) -> dict[str, Any]:
    """Only applied behavior belongs in a route comparison, never status counters."""
    configuration: dict[str, Any] = {
        key: router[key]
        for key in (
            "rule",
            "ruleSyntax",
            "service",
            "entryPoints",
            "priority",
            "middlewares",
            "tls",
        )
        if key in router
    }
    configuration.setdefault("middlewares", [])
    return configuration


def route_document(plan: TestingLaneRoute, backend: str) -> dict[str, Any]:
    if backend not in {plan.old_backend, plan.new_backend}:
        raise ValueError("Testing traffic cannot select an unadmitted backend.")
    document = deepcopy(plan.template)
    document["http"]["services"][plan.route_id]["loadBalancer"]["servers"] = [{"url": backend}]
    return document


def testing_ingress_entrypoints(raw: dict[str, Any], host: str) -> tuple[str, ...]:
    """Discover the exact application's listeners, not every proxy listener.

    Compilation still checks all possibly matching rules on those listeners.
    Neither a broad rule nor an internal admin route can expand this authority.
    """
    routers = raw.get("routers")
    if not isinstance(routers, dict):
        raise ValueError("Applied testing router inventory is unavailable.")
    points: set[str] = set()
    for name, router in routers.items():
        if (
            isinstance(name, str)
            and name.endswith(("@docker", "@file"))
            and isinstance(router, dict)
            and router.get("rule") in {f"Host(`{host}`)", f'Host("{host}")'}
        ):
            bound = router.get("entryPoints")
            if (
                not isinstance(bound, list)
                or not bound
                or any(not isinstance(point, str) or not point for point in bound)
            ):
                raise ValueError("The testing application's entrypoints are unbound.")
            points.update(bound)
    if not points:
        raise ValueError("The testing application's entrypoints are unavailable.")
    return tuple(sorted(points))


def proxy_internal_router(
    name: str, router: dict[str, Any], application_points: tuple[str, ...]
) -> bool:
    """Recognize only Traefik's canonical challenge and isolated admin routers.

    The reserved priorities keep ACME above every admitted application winner.
    An admin listener overlapping the application is a conflict, never exempt.
    """
    kind = name.removesuffix("@internal")
    if name != kind + "@internal" or kind not in {"acme-http", "api", "dashboard"}:
        return False
    rule, offset, middlewares = {
        "acme-http": ("PathPrefix(`/.well-known/acme-challenge/`)", 0, []),
        "api": ("PathPrefix(`/api`)", 1, []),
        "dashboard": (
            "PathPrefix(`/`)",
            2,
            ["dashboard_redirect@internal", "dashboard_stripprefix@internal"],
        ),
    }[kind]
    configuration = router_configuration(router)
    syntax = configuration.pop("ruleSyntax", None)
    points = configuration.get("entryPoints")
    priority = configuration.get("priority")
    if (
        router.get("status") != "enabled"
        or syntax not in {None, "default", "v3"}
        or not isinstance(points, list)
        or not points
        or any(not isinstance(point, str) or not point for point in points)
        or type(priority) is not int
        or priority not in {2**31 - 1 - offset, 2**63 - 1 - offset}
        or (
            kind != "acme-http" and (points != ["traefik"] or set(points) & set(application_points))
        )
    ):
        return False
    return configuration == {
        "rule": rule,
        "service": name,
        "entryPoints": points,
        "priority": priority,
        "middlewares": middlewares,
    }


def possible_lane_routers(
    raw: dict[str, Any], host: str, entrypoints: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    routers = raw.get("routers")
    if not isinstance(routers, dict):
        raise ValueError("Applied testing router inventory is unavailable.")
    matches = {}
    for name, router in routers.items():
        if (
            not isinstance(name, str)
            or not isinstance(router, dict)
            or not isinstance(router.get("entryPoints"), list)
        ):
            raise ValueError("Applied testing router inventory is incomplete.")
        if proxy_internal_router(name, router, entrypoints):
            continue
        if set(router["entryPoints"]) & set(entrypoints) and host_rule_may_match(
            str(router.get("rule") or ""), host
        ):
            matches[name] = router
    return matches


def applied_service_backend(raw: dict[str, Any], service: str, provider: str) -> str:
    name = service if "@" in service else service + "@" + provider
    observed = raw.get("services", {}).get(name)
    if not isinstance(observed, dict) or observed.get("status") != "enabled":
        raise ValueError("Applied testing backend is unavailable.")
    balance = observed.get("loadBalancer")
    if not isinstance(balance, dict) or balance.get("passHostHeader", True) is not True:
        raise ValueError("Testing backend must preserve the stable host.")
    servers = balance.get("servers")
    if (
        not isinstance(servers, list)
        or len(servers) != 1
        or not isinstance(servers[0], dict)
        or not isinstance(servers[0].get("url"), str)
    ):
        raise ValueError("Testing backend must identify one private copy.")
    return str(servers[0]["url"])


def redirects_to_https(raw: dict[str, Any], router: dict[str, Any]) -> bool:
    for reference in router.get("middlewares", []):
        if not isinstance(reference, str):
            raise ValueError("Testing predecessor middleware is malformed.")
        reference = reference if "@" in reference else reference + "@docker"
        middleware = raw.get("middlewares", {}).get(reference)
        if (
            isinstance(middleware, dict)
            and middleware.get("redirectScheme", {}).get("scheme") == "https"
        ):
            return True
    return False


def compile_testing_route(
    *,
    product: str,
    context: str,
    host: str,
    old_backend: str,
    old_backend_addresses: tuple[str, ...] = (),
    new_backend: str,
    raw: dict[str, Any],
    public_entrypoints: tuple[str, ...],
    owned_document: dict[str, Any] | None = None,
    recorded_template: dict[str, Any] | None = None,
) -> TestingLaneRoute:
    """Bind actual predecessor authority; the caller proves backend physical custody.

    A later rebuild loads its template from the existing route-binding's original
    receipt. A filename alone never authorizes adopting an unrecorded file.
    """
    identifier = testing_route_id(product, context)
    matches = possible_lane_routers(raw, host, public_entrypoints)
    legacy = {
        name: router_configuration(router)
        for name, router in matches.items()
        if name.endswith("@docker")
    }
    legacy_backends = {
        applied_service_backend(raw, str(matches[name].get("service") or ""), "docker")
        for name in legacy
    }
    if len(legacy_backends) > 1 or any(
        backend not in {old_backend, *old_backend_addresses} for backend in legacy_backends
    ):
        raise ValueError("Docker predecessor differs from its physically verified copy.")
    legacy_backend = next(iter(legacy_backends), "")
    if owned_document is not None:
        if recorded_template is None:
            raise ValueError("Owned testing route requires its recorded template authority.")
        plan = TestingLaneRoute(
            product=product,
            context=context,
            host=host,
            route_id=identifier,
            old_backend=old_backend,
            new_backend=new_backend,
            template=recorded_template,
            legacy_routers=legacy,
            legacy_backend=legacy_backend,
            public_entrypoints=public_entrypoints,
        )
        if owned_document != route_document(plan, old_backend):
            raise ValueError("Testing route file differs from the serving copy's authority.")
        observe_testing_route(plan, raw, owned_document)
        return plan
    if recorded_template is not None or not matches or len(legacy) != len(matches):
        raise ValueError("Initial testing traffic must have only its observed Docker predecessor.")
    routers: dict[str, Any] = {}
    services: dict[str, Any] = {}
    connection_middleware = identifier + "-connections"
    owned_middlewares: dict[str, Any] = {}
    used_entrypoints: set[str] = set()
    equivalents: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, (name, router) in enumerate(sorted(matches.items())):
        configuration = router_configuration(router)
        rule = str(router.get("rule") or "")
        exact_rules = {f"Host(`{host}`)", f'Host("{host}")'}
        if (
            router.get("status") != "enabled"
            or rule not in exact_rules
            or applied_service_backend(raw, str(router.get("service") or ""), "docker")
            != legacy_backend
        ):
            raise ValueError("Testing predecessor is not the exact healthy lane backend.")
        points = tuple(router["entryPoints"])
        if not points or not set(points) <= set(public_entrypoints):
            raise ValueError("Testing predecessor has overlapping or unbound entrypoints.")
        priority = router.get("priority") or len(rule.encode())
        if type(priority) is not int or not 0 < priority < 2147482647:
            raise ValueError("Testing predecessor priority cannot admit a strict winner.")
        references = configuration["middlewares"]
        if not isinstance(references, list) or any(
            not isinstance(reference, str) for reference in references
        ):
            raise ValueError("Testing predecessor middleware is malformed.")
        # Service names can differ only after every service has been bound to
        # the same observed backend above. Keep all aliases in legacy_routers:
        # observation must still reject drift in any of them after takeover.
        behavior = {
            **configuration,
            "rule": f"Host(`{host}`)",
            "entryPoints": sorted(set(points)),
            "priority": priority,
            "middlewares": [
                reference if "@" in reference else reference + "@docker" for reference in references
            ],
        }
        service = str(router.get("service") or "")
        service = service if "@" in service else service + "@docker"
        behavior["loadBalancer"] = raw["services"][service]["loadBalancer"]
        behavior.pop("service", None)
        group = tuple(sorted(set(points)))
        if used_entrypoints & set(points):
            if equivalents.get(group) != behavior:
                raise ValueError("Testing predecessor aliases have conflicting routing behavior.")
            continue
        equivalents[group] = behavior
        used_entrypoints.update(points)
        middlewares = []
        for reference in router.get("middlewares", []):
            if not isinstance(reference, str):
                raise ValueError("Testing predecessor middleware is malformed.")
            reference = reference if "@" in reference else reference + "@docker"
            if reference.endswith("@file"):
                middlewares.append(reference)
                continue
            current = raw.get("middlewares", {}).get(reference)
            if (
                not reference.endswith("@docker")
                or not isinstance(current, dict)
                or current.get("status") != "enabled"
                or current.get("headers") != {"contentSecurityPolicy": TESTING_CONNECTION_CSP}
                or set(current) - {"headers", "status", "usedBy", "name", "provider", "type"}
            ):
                raise ValueError("Testing predecessor has unsupported copy-owned middleware.")
            # This managed public header must survive retirement of its Docker producer.
            owned_middlewares[connection_middleware] = {"headers": current["headers"]}
            middlewares.append(connection_middleware + "@file")
        if not redirects_to_https(raw, router):
            owned_middlewares[connection_middleware] = {
                "headers": {"contentSecurityPolicy": TESTING_CONNECTION_CSP}
            }
        if (
            not redirects_to_https(raw, router)
            and connection_middleware + "@file" not in middlewares
        ):
            middlewares.append(connection_middleware + "@file")
        tls = configuration.get("tls")
        if tls is not None and (
            not isinstance(tls, dict) or set(tls) - {"certResolver", "options"}
        ):
            raise ValueError("Testing predecessor TLS configuration is unsupported.")
        document = http_route_payload(
            route_id=identifier,
            host=host,
            entrypoints=points,
            backend=old_backend,
            tls=tls is not None,
            certificate_resolver=str((tls or {}).get("certResolver") or ""),
            middlewares=tuple(middlewares),
        )
        desired = document["http"]["routers"][identifier]
        if "ruleSyntax" in configuration:
            desired["ruleSyntax"] = configuration["ruleSyntax"]
        if tls is not None:
            desired["tls"] = dict(tls)
        desired["priority"] = priority + 1
        routers[identifier + "-" + str(index)] = desired
        services = document["http"]["services"]
    plan = TestingLaneRoute(
        product=product,
        context=context,
        host=host,
        route_id=identifier,
        old_backend=old_backend,
        new_backend=new_backend,
        template={
            "http": {
                "routers": routers,
                "services": services,
                **({"middlewares": owned_middlewares} if owned_middlewares else {}),
            }
        },
        legacy_routers=legacy,
        legacy_backend=legacy_backend,
        public_entrypoints=public_entrypoints,
    )
    observe_testing_route(plan, raw, None)
    return plan


def observe_testing_route(
    plan: TestingLaneRoute, raw: dict[str, Any], owned_document: dict[str, Any] | None
) -> str:
    """Require one effective owned winner per entrypoint, plus its actual backend."""
    matches = possible_lane_routers(raw, plan.host, plan.public_entrypoints)
    owned = plan.template["http"]["routers"]
    names = {name + "@file" for name in owned}
    for name, router in matches.items():
        if name in names:
            expected = owned[name.removesuffix("@file")]
            observed = router_configuration(router)
            # Traefik qualifies file middleware/service references in rawdata.
            expected = {**expected, "service": plan.route_id + "@file"}
            observed["service"] = str(observed.get("service") or "").removesuffix("@file") + "@file"
            if router.get("status") != "enabled" or observed != expected:
                raise ValueError("Owned testing router differs from the admitted lane route.")
        elif (
            name not in plan.legacy_routers
            or router.get("status") != "enabled"
            or router_configuration(router) != plan.legacy_routers[name]
            or applied_service_backend(raw, str(router.get("service") or ""), "docker")
            != (plan.legacy_backend or plan.old_backend)
        ):
            raise ValueError("Foreign or changed testing router appeared.")
    present = names & set(matches)
    if not present:
        if owned_document is not None:
            raise TestingRoutePending("Testing route file has not been applied.")
        if set(matches) != set(plan.legacy_routers) or not matches:
            raise ValueError("Original testing predecessor is unavailable.")
        return plan.old_backend
    if present != names:
        raise TestingRoutePending("Testing route group is only partly applied.")
    # Each recorded legacy route must remain strictly below its owned winner.
    for name, router in matches.items():
        if name in names:
            continue
        priority = router.get("priority") or len(str(router["rule"]).encode())
        for desired in owned.values():
            if (
                set(router["entryPoints"]) & set(desired["entryPoints"])
                and priority >= desired["priority"]
            ):
                raise ValueError("Testing routing has no strict owned winner.")
    for name, expected in plan.template["http"].get("middlewares", {}).items():
        applied = raw.get("middlewares", {}).get(name + "@file")
        if (
            not isinstance(applied, dict)
            or applied.get("status") != "enabled"
            or applied.get("headers") != expected["headers"]
        ):
            raise TestingRoutePending("Testing connection middleware has not been applied.")
    backend = applied_service_backend(raw, plan.route_id, "file")
    if backend not in {plan.old_backend, plan.new_backend}:
        raise ValueError("Testing route selected an unadmitted backend.")
    if owned_document != route_document(plan, backend):
        raise TestingRoutePending("Testing file and applied backend have not converged.")
    return backend
