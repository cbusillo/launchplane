from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GitHubJsonFetcher = Callable[[str, str], Mapping[str, object]]
_REPOSITORY_SCOPE_PATTERN = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


@dataclass(frozen=True)
class GitHubAppInstallationScopeEvidence:
    repository_count: int
    repository_sha256: str


def expected_runner_host_github_repositories(
    *,
    expected_owner: str,
    github_idle_bindings: str,
    generated_run_cache_roots: str,
) -> frozenset[str]:
    normalized_owner = expected_owner.strip().lower()
    if not normalized_owner:
        raise ValueError("GitHub App installation validation requires an owner.")
    repositories: set[str] = set()

    def add_repository(repository_scope: str) -> None:
        match = _REPOSITORY_SCOPE_PATTERN.fullmatch(repository_scope.strip())
        if match is None:
            raise ValueError("GitHub evidence repository must use owner/name.")
        owner, repository = match.groups()
        if owner.lower() != normalized_owner:
            raise ValueError("GitHub App evidence repositories must share the workflow owner.")
        repositories.add(f"{owner.lower()}/{repository.lower()}")

    for raw_binding in github_idle_bindings.split(","):
        binding = raw_binding.strip()
        if not binding:
            continue
        parts = binding.split("|")
        if len(parts) != 4:
            raise ValueError("GitHub idle binding must contain four fields.")
        add_repository(parts[0])

    for raw_root in generated_run_cache_roots.split(","):
        root = raw_root.strip()
        if not root:
            continue
        parts = root.split("|")
        if len(parts) != 6:
            raise ValueError("Generated cache root must contain six fields.")
        add_repository(parts[1])

    if not repositories:
        raise ValueError("At least one GitHub evidence repository is required.")
    return frozenset(repositories)


def read_github_app_installation_repositories(
    *,
    api_url: str,
    bearer_token: str,
    fetch_json: GitHubJsonFetcher | None = None,
) -> frozenset[str]:
    normalized_api_url = api_url.strip().rstrip("/")
    normalized_token = bearer_token.strip()
    if not normalized_api_url or not normalized_token:
        raise ValueError("GitHub App installation validation requires API URL and token.")
    fetch = fetch_json or _fetch_github_json
    repositories: set[str] = set()
    total_count: int | None = None
    page = 1
    while True:
        query = urlencode({"per_page": 100, "page": page})
        payload = fetch(
            f"{normalized_api_url}/installation/repositories?{query}",
            normalized_token,
        )
        page_repositories = payload.get("repositories")
        payload_total_count = payload.get("total_count")
        if not isinstance(page_repositories, list) or not isinstance(payload_total_count, int):
            raise ValueError("GitHub App installation repository response is malformed.")
        if total_count is None:
            total_count = payload_total_count
        elif total_count != payload_total_count:
            raise ValueError("GitHub App installation repository count changed during read.")
        for repository in page_repositories:
            full_name = repository.get("full_name") if isinstance(repository, dict) else None
            if (
                not isinstance(full_name, str)
                or _REPOSITORY_SCOPE_PATTERN.fullmatch(full_name.strip()) is None
            ):
                raise ValueError("GitHub App installation repository entry is malformed.")
            repositories.add(full_name.strip().lower())
        if len(page_repositories) < 100 or len(repositories) >= payload_total_count:
            break
        page += 1
        if page > 100:
            raise ValueError("GitHub App installation repository response is truncated.")

    if total_count != len(repositories):
        raise ValueError("GitHub App installation repository response is incomplete.")
    return frozenset(repositories)


def validate_github_app_installation_scope(
    *,
    expected_owner: str,
    github_idle_bindings: str,
    generated_run_cache_roots: str,
    api_url: str,
    bearer_token: str,
    fetch_json: GitHubJsonFetcher | None = None,
) -> GitHubAppInstallationScopeEvidence:
    expected = expected_runner_host_github_repositories(
        expected_owner=expected_owner,
        github_idle_bindings=github_idle_bindings,
        generated_run_cache_roots=generated_run_cache_roots,
    )
    observed = read_github_app_installation_repositories(
        api_url=api_url,
        bearer_token=bearer_token,
        fetch_json=fetch_json,
    )
    expected_digest = _repository_scope_digest(expected)
    observed_digest = _repository_scope_digest(observed)
    if expected != observed:
        raise ValueError(
            "GitHub App installation repositories do not match runtime evidence scope: "
            f"expected_count={len(expected)} observed_count={len(observed)} "
            f"expected_sha256={expected_digest} observed_sha256={observed_digest}."
        )
    return GitHubAppInstallationScopeEvidence(
        repository_count=len(expected),
        repository_sha256=expected_digest,
    )


def _repository_scope_digest(repositories: frozenset[str]) -> str:
    return hashlib.sha256("\n".join(sorted(repositories)).encode()).hexdigest()[:16]


def _fetch_github_json(url: str, bearer_token: str) -> Mapping[str, object]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("GitHub App installation repository response is malformed.")
    return payload


def main() -> None:
    try:
        evidence = validate_github_app_installation_scope(
            expected_owner=os.environ.get("EXPECTED_OWNER", ""),
            github_idle_bindings=os.environ.get("RUNNER_GITHUB_IDLE_BINDINGS", ""),
            generated_run_cache_roots=os.environ.get("RUNNER_GENERATED_RUN_CACHE_ROOTS", ""),
            api_url=os.environ.get("GITHUB_API_URL", ""),
            bearer_token=os.environ.get("GH_TOKEN", ""),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        "GitHub App installation scope validated: "
        f"repositories={evidence.repository_count} "
        f"sha256={evidence.repository_sha256}"
    )


if __name__ == "__main__":
    main()
