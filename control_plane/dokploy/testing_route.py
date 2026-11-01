"""Lane-owned testing traffic over retained Docker routes and disposable copies."""

from copy import deepcopy
import hashlib
from ipaddress import ip_address
import json
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
import yaml

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.dokploy.compose import TESTING_CONNECTION_CSP
from control_plane.dokploy.route_rules import host_rule_may_match
from control_plane.dokploy.traefik_cutover import http_route_payload

type TestingRouteHost = Annotated[str, Field(pattern=r"^[a-zA-Z0-9.-]+$")]


def testing_route_rule(host: str, aliases: tuple[str, ...] = ()) -> str:
    return " || ".join(f"Host(`{name}`)" for name in (host, *aliases))


def select_testing_route_raw(
    raw: dict[str, Any],
    host: str,
    points: tuple[str, ...],
    external_middlewares: tuple[str, ...] | None = None,
    discover_external_redirects: bool = False,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    routers = raw.get("routers")
    if not isinstance(routers, dict):
        raise RuntimeError("Applied router inventory is unavailable")
    selected = {}
    services = {}
    middlewares = {}
    supplied_middlewares = raw.get("middlewares")
    if external_middlewares and not isinstance(supplied_middlewares, dict):
        raise RuntimeError("Applied middleware inventory is unavailable")
    for name, item in (supplied_middlewares or {}).items():
        if external_middlewares is not None and name in external_middlewares:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("status"), str)
                or "redirectScheme" in item
                and not isinstance(item["redirectScheme"], dict)
            ):
                raise RuntimeError("Applied external middleware inventory is incomplete")
            middlewares[name] = {
                key: item[key] for key in ("redirectScheme", "status") if key in item
            }
            continue
        if (
            discover_external_redirects
            and external_middlewares is None
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
        if (points and not set(item["entryPoints"]) & set(points)) or not any(
            host_rule_may_match(str(item.get("rule") or ""), name) for name in (host, *aliases)
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
                if (
                    external_middlewares is not None
                    and qualified in external_middlewares
                    and isinstance(supplied_middlewares, dict)
                    and qualified not in supplied_middlewares
                ):
                    continue
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
    pass


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
    host: TestingRouteHost
    aliases: tuple[TestingRouteHost, ...] = Field(
        default=(), max_length=1, exclude_if=lambda value: not value
    )
    address_only: bool = Field(default=False, exclude_if=lambda value: not value)
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
    application_file_name: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9_-]+\.yml)?$", exclude_if=lambda value: not value
    )
    application_file_sha256: str = Field(
        default="", pattern=r"^(?:[a-f0-9]{64})?$", exclude_if=lambda value: not value
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
        if len({name.casefold() for name in (self.host, *self.aliases)}) != 1 + len(self.aliases):
            raise ValueError("Testing route hostnames must be distinct.")
        if bool(self.application_file_name) != bool(self.application_file_sha256):
            raise ValueError("The original application route requires its exact file identity.")
        if self.route_id != testing_route_id(self.product, self.context):
            raise ValueError("Testing traffic identity belongs to another lane.")
        if (self.old_backend == self.new_backend) != self.address_only:
            raise ValueError("Only an address amendment can preserve the current backend.")
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
            or router.get("rule") != testing_route_rule(self.host, self.aliases)
            or router.get("service") != self.route_id
            or not router.get("entryPoints")
            or not set(router["entryPoints"]) <= set(self.public_entrypoints)
            for router in routers.values()
        ):
            raise ValueError("Testing route template crosses its admitted host or service.")
        if self.aliases and any(
            "tls" in router
            and (
                not isinstance(router["tls"], dict)
                or router["tls"].get("domains")
                != [{"main": name} for name in (self.host, *self.aliases)]
            )
            for router in routers.values()
        ):
            raise ValueError("Testing aliases require independent hostname certificates.")
        return self


def amend_testing_route_address(before: TestingLaneRoute, hostname: str) -> TestingLaneRoute:
    if before.aliases or hostname.casefold() == before.host.casefold():
        raise ValueError("Address amendment requires a new canonical host and its original alias.")
    payload = before.model_dump(mode="json")
    payload.update(
        host=hostname,
        aliases=[before.host],
        address_only=True,
        old_backend=before.new_backend,
        unavailable_preimage=None,
        application_file_name="",
        application_file_sha256="",
    )
    for router in payload["template"]["http"]["routers"].values():
        router["rule"] = testing_route_rule(hostname, (before.host,))
        if "tls" in router:
            router["tls"]["domains"] = [{"main": hostname}, {"main": before.host}]
    return TestingLaneRoute.model_validate(payload)


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
    raw: dict[str, Any], host: str, entrypoints: tuple[str, ...], aliases: tuple[str, ...] = ()
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
        if set(router["entryPoints"]) & set(entrypoints) and any(
            host_rule_may_match(str(router.get("rule") or ""), name) for name in (host, *aliases)
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


def redirects_to_https(
    raw: dict[str, Any], router: dict[str, Any], provider: str = "docker"
) -> bool:
    for reference in router.get("middlewares", []):
        if not isinstance(reference, str):
            raise ValueError("Testing predecessor middleware is malformed.")
        reference = reference if "@" in reference else reference + "@" + provider
        middleware = raw.get("middlewares", {}).get(reference)
        if (
            isinstance(middleware, dict)
            and middleware.get("redirectScheme", {}).get("scheme") == "https"
        ):
            return True
    return False


def testing_route_middlewares_owned(template: dict[str, Any]) -> bool:
    return not testing_route_external_middlewares(template) and all(
        not router.get("tls", {}).get("options") for router in template["http"]["routers"].values()
    )


def testing_route_https_redirect(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"redirectScheme"}
        and isinstance(value["redirectScheme"], dict)
        and set(value["redirectScheme"]) <= {"scheme", "permanent"}
        and value["redirectScheme"].get("scheme") == "https"
        and type(value["redirectScheme"].get("permanent", False)) is bool
    )


def application_load_balancer_configuration(value: Any) -> Any:
    """Compare source and runtime with ordinary Traefik defaults materialized."""
    if not isinstance(value, dict):
        return value
    configuration = dict(value)
    configuration.setdefault("passHostHeader", True)
    configuration.setdefault("strategy", "wrr")
    forwarding = configuration.get("responseForwarding", {})
    if isinstance(forwarding, dict):
        configuration["responseForwarding"] = {"flushInterval": "100ms", **forwarding}
    return configuration


def require_independent_listener_middlewares(static: Any, points: tuple[str, ...]) -> None:
    if not isinstance(static, dict) or not isinstance(static.get("entryPoints"), dict):
        raise ValueError("The managed proxy's listener configuration is unavailable.")
    for point in points:
        listener = static["entryPoints"].get(point)
        if not isinstance(listener, dict) or not isinstance(listener.get("http", {}), dict):
            raise ValueError("The managed proxy's listener configuration is unavailable.")
        if listener.get("http", {}).get("middlewares"):
            raise ValueError("The testing listener uses unsupported middleware.")


def router_with_listener_defaults(
    router: dict[str, Any], static_configuration: str
) -> dict[str, Any]:
    configuration = router_configuration(router)
    if not static_configuration:
        return configuration
    static = yaml.safe_load(static_configuration)
    if not isinstance(static, dict):
        raise ValueError("The application listener configuration is unavailable.")
    core = static.get("core") or {}
    if not isinstance(core, dict):
        raise ValueError("The application rule syntax configuration is unavailable.")
    syntax = core.get("defaultRuleSyntax", "")
    if not isinstance(syntax, str) or syntax not in {"", "default", "v2", "v3"}:
        raise ValueError("The application rule syntax configuration is unavailable.")
    if not configuration.get("ruleSyntax") and syntax:
        configuration["ruleSyntax"] = syntax
    if configuration.get("tls") is not None:
        return configuration
    points = configuration.get("entryPoints")
    if (
        not isinstance(static.get("entryPoints"), dict)
        or not isinstance(points, (list, tuple))
        or len(points) != 1
    ):
        raise ValueError("The application listener TLS configuration is unavailable.")
    entrypoint = static["entryPoints"].get(points[0])
    if not isinstance(entrypoint, dict):
        raise ValueError("The application listener TLS configuration is unavailable.")
    http = entrypoint.get("http")
    if http is None:
        http = {}
    if not isinstance(http, dict):
        raise ValueError("The application listener TLS configuration is unavailable.")
    if "tls" not in http:
        configuration.pop("tls", None)
    else:
        tls = http["tls"]
        if (
            not isinstance(tls, dict)
            or set(tls) - {"certResolver", "options"}
            or any(not isinstance(value, str) for value in tls.values())
        ):
            raise ValueError("The application listener TLS configuration is unavailable.")
        configuration["tls"] = {key: value for key, value in tls.items() if value}
    return configuration


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
    aliases: tuple[str, ...] = (),
    application_document: dict[str, Any] | None = None,
    application_file_name: str = "",
    application_file_sha256: str = "",
    static_configuration: str = "",
    recorded_application_route: TestingLaneRoute | None = None,
    require_owned_middlewares: bool = False,
) -> TestingLaneRoute:
    route = _compile_testing_route(
        product=product,
        context=context,
        host=host,
        old_backend=old_backend,
        old_backend_addresses=old_backend_addresses,
        new_backend=new_backend,
        raw=raw,
        public_entrypoints=public_entrypoints,
        owned_document=owned_document,
        recorded_template=recorded_template,
        aliases=aliases,
        application_document=application_document,
        application_file_name=application_file_name,
        application_file_sha256=application_file_sha256,
        static_configuration=static_configuration,
        recorded_application_route=recorded_application_route,
        require_owned_middlewares=require_owned_middlewares,
    )
    assert route is not None
    return route


def admit_testing_route_predecessor(**arguments: Any) -> None:
    """Validate retained source and applied authority without inventing a successor."""
    _compile_testing_route(new_backend=None, **arguments)


def _compile_testing_route(
    *,
    product: str,
    context: str,
    host: str,
    old_backend: str,
    old_backend_addresses: tuple[str, ...] = (),
    new_backend: str | None,
    raw: dict[str, Any],
    public_entrypoints: tuple[str, ...],
    owned_document: dict[str, Any] | None = None,
    recorded_template: dict[str, Any] | None = None,
    aliases: tuple[str, ...] = (),
    application_document: dict[str, Any] | None = None,
    application_file_name: str = "",
    application_file_sha256: str = "",
    static_configuration: str = "",
    recorded_application_route: TestingLaneRoute | None = None,
    require_owned_middlewares: bool = False,
) -> TestingLaneRoute | None:
    """Bind actual predecessor authority; the caller proves backend physical custody.

    A later rebuild loads its template from the existing route-binding's original
    receipt. A filename alone never authorizes adopting an unrecorded file.
    """
    identifier = testing_route_id(product, context)
    if aliases and recorded_template is None:
        raise ValueError("Retained testing hostnames require recorded route authority.")
    matches = possible_lane_routers(raw, host, public_entrypoints, aliases)
    legacy = {
        name: router_configuration(router)
        for name, router in matches.items()
        if name.endswith("@docker")
    }
    if application_document is not None:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+\.yml", application_file_name) or not re.fullmatch(
            r"[a-f0-9]{64}", application_file_sha256
        ):
            raise ValueError("The retained application file has no physical custody.")
        http = application_document.get("http")
        if not isinstance(http, dict) or not isinstance(http.get("routers"), dict):
            raise ValueError("The retained application routing document is unavailable.")
        for name, router in matches.items():
            if not name.endswith("@file") or name.removesuffix("@file") not in http["routers"]:
                continue
            expected = router_configuration(http["routers"][name.removesuffix("@file")])
            observed = router_configuration(router)
            if (
                expected.get("tls") is None
                and observed.get("tls") is not None
                or not expected.get("ruleSyntax")
                and observed.get("ruleSyntax") not in {None, "default", "v3"}
            ):
                expected = router_with_listener_defaults(
                    http["routers"][name.removesuffix("@file")], static_configuration
                )
            for configuration in (expected, observed):
                configuration["service"] = (
                    str(configuration.get("service") or "").removesuffix("@file") + "@file"
                )
                configuration["middlewares"] = [
                    reference if "@" in reference else reference + "@file"
                    for reference in configuration["middlewares"]
                ]
                if not configuration.get("priority", 0):
                    configuration["priority"] = len(str(configuration["rule"]).encode("utf-8"))
                if configuration.get("ruleSyntax") in {None, "", "default", "v3"}:
                    configuration.pop("ruleSyntax", None)
            service = str(expected["service"]).removesuffix("@file")
            configured = http.get("services", {}).get(service)
            applied = raw.get("services", {}).get(service + "@file")
            if (
                expected != observed
                or not isinstance(configured, dict)
                or not isinstance(applied, dict)
                or application_load_balancer_configuration(configured.get("loadBalancer"))
                != application_load_balancer_configuration(applied.get("loadBalancer"))
            ):
                raise ValueError(
                    "The applied application predecessor differs from its actual file."
                )
            legacy[name] = router_configuration(router)
    elif application_file_name or application_file_sha256:
        raise ValueError("The retained application file is unavailable.")
    if recorded_application_route is not None and owned_document is not None:
        previous = recorded_application_route
        if (previous.product, previous.context, previous.host, previous.template) != (
            product,
            context,
            host,
            recorded_template,
        ) or observe_testing_route(previous, raw, owned_document) != old_backend:
            raise ValueError("The completed application route authority changed.")
        legacy = {
            name: configuration
            for name, configuration in previous.legacy_routers.items()
            if name in matches
        }
    legacy_backends = {
        applied_service_backend(
            raw, str(matches[name].get("service") or ""), name.rsplit("@", 1)[-1]
        )
        for name in legacy
    }
    admitted_backends = {old_backend, *old_backend_addresses}
    if recorded_application_route is not None:
        previous = recorded_application_route
        if (
            owned_document is None
            and (
                previous.product,
                previous.context,
                previous.host,
                previous.template,
                previous.application_file_name,
                previous.application_file_sha256,
            )
            != (
                product,
                context,
                host,
                recorded_template,
                application_file_name,
                application_file_sha256,
            )
            or owned_document is None
            and legacy != previous.legacy_routers
        ):
            raise ValueError("The original application's retained route custody changed.")
        admitted_backends.add(previous.legacy_backend)
    if len(legacy_backends) > 1 or any(
        backend not in admitted_backends for backend in legacy_backends
    ):
        raise ValueError("Docker predecessor differs from its physically verified copy.")
    legacy_backend = next(iter(legacy_backends), "")
    if owned_document is not None:
        if require_owned_middlewares and (
            recorded_template is None or not testing_route_middlewares_owned(recorded_template)
        ):
            raise ValueError("The testing route still depends on predecessor-owned configuration.")
        if new_backend is None:
            if (
                recorded_application_route is None
                or recorded_template != recorded_application_route.template
            ):
                raise ValueError("Owned testing route requires its original completed template.")
            if (
                observe_testing_route(recorded_application_route, raw, owned_document)
                != old_backend
            ):
                raise ValueError(
                    "The completed application route does not select its retained copy."
                )
            return None
        if recorded_template is None:
            raise ValueError("Owned testing route requires its recorded template authority.")
        plan = TestingLaneRoute(
            product=product,
            context=context,
            host=host,
            aliases=aliases,
            route_id=identifier,
            old_backend=old_backend,
            new_backend=new_backend,
            template=recorded_template,
            legacy_routers=legacy,
            legacy_backend=legacy_backend,
            public_entrypoints=public_entrypoints,
            application_file_name=application_file_name,
            application_file_sha256=application_file_sha256,
        )
        if owned_document != route_document(plan, old_backend):
            raise ValueError("Testing route file differs from the serving copy's authority.")
        observe_testing_route(plan, raw, owned_document)
        return plan
    if recorded_template is not None or not matches or len(legacy) != len(matches):
        raise ValueError("Initial testing traffic must have only its observed Docker predecessor.")
    if static_configuration:
        require_independent_listener_middlewares(
            yaml.safe_load(static_configuration),
            tuple({point for router in matches.values() for point in router["entryPoints"]}),
        )
    routers: dict[str, Any] = {}
    services: dict[str, Any] = {}
    connection_middleware = identifier + "-connections"
    owned_middlewares: dict[str, Any] = {}
    used_entrypoints: set[str] = set()
    equivalents: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, (name, router) in enumerate(sorted(matches.items())):
        provider = name.rsplit("@", 1)[-1]
        configuration = router_configuration(router)
        rule = str(router.get("rule") or "")
        exact_rules = {f"Host(`{host}`)", f'Host("{host}")'}
        if (
            router.get("status") != "enabled"
            or rule not in exact_rules
            or applied_service_backend(raw, str(router.get("service") or ""), provider)
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
        middlewares = []
        for reference in router.get("middlewares", []):
            if not isinstance(reference, str):
                raise ValueError("Testing predecessor middleware is malformed.")
            reference = reference if "@" in reference else reference + "@" + provider
            if reference.endswith("@file"):
                current = raw.get("middlewares", {}).get(reference)
                redirect_configuration = (
                    {"redirectScheme": current["redirectScheme"]}
                    if isinstance(current, dict) and "redirectScheme" in current
                    else None
                )
                if (
                    testing_route_https_redirect(redirect_configuration)
                    and current.get("status") == "enabled"
                    and not set(current)
                    - {"redirectScheme", "status", "usedBy", "name", "provider", "type"}
                ):
                    redirect = identifier + "-https"
                    assert redirect_configuration is not None
                    if (
                        redirect in owned_middlewares
                        and owned_middlewares[redirect] != redirect_configuration
                    ):
                        raise ValueError("The testing HTTPS redirects disagree.")
                    owned_middlewares[redirect] = redirect_configuration
                    middlewares.append(redirect + "@file")
                    continue
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
        if not redirects_to_https(raw, router, provider):
            owned_middlewares[connection_middleware] = {
                "headers": {"contentSecurityPolicy": TESTING_CONNECTION_CSP}
            }
        if (
            not redirects_to_https(raw, router, provider)
            and connection_middleware + "@file" not in middlewares
        ):
            middlewares.append(connection_middleware + "@file")
        behavior = {
            **configuration,
            "rule": f"Host(`{host}`)",
            "entryPoints": sorted(set(points)),
            "priority": priority,
            "middlewares": middlewares,
        }
        service = str(router.get("service") or "")
        service = service if "@" in service else service + "@" + provider
        behavior["loadBalancer"] = raw["services"][service]["loadBalancer"]
        behavior.pop("service", None)
        group = tuple(sorted(set(points)))
        if used_entrypoints & set(points):
            if equivalents.get(group) != behavior:
                raise ValueError("Testing predecessor aliases have conflicting routing behavior.")
            continue
        equivalents[group] = behavior
        used_entrypoints.update(points)
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
    if require_owned_middlewares and not testing_route_middlewares_owned(
        {"http": {"routers": routers, "middlewares": owned_middlewares}}
    ):
        raise ValueError("The testing route still depends on predecessor-owned configuration.")
    if new_backend is None:
        return None
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
        application_file_name=application_file_name,
        application_file_sha256=application_file_sha256,
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
    aliases: tuple[str, ...] = (),
    recorded_backend: str | None = None,
) -> TestingLaneRoute:
    static = yaml.safe_load(static_configuration)
    if not isinstance(static, dict) or not isinstance(static.get("entryPoints"), dict):
        raise ValueError("The managed proxy's listener configuration is unavailable.")
    identifier = testing_route_id(product, context)
    template = deepcopy(recorded_template)
    if aliases and template is None:
        raise ValueError("Retained testing hostnames require recorded route authority.")
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
        for name, rule in sorted(declared.items()):
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
            require_independent_listener_middlewares(static, points)
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
            tls = fields.get("tls", "").lower()
            if tls not in {"", "true", "false"}:
                raise ValueError("The declared testing TLS behavior is unavailable.")
            resolver = fields.get("tls.certresolver", "") if tls != "false" else ""
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
            if "tls.options" in fields and tls != "false":
                raise ValueError(
                    "Declared TLS options require an original recorded testing template."
                )
            for point in dict.fromkeys(points):
                document = http_route_payload(
                    route_id=identifier,
                    host=host,
                    entrypoints=(point,),
                    backend=new_backend,
                    tls="tls" in fields and tls != "false" or bool(resolver),
                    certificate_resolver=resolver,
                    middlewares=tuple(references),
                )
                router = document["http"]["routers"][identifier]
                router["priority"] = priority + 1
                if "rulesyntax" in fields:
                    router["ruleSyntax"] = fields["rulesyntax"]
                router = router_with_listener_defaults(router, static_configuration)
                if router.get("tls", {}).get("options"):
                    raise ValueError(
                        "Declared TLS options require an original recorded testing template."
                    )
                group = (point,)
                if point in used_entrypoints:
                    if equivalent_routers.get(group) != router:
                        raise ValueError(
                            "Declared testing aliases have conflicting routing behavior."
                        )
                    continue
                equivalent_routers[group] = router
                used_entrypoints.add(point)
                routers = template["http"]["routers"]
                routers[identifier + "-" + str(len(routers))] = router
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
    raw = select_testing_route_raw(
        raw, host, points, testing_route_external_middlewares(template), aliases=aliases
    )
    matches = possible_lane_routers(raw, host, points, aliases)
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
            aliases=aliases,
        )
    return TestingLaneRoute(
        product=product,
        context=context,
        host=host,
        aliases=aliases,
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
            static_sha256=testing_route_static_sha256(static_configuration, template),
            declaration_sha256=canonical_json_sha256(declared_testing_ingress(configured_compose))
            if recorded_template is None
            else "",
        ),
    )


def testing_route_static_sha256(static_configuration: str, template: dict[str, Any]) -> str:
    static = yaml.safe_load(static_configuration)
    if not isinstance(static, dict) or not isinstance(static.get("entryPoints"), dict):
        raise ValueError("The managed proxy's listener configuration is unavailable.")
    routers = template["http"]["routers"].values()
    points = {point for router in routers for point in router.get("entryPoints", [])}
    resolvers = {
        router["tls"]["certResolver"]
        for router in routers
        if isinstance(router.get("tls"), dict) and router["tls"].get("certResolver")
    }
    configured_resolvers = static.get("certificatesResolvers", {})
    if not isinstance(configured_resolvers, dict):
        raise ValueError("The managed proxy's certificate configuration is unavailable.")
    return canonical_json_sha256(
        {
            "entryPoints": {name: static["entryPoints"].get(name) for name in sorted(points)},
            "certificatesResolvers": {
                name: configured_resolvers.get(name) for name in sorted(resolvers)
            },
        }
    )


def declared_testing_ingress(configured_compose: str) -> dict[str, str]:
    try:
        configuration = yaml.safe_load(configured_compose)
    except yaml.YAMLError as error:
        raise ValueError("The provider's testing ingress declaration is malformed.") from error
    services = configuration.get("services") if isinstance(configuration, dict) else None
    if not isinstance(services, dict):
        raise ValueError("The provider's converted testing services are unavailable.")
    if "web" not in services:
        return {}
    web = services["web"]
    if not isinstance(web, dict):
        raise ValueError("The provider's converted testing web service is unavailable.")
    if "labels" not in web:
        return {}
    supplied = web["labels"]
    if isinstance(supplied, list):
        if any(not isinstance(item, str) or "=" not in item for item in supplied):
            raise ValueError("The declared testing router labels are incomplete.")
        pairs = [item.split("=", 1) for item in supplied]
        labels = dict(pairs)
        if any(labels[key] != value for key, value in pairs):
            raise ValueError("The declared testing router labels conflict.")
    elif isinstance(supplied, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in supplied.items()
    ):
        labels = supplied
    else:
        raise ValueError("The provider's converted testing router declaration is unavailable.")
    return {key: value for key, value in labels.items() if key.startswith("traefik.")}


def require_original_testing_route_applied(plan: TestingLaneRoute, raw: dict[str, Any]) -> None:
    preimage = plan.unavailable_preimage
    if preimage is None:
        raise ValueError("Original unavailable ingress requires its admitted preimage.")
    current = select_testing_route_raw(
        raw,
        plan.host,
        plan.public_entrypoints,
        testing_route_external_middlewares(plan.template),
        aliases=plan.aliases,
    )
    for section in ("routers", "services", "middlewares"):
        for name in plan.template["http"].get(section, {}):
            key = name + "@file"
            if current[section].get(key) != preimage.raw[section].get(key):
                raise TestingRoutePending("The original testing ingress has not been applied.")


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
            aliases=plan.aliases,
        )
        for name in testing_route_external_middlewares(plan.template):
            current = scoped["middlewares"].get(name)
            expected = preimage.raw["middlewares"].get(name)
            if current is not None and current.get("status") != "enabled":
                raise TestingRoutePending("Original testing redirect is not enabled.")
            if current != expected:
                raise TestingRouteAuthorityChanged("Original testing redirect changed.")
        if owned_document == original and scoped == preimage.raw:
            return None
    matches = possible_lane_routers(raw, plan.host, plan.public_entrypoints, plan.aliases)
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
                if preimage is None or router.get("status") == "enabled":
                    raise TestingRouteAuthorityChanged(
                        "Owned testing router differs from the admitted lane route."
                    )
                raise TestingRoutePending("Owned testing router is not enabled.")
        elif (
            name not in plan.legacy_routers
            or router.get("status") != "enabled"
            or router_configuration(router) != plan.legacy_routers[name]
            or applied_service_backend(
                raw, str(router.get("service") or ""), name.rsplit("@", 1)[-1]
            )
            != (plan.legacy_backend or plan.old_backend)
        ):
            if preimage is None or router.get("status") == "enabled":
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
                raise TestingRouteAuthorityChanged("Testing routing has no strict owned winner.")
    for name, expected in plan.template["http"].get("middlewares", {}).items():
        applied = raw.get("middlewares", {}).get(name + "@file")
        if (
            not isinstance(applied, dict)
            or applied.get("status") != "enabled"
            or any(applied.get(key) != value for key, value in expected.items())
        ):
            raise TestingRoutePending("Testing connection middleware has not been applied.")
    backend = applied_service_backend(raw, plan.route_id, "file")
    if backend not in {plan.old_backend, plan.new_backend}:
        raise TestingRouteAuthorityChanged("Testing route selected an unadmitted backend.")
    if owned_document != route_document(plan, backend):
        raise TestingRoutePending("Testing file and applied backend have not converged.")
    return backend
