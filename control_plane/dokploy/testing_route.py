"""Lane-owned testing traffic over retained Docker routes and disposable copies."""

from copy import deepcopy
import hashlib
from ipaddress import ip_address
import json
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
import yaml

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.dokploy.compose import TESTING_CONNECTION_CSP
from control_plane.dokploy.route_rules import host_rule_may_match
from control_plane.dokploy.traefik_cutover import http_route_payload


def select_testing_route_raw(
    raw: dict[str, Any],
    host: str,
    points: tuple[str, ...],
    external_middlewares: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    routers = raw.get("routers")
    if not isinstance(routers, dict):
        raise RuntimeError("Applied router inventory is unavailable")
    selected = {}
    services = {}
    middlewares = {}
    # Declared stopped-container routes may reference this already-applied,
    # value-free HTTPS redirect without any application router being present.
    for name, item in raw.get("middlewares", {}).items():
        if (
            (external_middlewares is None or name in external_middlewares)
            and name.endswith("@file")
            and isinstance(item, dict)
            and item.get("redirectScheme", {}).get("scheme") == "https"
        ):
            middlewares[name] = {
                key: item[key] for key in ("redirectScheme", "status") if key in item
            }
    for name, item in routers.items():
        if not isinstance(item, dict) or not isinstance(item.get("entryPoints"), list):
            raise RuntimeError("Applied router inventory is incomplete")
        if external_middlewares is not None and canonical_acme_challenge(name, item):
            continue
        if (points and not set(item["entryPoints"]) & set(points)) or not host_rule_may_match(
            str(item.get("rule") or ""), host
        ):
            continue
        selected[name] = {
            key: item[key]
            for key in (
                "rule",
                "ruleSyntax",
                "service",
                "entryPoints",
                "priority",
                "middlewares",
                "tls",
                "status",
            )
            if key in item
        }
        provider = name.rsplit("@", 1)[-1]
        service = str(item.get("service") or "")
        service = service if "@" in service else service + "@" + provider
        value = raw.get("services", {}).get(service)
        if not isinstance(value, dict):
            raise RuntimeError("Applied backend inventory is incomplete")
        services[service] = {
            "status": value.get("status"),
            "loadBalancer": value.get("loadBalancer"),
        }
        for reference in item.get("middlewares", []):
            qualified = reference if "@" in reference else reference + "@" + provider
            middleware = raw.get("middlewares", {}).get(qualified)
            if not isinstance(middleware, dict):
                raise RuntimeError("Applied middleware inventory is incomplete")
            middlewares[qualified] = {
                key: middleware[key]
                for key in ("headers", "redirectScheme", "status")
                if key in middleware
            }
    return {"routers": selected, "services": services, "middlewares": middlewares}


def testing_route_external_middlewares(template: dict[str, Any]) -> tuple[str, ...]:
    owned = {name + "@file" for name in template["http"].get("middlewares", {})}
    return tuple(
        sorted(
            {
                reference
                for router in template["http"]["routers"].values()
                for reference in router.get("middlewares", [])
                if reference.endswith("@file") and reference not in owned
            }
        )
    )


class TestingRoutePending(ValueError):
    """The owned file and applied provider view have not converged."""


class TestingRouteAuthorityChanged(ValueError):
    """A positively enabled route conflicts with the original lane authority."""


class UnavailableTestingRoutePreimage(FrozenRecord):
    file_content: str | None
    raw: dict[str, Any]
    observation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    applied: bool
    static_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    declaration_sha256: str = Field(default="", exclude_if=lambda value: not value)

    @field_validator("raw", mode="before")
    @classmethod
    def capture_raw(cls, value: object) -> object:
        return json.loads(json.dumps(value))


class TestingLaneRoute(FrozenRecord):
    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    old_backend: str | None
    new_backend: str
    template: dict[str, Any]
    legacy_routers: dict[str, dict[str, Any]]
    legacy_backend: str = ""
    public_entrypoints: tuple[str, ...] = Field(min_length=1)
    unavailable_preimage: UnavailableTestingRoutePreimage | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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
        if (self.old_backend is None) != (self.unavailable_preimage is not None):
            raise ValueError("Unavailable predecessor routing requires its distinct preimage.")
        for backend in (self.old_backend, self.new_backend):
            if backend is None:
                if self.unavailable_preimage is None:
                    raise ValueError("A missing backend requires its unavailable route preimage.")
                continue
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


def route_document(plan: TestingLaneRoute, backend: str | None) -> dict[str, Any]:
    if backend not in {plan.old_backend, plan.new_backend}:
        raise ValueError("Testing traffic cannot select an unadmitted backend.")
    if backend is None:
        assert plan.unavailable_preimage is not None
        content = plan.unavailable_preimage.file_content
        if content is None:
            raise ValueError("An absent preimage has no route document.")
        document = json.loads(content)
        if not isinstance(document, dict):
            raise ValueError("The captured testing route is not a document.")
        return document
    document = deepcopy(plan.template)
    document["http"]["services"][plan.route_id]["loadBalancer"]["servers"] = [{"url": backend}]
    return document


def route_document_content(plan: TestingLaneRoute, backend: str | None) -> str | None:
    if backend is None:
        assert plan.unavailable_preimage is not None
        return plan.unavailable_preimage.file_content
    return json.dumps(route_document(plan, backend), sort_keys=True, separators=(",", ":"))


def route_document_digest(plan: TestingLaneRoute, backend: str | None) -> str:
    content = route_document_content(plan, backend)
    return hashlib.sha256(content.encode()).hexdigest() if content is not None else "absent"


def testing_ingress_entrypoints(raw: dict[str, Any], host: str) -> tuple[str, ...]:
    """Discover potentially serving application listeners, not proxy-internal ones.

    Compilation still checks all possibly matching rules on those listeners.
    Non-exact rules enter that check so conflicts cannot hide on another listener.
    """
    routers = raw.get("routers")
    if not isinstance(routers, dict):
        raise ValueError("Applied testing router inventory is unavailable.")
    points: set[str] = set()
    for name, router in routers.items():
        if (
            isinstance(name, str)
            and not name.endswith("@internal")
            and isinstance(router, dict)
            and host_rule_may_match(str(router.get("rule") or ""), host)
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


def canonical_acme_challenge(name: str, router: dict[str, Any]) -> bool:
    """Keep the canonical challenge above every admitted application winner."""
    if name != "acme-http@internal":
        return False
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
        or priority not in {2**31 - 1, 2**63 - 1}
    ):
        return False
    return configuration == {
        "rule": "PathPrefix(`/.well-known/acme-challenge/`)",
        "service": name,
        "entryPoints": points,
        "priority": priority,
        "middlewares": [],
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
        if canonical_acme_challenge(name, router):
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


def compile_unavailable_testing_route(
    *,
    product: str,
    context: str,
    host: str,
    new_backend: str,
    raw: dict[str, Any],
    file_content: str | None,
    observation_sha256: str,
    static_configuration: str,
    configured_compose: str,
    recorded_template: dict[str, Any] | None = None,
    recorded_backend: str | None = None,
) -> TestingLaneRoute:
    """Use declared ingress without claiming a stopped Docker router is applied."""
    static = yaml.safe_load(static_configuration)
    if not isinstance(static, dict) or not isinstance(static.get("entryPoints"), dict):
        raise ValueError("The managed proxy's listener configuration is unavailable.")
    identifier = testing_route_id(product, context)
    template = deepcopy(recorded_template)
    if template is None:
        if file_content is not None:
            raise ValueError("The existing testing route has no recorded template authority.")
        labels = declared_testing_ingress(configured_compose)
        if (
            labels.get("traefik.enable", "").lower() != "true"
            or labels.get("traefik.docker.network") != "dokploy-network"
        ):
            raise ValueError("The declared testing web service does not own managed ingress.")
        declared = {}
        for key, value in labels.items():
            if key.startswith("traefik.http.routers.") and key.endswith(".rule"):
                if host_rule_may_match(value, host):
                    if value not in {f"Host(`{host}`)", f'Host("{host}")'}:
                        raise ValueError("The declared testing router is not an exact host rule.")
                    declared[key.removeprefix("traefik.http.routers.").removesuffix(".rule")] = (
                        value
                    )
        if not declared:
            raise ValueError("The provider has no declared router for the owned testing address.")
        template = {"http": {"routers": {}, "services": {}, "middlewares": {}}}
        connections = identifier + "-connections"
        used_entrypoints: set[str] = set()
        equivalent_routers: dict[tuple[str, ...], dict[str, Any]] = {}
        for index, (name, rule) in enumerate(sorted(declared.items())):
            prefix = "traefik.http.routers." + name + "."
            fields = {
                key.removeprefix(prefix): value
                for key, value in labels.items()
                if key.startswith(prefix)
            }
            service = fields.get("service")
            if service is not None:
                service_prefix = "traefik.http.services." + service + "."
                service_fields = {
                    key.removeprefix(service_prefix): value
                    for key, value in labels.items()
                    if key.startswith(service_prefix)
                }
                port = service_fields.get("loadbalancer.server.port", "")
                if (
                    "@" in service
                    or set(service_fields) != {"loadbalancer.server.port"}
                    or not port.isdigit()
                    or not 0 < int(port) < 65536
                ):
                    raise ValueError(
                        "The declared testing router does not select its local web service."
                    )
            if set(fields) - {
                "rule",
                "entrypoints",
                "service",
                "middlewares",
                "tls",
                "tls.certresolver",
                "tls.options",
                "priority",
                "rulesyntax",
            }:
                raise ValueError("The declared testing router uses unsupported routing behavior.")
            points = tuple(fields.get("entrypoints", "").split(","))
            if not points or any(
                not point or point not in static["entryPoints"] for point in points
            ):
                raise ValueError(
                    "The declared testing listeners are unavailable on the managed proxy."
                )
            references = []
            for reference in filter(None, fields.get("middlewares", "").split(",")):
                if reference.endswith("@file"):
                    applied = raw.get("middlewares", {}).get(reference)
                    if (
                        not isinstance(applied, dict)
                        or applied.get("status") != "enabled"
                        or applied.get("redirectScheme", {}).get("scheme") != "https"
                    ):
                        raise ValueError("The declared testing redirect middleware is unavailable.")
                    references.append(reference)
                elif (
                    labels.get(
                        "traefik.http.middlewares." + reference + ".headers.contentsecuritypolicy"
                    )
                    == TESTING_CONNECTION_CSP
                ):
                    if {
                        key
                        for key in labels
                        if key.startswith("traefik.http.middlewares." + reference + ".")
                    } != {
                        "traefik.http.middlewares." + reference + ".headers.contentsecuritypolicy"
                    }:
                        raise ValueError(
                            "The copy-owned testing middleware has unsupported behavior."
                        )
                    references.append(connections + "@file")
                else:
                    raise ValueError(
                        "The declared testing middleware has no supported independent copy."
                    )
            tls = fields.get("tls", "false").lower()
            if tls not in {"true", "false"}:
                raise ValueError("The declared testing TLS behavior is unavailable.")
            resolver = fields.get("tls.certresolver", "")
            if resolver and resolver not in static.get("certificatesResolvers", {}):
                raise ValueError("The declared testing certificate resolver is unavailable.")
            priority = int(fields.get("priority", str(len(rule.encode()))))
            if not 0 < priority < 2147482647:
                raise ValueError("The declared testing priority cannot admit a strict winner.")
            if (
                not redirects_to_https(raw, {"middlewares": references})
                and connections + "@file" not in references
            ):
                references.append(connections + "@file")
            document = http_route_payload(
                route_id=identifier,
                host=host,
                entrypoints=points,
                backend=new_backend,
                tls=tls == "true",
                certificate_resolver=resolver,
                middlewares=tuple(references),
            )
            router = document["http"]["routers"][identifier]
            router["priority"] = priority + 1
            if "rulesyntax" in fields:
                router["ruleSyntax"] = fields["rulesyntax"]
            if "tls.options" in fields:
                raise ValueError(
                    "Declared TLS options require an original recorded testing template."
                )
            group = tuple(sorted(set(points)))
            if used_entrypoints & set(points):
                if equivalent_routers.get(group) != router:
                    raise ValueError("Declared testing aliases have conflicting routing behavior.")
                continue
            equivalent_routers[group] = router
            used_entrypoints.update(points)
            template["http"]["routers"][identifier + "-" + str(index)] = router
            template["http"]["services"] = document["http"]["services"]
        template["http"]["middlewares"][connections] = {
            "headers": {"contentSecurityPolicy": TESTING_CONNECTION_CSP}
        }
    points = tuple(
        sorted(
            {
                point
                for router in template["http"]["routers"].values()
                for point in router.get("entryPoints", [])
            }
        )
    )
    if not points or not set(points) <= set(static["entryPoints"]):
        raise ValueError("The original testing template's listeners are unavailable.")
    for router in template["http"]["routers"].values():
        tls_config = router.get("tls")
        if tls_config is not None:
            if not isinstance(tls_config, dict):
                raise ValueError("The testing template's TLS configuration is unavailable.")
            recorded_resolver = tls_config.get("certResolver")
            if recorded_resolver and recorded_resolver not in static.get(
                "certificatesResolvers", {}
            ):
                raise ValueError("The testing template's certificate resolver is unavailable.")
    raw = select_testing_route_raw(raw, host, points, testing_route_external_middlewares(template))
    matches = possible_lane_routers(raw, host, points)
    if file_content is None:
        if matches:
            raise ValueError("An unavailable Docker predecessor still has applied testing routers.")
    else:
        if recorded_template is None or recorded_backend is None:
            raise ValueError("The original testing file has no recorded authority.")
        compile_testing_route(
            product=product,
            context=context,
            host=host,
            old_backend=recorded_backend,
            new_backend=new_backend,
            raw=raw,
            public_entrypoints=points,
            owned_document=json.loads(file_content),
            recorded_template=recorded_template,
        )
    return TestingLaneRoute(
        product=product,
        context=context,
        host=host,
        route_id=identifier,
        old_backend=None,
        new_backend=new_backend,
        template=template,
        legacy_routers={},
        public_entrypoints=points,
        unavailable_preimage=UnavailableTestingRoutePreimage(
            file_content=file_content,
            raw=raw,
            observation_sha256=observation_sha256,
            applied=bool(matches),
            static_sha256=hashlib.sha256(static_configuration.encode()).hexdigest(),
            declaration_sha256=canonical_json_sha256(declared_testing_ingress(configured_compose))
            if recorded_template is None
            else "",
        ),
    )


def declared_testing_ingress(configured_compose: str) -> dict[str, str]:
    """The provider's web ingress labels, independent of rendering and application data."""
    try:
        configuration = yaml.safe_load(configured_compose)
    except yaml.YAMLError as error:
        raise ValueError("The provider's testing ingress declaration is malformed.") from error
    services = configuration.get("services") if isinstance(configuration, dict) else None
    web = services.get("web") if isinstance(services, dict) else None
    supplied = web.get("labels") if isinstance(web, dict) else None
    if isinstance(supplied, list):
        if any(not isinstance(item, str) or "=" not in item for item in supplied):
            raise ValueError("The declared testing router labels are incomplete.")
        pairs = [item.split("=", 1) for item in supplied]
        if len({pair[0] for pair in pairs}) != len(pairs):
            raise ValueError("The declared testing router labels conflict.")
        labels = dict(pairs)
    elif isinstance(supplied, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in supplied.items()
    ):
        labels = supplied
    else:
        raise ValueError("The provider's converted testing router declaration is unavailable.")
    return {key: value for key, value in labels.items() if key.startswith("traefik.")}


def observe_testing_route(
    plan: TestingLaneRoute, raw: dict[str, Any], owned_document: dict[str, Any] | None
) -> str | None:
    """Require one effective owned winner per entrypoint, plus its actual backend."""
    preimage = plan.unavailable_preimage
    if preimage is not None:
        original = json.loads(preimage.file_content) if preimage.file_content is not None else None
        scoped = select_testing_route_raw(
            raw,
            plan.host,
            plan.public_entrypoints,
            testing_route_external_middlewares(plan.template),
        )
        if owned_document == original and scoped == preimage.raw:
            return None
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
                if router.get("status") == "enabled":
                    raise TestingRouteAuthorityChanged(
                        "Owned testing router differs from the admitted lane route."
                    )
                raise TestingRoutePending("Owned testing router is not enabled.")
        elif (
            name not in plan.legacy_routers
            or router.get("status") != "enabled"
            or router_configuration(router) != plan.legacy_routers[name]
            or applied_service_backend(raw, str(router.get("service") or ""), "docker")
            != (plan.legacy_backend or plan.old_backend)
        ):
            if router.get("status") == "enabled":
                raise TestingRouteAuthorityChanged("Foreign or changed testing router appeared.")
            raise TestingRoutePending("The current testing router is not enabled.")
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
