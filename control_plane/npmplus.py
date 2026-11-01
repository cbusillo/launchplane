import json
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from control_plane.http_error_evidence import ProviderResponseValidationError


type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]
type JsonObject = dict[str, JsonValue]

NpmplusForwardScheme = Literal["http", "https", "path", "empty", "grpc", "grpcs"]
NpmplusAuthRequest = Literal[
    "none",
    "anubis",
    "tinyauth",
    "authelia",
    "authentik",
    "authentik-send-basic-auth",
]


def _normalize_proxy_host_domains(values: tuple[str, ...]) -> tuple[str, ...]:
    names: list[str] = []
    for raw in values:
        name = raw.strip().lower()
        if not name:
            raise ValueError("NPMplus proxy host domain names must be non-empty")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("NPMplus proxy host requires at least one domain name")
    return tuple(names)


def _positive_access_list_ids(value: object) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(access_list_id, bool)
        or not isinstance(access_list_id, int)
        or access_list_id < 1
        for access_list_id in value
    ):
        raise ValueError("NPMplus proxy host access-list ids must be positive integers")
    return value


class NpmplusHostIdentity(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def domain_names(self) -> tuple[str, ...]: ...


class NpmplusProxyHostClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=1)
    domain_names: tuple[str, ...]

    @field_validator("domain_names", mode="after")
    @classmethod
    def _normalize_domain_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_proxy_host_domains(value)


PRIVATE_BEARER_FORWARDING = (
    'set $launchplane_bearer_authorization "";\n'
    'if ($http_authorization ~ "^Bearer .+") {\n'
    "    set $launchplane_bearer_authorization $http_authorization;\n"
    "}\n"
    "proxy_set_header Authorization $launchplane_bearer_authorization;\n"
)


class NpmplusAccessList(BaseModel):
    """Non-secret list metadata, without expanding authentication entries."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    satisfy_any: bool
    pass_auth: bool


class NpmplusLocationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    forward_scheme: NpmplusForwardScheme
    forward_host: str
    forward_port: int | None = Field(default=None, ge=1, le=65535)
    id: int | None = None
    npmplus_enabled: bool = True
    location_type: str = ""
    npmplus_noindex: bool = False
    npmplus_crowdsec_appsec: bool = False
    npmplus_proxy_request_buffering: bool = False
    npmplus_proxy_response_buffering: bool = False
    npmplus_upstream_compression: bool = False
    npmplus_fancyindex: bool = False
    npmplus_x_frame_options: Literal["DENY", "SAMEORIGIN", "upstream", "none"] = "SAMEORIGIN"
    npmplus_auth_request: NpmplusAuthRequest = "none"
    advanced_config: str = ""

    @field_validator("path", "forward_host", mode="after")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("NPMplus location fields must be non-empty")
        return normalized_value


class NpmplusLocation(NpmplusLocationPayload):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _require_round_trip_access_list_policy(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        membership_fields = ("npmplus_access_list_ids", "access_lists")
        if not any(field in value for field in ("npmplus_access_list_type", *membership_fields)):
            return value
        if value.get("npmplus_access_list_type") != "global" or not any(
            field in value for field in membership_fields
        ):
            raise ValueError("NPMplus location requires verified host access-list inheritance")
        if any(value[field] != [] for field in membership_fields if field in value):
            raise ValueError(
                "NPMplus location cannot round-trip independent access-list membership"
            )
        return value


class NpmplusProxyHostPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_names: tuple[str, ...]
    forward_scheme: NpmplusForwardScheme
    forward_host: str
    forward_port: int | None = Field(default=None, ge=1, le=65535)
    certificate_id: int | Literal["new"] = 0
    ssl_forced: bool = True
    hsts_enabled: bool = False
    hsts_subdomains: bool = False
    trust_forwarded_proto: bool = False
    http2_support: bool = True
    npmplus_http3_support: bool = True
    access_list_id: int = Field(default=0, ge=0)
    npmplus_noindex: bool = False
    npmplus_crowdsec_appsec: bool = False
    npmplus_proxy_request_buffering: bool = False
    npmplus_proxy_response_buffering: bool = False
    npmplus_upstream_compression: bool = False
    npmplus_fancyindex: bool = False
    npmplus_x_frame_options: Literal["DENY", "SAMEORIGIN", "upstream", "none"] = "SAMEORIGIN"
    npmplus_auth_request: NpmplusAuthRequest = "none"
    advanced_config: str = ""
    enabled: bool = True
    npmplus_location_config: str = Field(default="", exclude_if=lambda value: not value)
    meta: JsonObject = Field(default_factory=dict)
    locations: tuple[NpmplusLocationPayload, ...] = ()

    @field_validator("forward_host", mode="after")
    @classmethod
    def _validate_forward_host(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("NPMplus proxy host requires a forward host")
        return normalized_value

    @model_validator(mode="after")
    def _validate_domain_names(self) -> "NpmplusProxyHostPayload":
        self.domain_names = _normalize_proxy_host_domains(self.domain_names)
        return self

    def to_api_payload(self) -> JsonObject:
        if self.npmplus_location_config not in ("", PRIVATE_BEARER_FORWARDING):
            raise ValueError("Only the code-owned private Bearer forwarder may be written.")
        if self.trust_forwarded_proto or not self.http2_support or self.meta:
            raise ValueError(
                "The current NPMplus API does not accept legacy protocol or metadata writes."
            )
        payload = self.model_dump(
            mode="json",
            exclude={
                "access_list_id": True,
                "enabled": True,
                "meta": True,
                "trust_forwarded_proto": True,
                "http2_support": True,
                "locations": {"__all__": {"id"}},
            },
        )
        payload["npmplus_access_list_ids"] = (
            [] if self.access_list_id == 0 else [self.access_list_id]
        )
        payload["npmplus_access_list_type"] = "public" if self.access_list_id == 0 else "custom"
        for location in payload["locations"]:
            location["npmplus_access_list_type"] = "global"
            location["npmplus_access_list_ids"] = []
        return payload


class NpmplusProxyHost(NpmplusProxyHostPayload):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=1)
    access_list_id: int = Field(ge=0)
    locations: tuple[NpmplusLocation, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_access_list_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        access_list_ids = value.get("npmplus_access_list_ids")
        access_list_type = value.get("npmplus_access_list_type")
        if access_list_ids is None and access_list_type is None and "access_lists" not in value:
            return value
        if access_list_type is None:
            raise ValueError("NPMplus proxy host access-list type is required")
        if "access_lists" in value:
            relations = value["access_lists"]
            if not isinstance(relations, list) or any(
                not isinstance(relation, dict) or "id" not in relation for relation in relations
            ):
                raise ValueError("NPMplus proxy host access-list relations must contain ids")
            related_ids = _positive_access_list_ids([relation["id"] for relation in relations])
            if (
                access_list_ids is not None
                and _positive_access_list_ids(access_list_ids) != related_ids
            ):
                raise ValueError("NPMplus proxy host access-list representations disagree")
            access_list_ids = related_ids
        if access_list_ids is None:
            raise ValueError("NPMplus proxy host access-list membership is required")
        access_list_ids = _positive_access_list_ids(access_list_ids)
        if access_list_type == "public":
            if access_list_ids:
                raise ValueError("Public NPMplus proxy hosts cannot reference access-list ids")
            normalized_access_list_id = 0
        elif access_list_type == "custom":
            if len(access_list_ids) != 1:
                raise ValueError(
                    "Launchplane requires exactly one access-list id for custom NPMplus hosts"
                )
            normalized_access_list_id = access_list_ids[0]
        else:
            raise ValueError("Unsupported NPMplus proxy host access-list type")
        return {**value, "access_list_id": normalized_access_list_id}

    @field_validator("locations", mode="before")
    @classmethod
    def _normalize_null_locations(cls, value: object) -> object:
        return () if value is None else value


class NpmplusCertificate(BaseModel):
    """Public certificate metadata only; provider DNS credentials are discarded."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=1)
    domain_names: tuple[str, ...]
    expires_on: str


class NpmplusOtherHost(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domain_names: tuple[str, ...] = Field(min_length=1)
    enabled: bool


def _validate_response[Model: BaseModel](model: type[Model], value: object) -> Model:
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise ProviderResponseValidationError(error, model) from error


@dataclass(frozen=True)
class NpmplusCredentials:
    base_url: str
    identity: str
    secret: str

    def __post_init__(self) -> None:
        normalized_base_url = self.base_url.strip().rstrip("/")
        normalized_identity = self.identity.strip()
        if not normalized_base_url:
            raise ValueError("NPMplus base URL is required")
        if not normalized_identity:
            raise ValueError("NPMplus identity is required")
        if not self.secret.strip():
            raise ValueError("NPMplus secret is required")
        object.__setattr__(self, "base_url", normalized_base_url)
        object.__setattr__(self, "identity", normalized_identity)
        object.__setattr__(self, "secret", self.secret.strip())

    @property
    def normalized_base_url(self) -> str:
        return self.base_url


class NpmplusHttpResponse(Protocol):
    def __enter__(self) -> "NpmplusHttpResponse": ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def read(self) -> bytes: ...


class NpmplusHttpOpener(Protocol):
    def open(self, request: Request, timeout: int | float) -> NpmplusHttpResponse: ...


class NpmplusClient:
    def __init__(
        self,
        *,
        credentials: NpmplusCredentials,
        timeout_seconds: int | float = 30,
        opener: NpmplusHttpOpener | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._cookie_jar = CookieJar()
        self._opener = opener or build_opener(HTTPCookieProcessor(self._cookie_jar))
        self._authenticated = False

    def authenticate(self) -> None:
        payload = self._request(
            method="POST",
            path="/api/tokens",
            payload={
                "identity": self._credentials.identity,
                "secret": self._credentials.secret,
            },
            require_authentication=False,
        )
        if _looks_like_authentication_challenge(payload):
            raise click.ClickException(
                "NPMplus authentication requires an interactive challenge; "
                "use a non-2FA automation account for Launchplane ingress."
            )
        self._authenticated = True

    def list_proxy_hosts(self) -> tuple[NpmplusProxyHostClaim, ...]:
        payload = self._request(method="GET", path="/api/nginx/proxy-hosts")
        if not isinstance(payload, list):
            raise click.ClickException("NPMplus proxy-host list returned an invalid payload.")
        proxy_hosts: list[NpmplusProxyHostClaim] = []
        for index, host_payload in enumerate(payload):
            if not isinstance(host_payload, dict):
                raise click.ClickException(
                    f"NPMplus proxy-host list entry {index} returned an invalid payload."
                )
            proxy_hosts.append(_validate_response(NpmplusProxyHostClaim, host_payload))
        return tuple(proxy_hosts)

    def get_proxy_host(
        self, host_id: int, *, public_only: bool = False, private_bearer_auth: bool = False
    ) -> NpmplusProxyHost:
        payload = self._request(method="GET", path=f"/api/nginx/proxy-hosts/{host_id}")
        if not isinstance(payload, dict):
            raise click.ClickException("NPMplus proxy-host read returned an invalid payload.")
        if public_only and (
            (
                payload.get("enabled") is not False
                and payload.get("npmplus_nginx_online") is not True
            )
            or any(
                payload.get(field) != expected
                for field, expected in {
                    "npmplus_mtls_certificate_id": 0,
                    "npmplus_mtls_verify_client_optional": False,
                    "npmplus_mtls_bypass_auth_request": False,
                    "npmplus_auth_request_upstream": "",
                    "npmplus_directory": "",
                }.items()
            )
            or payload.get("npmplus_location_config")
            not in (("", PRIVATE_BEARER_FORWARDING) if private_bearer_auth else ("",))
        ):
            raise click.ClickException("NPMplus route is not a loaded public proxy host.")
        host = _validate_response(NpmplusProxyHost, payload)
        if host.id != host_id:
            raise click.ClickException("NPMplus proxy host identity does not match the request.")
        return host

    def list_other_host_domains(self) -> tuple[str, ...]:
        domains: list[str] = []
        for kind in ("redirection-hosts", "dead-hosts"):
            payload = self._request(method="GET", path=f"/api/nginx/{kind}")
            if not isinstance(payload, list):
                raise click.ClickException("NPMplus hostname claims are unavailable.")
            for item in payload:
                host = _validate_response(NpmplusOtherHost, item)
                if host.enabled:
                    domains.extend(name.strip().lower() for name in host.domain_names)
        return tuple(domains)

    def get_certificate(self, certificate_id: int) -> NpmplusCertificate:
        payload = self._request(method="GET", path=f"/api/nginx/certificates/{certificate_id}")
        if not isinstance(payload, dict):
            raise click.ClickException("NPMplus certificate read returned an invalid payload.")
        certificate = _validate_response(NpmplusCertificate, payload)
        if certificate.id != certificate_id:
            raise click.ClickException("NPMplus certificate identity changed.")
        return certificate

    def get_access_list(self, access_list_id: int) -> NpmplusAccessList:
        payload = self._request(method="GET", path=f"/api/nginx/access-lists/{access_list_id}")
        if not isinstance(payload, dict):
            raise click.ClickException("NPMplus access-list read returned an invalid payload.")
        access_list = _validate_response(NpmplusAccessList, payload)
        if access_list.id != access_list_id:
            raise click.ClickException("NPMplus access-list identity does not match the request.")
        return access_list

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
        if not payload.enabled:
            raise click.ClickException(
                "The current NPMplus API cannot atomically create a disabled host; "
                "existing hosts use the separate enable/disable lifecycle."
            )
        response = self._request(
            method="POST",
            path="/api/nginx/proxy-hosts",
            payload=payload.to_api_payload(),
        )
        if not isinstance(response, dict):
            raise click.ClickException("NPMplus proxy-host create returned an invalid payload.")
        return _validate_response(NpmplusProxyHost, response)

    def update_proxy_host(
        self, *, host_id: int, payload: NpmplusProxyHostPayload
    ) -> NpmplusProxyHost:
        response = self._request(
            method="PUT",
            path=f"/api/nginx/proxy-hosts/{host_id}",
            payload=payload.to_api_payload(),
        )
        if not isinstance(response, dict):
            raise click.ClickException("NPMplus proxy-host update returned an invalid payload.")
        return _validate_response(NpmplusProxyHost, response)

    def disable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self._request(method="POST", path=f"/api/nginx/proxy-hosts/{host_id}/disable")
        return self.get_proxy_host(host_id)

    def enable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self._request(method="POST", path=f"/api/nginx/proxy-hosts/{host_id}/enable")
        return self.get_proxy_host(host_id)

    def delete_proxy_host(self, host_id: int) -> None:
        self._request(method="DELETE", path=f"/api/nginx/proxy-hosts/{host_id}")

    def _request(
        self,
        *,
        method: str,
        path: str,
        payload: JsonObject | None = None,
        query: dict[str, str | int] | None = None,
        require_authentication: bool = True,
    ) -> JsonValue:
        if require_authentication and not self._authenticated:
            self.authenticate()

        normalized_path = path if path.startswith("/") else f"/{path}"
        request_url = f"{self._credentials.normalized_base_url}{normalized_path}"
        if query:
            request_url = f"{request_url}?{urlencode(query)}"

        request_headers = {"Accept": "application/json"}
        request_body: bytes | None = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            request_body = json.dumps(payload).encode()

        request = Request(request_url, data=request_body, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw_payload = response.read()
        except HTTPError as error:
            error_body = error.read().decode(errors="replace").strip()
            raise click.ClickException(
                f"NPMplus API {method} {normalized_path} failed ({error.code}): {error_body}"
            ) from error
        except URLError as error:
            raise click.ClickException(
                f"NPMplus API {method} {normalized_path} request failed: {error.reason}"
            ) from error

        if not raw_payload:
            return {}
        try:
            return _normalize_json_value(json.loads(raw_payload))
        except json.JSONDecodeError:
            return {"raw": raw_payload.decode("utf-8", errors="replace")}


def _normalize_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_value(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
    return str(value)


def _looks_like_authentication_challenge(payload: JsonValue) -> bool:
    if not isinstance(payload, dict):
        return False
    challenge_markers = {
        "challenge",
        "mfa",
        "needs_2fa",
        "otp",
        "requires_2fa",
        "requires_mfa",
        "requires_otp",
        "two_factor",
        "two_factor_required",
    }
    payload_keys = {key.lower().replace("-", "_") for key in payload}
    if payload_keys.intersection(challenge_markers):
        return True
    message = str(payload.get("message") or payload.get("error") or "").lower()
    return "2fa" in message or "mfa" in message or "two-factor" in message
