import json
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
        normalized_domain_names: list[str] = []
        for raw_domain_name in self.domain_names:
            domain_name = raw_domain_name.strip().lower()
            if not domain_name:
                raise ValueError("NPMplus proxy host domain names must be non-empty")
            if domain_name not in normalized_domain_names:
                normalized_domain_names.append(domain_name)
        if not normalized_domain_names:
            raise ValueError("NPMplus proxy host requires at least one domain name")
        self.domain_names = tuple(normalized_domain_names)
        return self

    def to_api_payload(self) -> JsonObject:
        payload = self.model_dump(
            mode="json",
            exclude={
                "access_list_id": True,
                "locations": {"__all__": {"id"}},
            },
        )
        payload["npmplus_access_list_ids"] = (
            [] if self.access_list_id == 0 else [self.access_list_id]
        )
        payload["npmplus_access_list_type"] = "public" if self.access_list_id == 0 else "custom"
        return payload


class NpmplusProxyHost(NpmplusProxyHostPayload):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=1)
    locations: tuple[NpmplusLocation, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_access_list_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        access_list_ids = value.get("npmplus_access_list_ids")
        access_list_type = value.get("npmplus_access_list_type")
        if access_list_ids is None and access_list_type is None:
            return value
        if access_list_ids is None or access_list_type is None:
            raise ValueError("NPMplus proxy host access-list fields must be provided together")
        if not isinstance(access_list_ids, list) or any(
            isinstance(access_list_id, bool)
            or not isinstance(access_list_id, int)
            or access_list_id < 1
            for access_list_id in access_list_ids
        ):
            raise ValueError("NPMplus proxy host access-list ids must be positive integers")
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

    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]:
        payload = self._request(method="GET", path="/api/nginx/proxy-hosts")
        if not isinstance(payload, list):
            raise click.ClickException("NPMplus proxy-host list returned an invalid payload.")
        proxy_hosts: list[NpmplusProxyHost] = []
        for index, host_payload in enumerate(payload):
            if not isinstance(host_payload, dict):
                raise click.ClickException(
                    f"NPMplus proxy-host list entry {index} returned an invalid payload."
                )
            proxy_hosts.append(NpmplusProxyHost.model_validate(host_payload))
        return tuple(proxy_hosts)

    def get_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        payload = self._request(method="GET", path=f"/api/nginx/proxy-hosts/{host_id}")
        if not isinstance(payload, dict):
            raise click.ClickException("NPMplus proxy-host read returned an invalid payload.")
        return NpmplusProxyHost.model_validate(payload)

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
        response = self._request(
            method="POST",
            path="/api/nginx/proxy-hosts",
            payload=payload.to_api_payload(),
        )
        if not isinstance(response, dict):
            raise click.ClickException("NPMplus proxy-host create returned an invalid payload.")
        return NpmplusProxyHost.model_validate(response)

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
        return NpmplusProxyHost.model_validate(response)

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
