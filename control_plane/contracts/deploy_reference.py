from __future__ import annotations

import re

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FLOATING_TAGS = frozenset(
    {
        "dev",
        "development",
        "latest",
        "main",
        "master",
        "prod",
        "production",
        "stable",
        "testing",
    }
)


def docker_image_repository(image_reference: str) -> str:
    normalized_reference = image_reference.strip()
    if "@" in normalized_reference:
        return normalized_reference.split("@", 1)[0].rstrip("/")
    last_component = normalized_reference.rsplit("/", 1)[-1]
    if ":" not in last_component:
        return normalized_reference.rstrip("/")
    return normalized_reference[: -len(last_component.rsplit(":", 1)[-1]) - 1].rstrip("/")


def docker_image_tag(image_reference: str) -> str:
    normalized_reference = image_reference.strip()
    if "@" in normalized_reference:
        return ""
    last_component = normalized_reference.rsplit("/", 1)[-1]
    if ":" not in last_component:
        return ""
    return last_component.rsplit(":", 1)[-1].strip()


def docker_image_digest(image_reference: str) -> str:
    normalized_reference = image_reference.strip()
    if "@" not in normalized_reference:
        return normalized_reference if _SHA256_DIGEST_RE.fullmatch(normalized_reference) else ""
    digest = normalized_reference.rsplit("@", 1)[-1].strip()
    return digest if _SHA256_DIGEST_RE.fullmatch(digest) else ""


def is_digest_pinned_image_reference(image_reference: str) -> bool:
    return bool(docker_image_digest(image_reference))


def is_non_floating_tag_reference(image_reference: str) -> bool:
    tag = docker_image_tag(image_reference)
    return bool(tag) and tag.casefold() not in _FLOATING_TAGS


def provider_image_reference(*, artifact_id: str, deploy_reference: str = "") -> str:
    return deploy_reference.strip() or artifact_id.strip()


def validate_provider_deploy_reference(
    *,
    artifact_id: str,
    deploy_reference: str,
    label: str,
) -> str:
    normalized_deploy_reference = deploy_reference.strip()
    if not normalized_deploy_reference:
        return ""
    normalized_artifact_id = artifact_id.strip()
    if not is_digest_pinned_image_reference(normalized_artifact_id):
        raise ValueError(
            f"{label} deploy_reference requires artifact_id to be a digest-pinned image "
            "reference such as repo@sha256:<digest>."
        )
    if not is_non_floating_tag_reference(normalized_deploy_reference):
        raise ValueError(
            f"{label} deploy_reference must be an immutable, non-floating image tag "
            "such as repo:sha-<commit>; digest references and floating tags like latest "
            "are not valid provider deploy references."
        )
    artifact_repository = docker_image_repository(normalized_artifact_id)
    deploy_repository = docker_image_repository(normalized_deploy_reference)
    if artifact_repository != deploy_repository:
        raise ValueError(
            f"{label} deploy_reference must use the same image repository as artifact_id. "
            f"Artifact repository={artifact_repository!r}; deploy repository={deploy_repository!r}."
        )
    return normalized_deploy_reference
