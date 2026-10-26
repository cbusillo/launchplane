from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from control_plane.contracts.prepared_public_site import FrozenRecord, validate_public_path


def canonical_host_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("cutover host requires an explicit canonical absolute path")
    return value


class CutoverFileRoute(FrozenRecord):
    route_file: str
    api_url: str
    entrypoints: tuple[str, ...] = Field(min_length=1)
    tls: bool
    certificate_resolver: str = ""
    middlewares: tuple[str, ...] = ()

    @field_validator("route_file")
    @classmethod
    def route_path(cls, value: str) -> str:
        canonical_host_path(value)
        if PurePosixPath(value).suffix not in {".yaml", ".yml"}:
            raise ValueError("cutover route requires one YAML file in its watched directory")
        return value

    @field_validator("api_url")
    @classmethod
    def api_origin(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("cutover proxy API requires an explicit HTTP origin")
        return value


class CutoverReadinessRoute(FrozenRecord):
    path: str
    kind: Literal["homepage", "login", "public"]
    contains: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def public_path(cls, value: str) -> str:
        return validate_public_path(value)


class OdooCutoverHostRuntime(FrozenRecord):
    route: CutoverFileRoute
    public_origin: str
    authority_directory: str
    data_volume: str = Field(min_length=1)
    private_network: str = Field(min_length=1)
    prepared_volume: str = Field(min_length=1)
    prepared_container: str = Field(min_length=1)
    prepared_image: str = Field(pattern=r"^.+@sha256:[a-f0-9]{64}$")
    restore_image: str = Field(pattern=r"^.+@sha256:[a-f0-9]{64}$")
    filestore_path: str
    backup_root: str
    website_id: int = Field(ge=1)
    modules: tuple[str, ...] = Field(min_length=1)
    readiness_routes: tuple[CutoverReadinessRoute, ...] = Field(min_length=1)
    public_routes: tuple[str, ...] = Field(min_length=1)
    retained_assets: tuple[str, ...]
    excluded_prefixes: tuple[str, ...] = Field(min_length=1)
    notice: str = Field(min_length=1)
    readiness_timeout_seconds: int = Field(default=90, ge=1, le=300)

    @field_validator("public_origin")
    @classmethod
    def public_origin_value(cls, value: str) -> str:
        return CutoverFileRoute.api_origin(value)

    @field_validator("authority_directory", "filestore_path", "backup_root")
    @classmethod
    def host_path(cls, value: str) -> str:
        return canonical_host_path(value)

    @field_validator("public_routes", "retained_assets", "excluded_prefixes")
    @classmethod
    def public_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            validate_public_path(value)
        if len(values) != len(set(values)):
            raise ValueError("cutover host coverage contains duplicate paths")
        return values

    @model_validator(mode="after")
    def complete_readiness(self) -> Self:
        if not {"homepage", "login", "public"} <= {route.kind for route in self.readiness_routes}:
            raise ValueError("cutover readiness requires homepage, login and public routes")
        if not {"base", "web", "website", "launchplane_runtime_health"} <= set(self.modules):
            raise ValueError("cutover readiness requires its native website and health modules")
        if len(set(self.modules)) != len(self.modules):
            raise ValueError("cutover module coverage must be unique")
        return self
