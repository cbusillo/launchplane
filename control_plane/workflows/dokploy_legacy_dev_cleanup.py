from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Iterable, Mapping
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import JsonObject, JsonValue


DokployRequest = Callable[..., JsonValue]
DokployTargetType = Literal["application", "compose"]

_CONFIRMATION = "delete legacy cm dev targets"


class LegacyDevCleanupTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: DokployTargetType
    target_id: str
    name: str
    project_name: str = ""
    domain_ids: tuple[str, ...] = ()


class LegacyDevCleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False
    suffix: str = "-dev"
    candidate_targets: tuple[LegacyDevCleanupTarget, ...] = ()
    requested_names: tuple[str, ...] = ()
    deleted_targets: tuple[LegacyDevCleanupTarget, ...] = ()
    skipped_targets: tuple[LegacyDevCleanupTarget, ...] = ()
    status: Literal["pass", "fail"] = "pass"
    error_message: str = ""


class LegacyDevCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False
    suffix: str = "-dev"
    target_names: tuple[str, ...] = ()
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "LegacyDevCleanupRequest":
        suffix = self.suffix.strip()
        if not suffix or not suffix.startswith("-"):
            raise ValueError("Legacy dev cleanup suffix must be a hyphen-prefixed suffix.")
        names = tuple(name.strip() for name in self.target_names if name.strip())
        if self.apply and not names:
            raise ValueError("Legacy dev cleanup apply requires exact target names.")
        self.suffix = suffix
        self.target_names = names
        return self


def _as_object(raw: JsonValue) -> JsonObject | None:
    return control_plane_dokploy.as_json_object(raw)


def _collect_list(value: JsonValue | None) -> Iterable[JsonObject]:
    if not isinstance(value, list):
        return ()
    return tuple(item for raw in value if (item := _as_object(raw)) is not None)


def _target_name(payload: Mapping[str, JsonValue]) -> str:
    for key in ("name", "appName", "composeName"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _target_id(payload: Mapping[str, JsonValue], *, target_type: DokployTargetType) -> str:
    id_keys = ("applicationId", "id") if target_type == "application" else ("composeId", "id")
    for key in id_keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _is_forbidden_target_name(name: str) -> bool:
    normalized = name.strip().lower()
    if not normalized:
        return True
    if normalized.startswith("pr-"):
        return True
    if normalized.endswith(("-testing", "-prod", "-production", "-preview")):
        return True
    return normalized in {"testing", "prod", "production", "cm-testing", "cm-prod"}


def _is_legacy_dev_target_name(name: str, *, suffix: str) -> bool:
    normalized = name.strip().lower()
    return normalized.endswith(suffix.lower()) and not _is_forbidden_target_name(normalized)


def _domain_ids_for_target(
    *,
    host: str,
    token: str,
    target: LegacyDevCleanupTarget,
    request: DokployRequest,
) -> tuple[str, ...]:
    if target.target_type == "application":
        path = "/api/domain.byApplicationId"
        query = {"applicationId": target.target_id}
    else:
        path = "/api/domain.byComposeId"
        query = {"composeId": target.target_id}
    raw_domains = request(host=host, token=token, path=path, query=query)
    domain_ids: list[str] = []
    for domain in _collect_list(raw_domains):
        domain_id = str(domain.get("domainId") or "").strip()
        if domain_id:
            domain_ids.append(domain_id)
    return tuple(domain_ids)


def discover_legacy_dev_targets(
    *,
    host: str,
    token: str,
    suffix: str = "-dev",
    request: DokployRequest = control_plane_dokploy.dokploy_request,
) -> tuple[LegacyDevCleanupTarget, ...]:
    projects = request(host=host, token=token, path="/api/project.all")
    targets: list[LegacyDevCleanupTarget] = []
    for project in _collect_list(projects):
        project_name = str(project.get("name") or project.get("projectId") or "").strip()
        for raw_application in _collect_list(project.get("applications")):
            name = _target_name(raw_application)
            target_id = _target_id(raw_application, target_type="application")
            if target_id and _is_legacy_dev_target_name(name, suffix=suffix):
                target = LegacyDevCleanupTarget(
                    target_type="application",
                    target_id=target_id,
                    name=name,
                    project_name=project_name,
                )
                targets.append(
                    target.model_copy(
                        update={
                            "domain_ids": _domain_ids_for_target(
                                host=host, token=token, target=target, request=request
                            )
                        }
                    )
                )
        for key in ("compose", "composes"):
            for raw_compose in _collect_list(project.get(key)):
                name = _target_name(raw_compose)
                target_id = _target_id(raw_compose, target_type="compose")
                if target_id and _is_legacy_dev_target_name(name, suffix=suffix):
                    target = LegacyDevCleanupTarget(
                        target_type="compose",
                        target_id=target_id,
                        name=name,
                        project_name=project_name,
                    )
                    targets.append(
                        target.model_copy(
                            update={
                                "domain_ids": _domain_ids_for_target(
                                    host=host, token=token, target=target, request=request
                                )
                            }
                        )
                    )
    return tuple(sorted(targets, key=lambda item: (item.target_type, item.name, item.target_id)))


def _delete_domain(*, host: str, token: str, domain_id: str, request: DokployRequest) -> None:
    request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _delete_target(
    *, host: str, token: str, target: LegacyDevCleanupTarget, request: DokployRequest
) -> None:
    if target.target_type == "application":
        request(
            host=host,
            token=token,
            path="/api/application.delete",
            method="POST",
            payload={"applicationId": target.target_id},
        )
        return
    request(
        host=host,
        token=token,
        path="/api/compose.delete",
        method="POST",
        payload={"composeId": target.target_id},
    )


def execute_legacy_dev_cleanup(
    *,
    host: str,
    token: str,
    cleanup_request: LegacyDevCleanupRequest,
    request: DokployRequest = control_plane_dokploy.dokploy_request,
) -> LegacyDevCleanupResult:
    candidates = discover_legacy_dev_targets(
        host=host, token=token, suffix=cleanup_request.suffix, request=request
    )
    if not cleanup_request.apply:
        return LegacyDevCleanupResult(
            apply=False,
            suffix=cleanup_request.suffix,
            candidate_targets=candidates,
            requested_names=cleanup_request.target_names,
            skipped_targets=candidates,
        )
    if cleanup_request.confirmation.strip() != _CONFIRMATION:
        raise click.ClickException(
            f"Apply requires confirmation exactly matching {_CONFIRMATION!r}."
        )
    candidates_by_name = {target.name: target for target in candidates}
    missing_names = tuple(
        name for name in cleanup_request.target_names if name not in candidates_by_name
    )
    if missing_names:
        raise click.ClickException(
            "Apply requested names that are not discoverable legacy dev targets: "
            + ", ".join(missing_names)
        )
    deleted_targets: list[LegacyDevCleanupTarget] = []
    for name in cleanup_request.target_names:
        target = candidates_by_name[name]
        for domain_id in target.domain_ids:
            _delete_domain(host=host, token=token, domain_id=domain_id, request=request)
        _delete_target(host=host, token=token, target=target, request=request)
        deleted_targets.append(target)
    deleted_names = {target.name for target in deleted_targets}
    return LegacyDevCleanupResult(
        apply=True,
        suffix=cleanup_request.suffix,
        candidate_targets=candidates,
        requested_names=cleanup_request.target_names,
        deleted_targets=tuple(deleted_targets),
        skipped_targets=tuple(target for target in candidates if target.name not in deleted_names),
    )


def _target_names_from_text(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Clean up legacy Dokploy -dev targets.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--suffix", default="-dev")
    parser.add_argument("--target-names", default="")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args(argv)
    host = os.environ.get("LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST", "").strip()
    token = os.environ.get("LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN", "").strip()
    if not host or not token:
        raise click.ClickException("Legacy dev cleanup requires Dokploy emergency secrets.")
    result = execute_legacy_dev_cleanup(
        host=host,
        token=token,
        cleanup_request=LegacyDevCleanupRequest(
            apply=args.apply,
            suffix=args.suffix,
            target_names=_target_names_from_text(args.target_names),
            confirmation=args.confirmation,
        ),
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
