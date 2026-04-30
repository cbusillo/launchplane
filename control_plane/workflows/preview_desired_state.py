from pathlib import Path
from urllib.parse import quote

import click

from control_plane.contracts.preview_desired_state_record import (
    PreviewDesiredStateRecord,
    build_preview_desired_state_id,
)
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecycleDesiredPreview
from control_plane.workflows.launchplane import (
    fetch_github_pull_request_head,
    github_api_request,
    resolve_launchplane_github_token,
)


def _repository_parts(repository: str) -> tuple[str, str]:
    owner, separator, repo = repository.strip().partition("/")
    if not separator or not owner.strip() or not repo.strip() or "/" in repo.strip():
        raise click.ClickException("GitHub repository must use owner/repo format.")
    return owner.strip(), repo.strip()


def render_preview_slug(
    *,
    anchor_pr_number: int,
    preview_slug_prefix: str = "pr-",
    preview_slug_template: str = "",
) -> str:
    if preview_slug_template.strip():
        if "{number}" not in preview_slug_template:
            raise click.ClickException("Preview slug template must contain {number}.")
        return preview_slug_template.strip().replace("{number}", str(anchor_pr_number))
    return f"{preview_slug_prefix}{anchor_pr_number}"


def list_github_open_pull_requests_with_label(
    *,
    owner: str,
    repo: str,
    label: str,
    token: str,
    max_pages: int = 10,
) -> tuple[dict[str, object], ...]:
    per_page = 100
    pull_requests: list[dict[str, object]] = []
    for page in range(1, max_pages + 1):
        payload = github_api_request(
            path=(
                f"/repos/{owner}/{repo}/issues"
                f"?state=open&labels={quote(label.strip())}&per_page={per_page}&page={page}"
            ),
            token=token,
        )
        if not isinstance(payload, list):
            raise click.ClickException(
                f"GitHub issues response for {owner}/{repo} label {label!r} must be a list."
            )
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("pull_request"), dict):
                continue
            number = item.get("number")
            if not isinstance(number, int) or number <= 0:
                raise click.ClickException(
                    f"GitHub pull request candidate for {owner}/{repo} is missing a positive number."
                )
            head_sha, pr_url = fetch_github_pull_request_head(
                owner=owner,
                repo=repo,
                pr_number=number,
                token=token,
            )
            pull_requests.append(
                {
                    "number": number,
                    "html_url": pr_url,
                    "head_sha": head_sha,
                }
            )
        if len(payload) < per_page:
            break
    return tuple(sorted(pull_requests, key=lambda item: int(item["number"])))


def build_preview_desired_state_record(
    *,
    product: str,
    context: str,
    source: str,
    discovered_at: str,
    repository: str,
    label: str,
    anchor_repo: str,
    preview_slug_prefix: str,
    desired_previews: tuple[PreviewLifecycleDesiredPreview, ...],
    error_message: str = "",
) -> PreviewDesiredStateRecord:
    sorted_previews = tuple(sorted(desired_previews, key=lambda preview: preview.preview_slug))
    return PreviewDesiredStateRecord(
        desired_state_id=build_preview_desired_state_id(
            context_name=context,
            discovered_at=discovered_at,
        ),
        product=product,
        context=context,
        source=source,
        discovered_at=discovered_at,
        repository=repository,
        label=label,
        anchor_repo=anchor_repo,
        preview_slug_prefix=preview_slug_prefix,
        status="fail" if error_message.strip() else "pass",
        desired_count=0 if error_message.strip() else len(sorted_previews),
        desired_previews=() if error_message.strip() else sorted_previews,
        error_message=error_message.strip(),
    )


def discover_github_preview_desired_state(
    *,
    control_plane_root: Path,
    product: str,
    context: str,
    source: str,
    discovered_at: str,
    repository: str,
    label: str,
    anchor_repo: str,
    preview_slug_prefix: str = "pr-",
    preview_slug_template: str = "",
    max_pages: int = 10,
) -> PreviewDesiredStateRecord:
    try:
        owner, repo = _repository_parts(repository)
        github_token = resolve_launchplane_github_token(
            control_plane_root=control_plane_root,
            context_name=context,
        )
        if not github_token:
            raise click.ClickException(
                "Launchplane runtime records do not expose GITHUB_TOKEN for this context"
            )
        pull_requests = list_github_open_pull_requests_with_label(
            owner=owner,
            repo=repo,
            label=label,
            token=github_token,
            max_pages=max_pages,
        )
        desired_previews = tuple(
            PreviewLifecycleDesiredPreview(
                preview_slug=render_preview_slug(
                    anchor_pr_number=int(pull_request["number"]),
                    preview_slug_prefix=preview_slug_prefix,
                    preview_slug_template=preview_slug_template,
                ),
                anchor_repo=anchor_repo,
                anchor_pr_number=int(pull_request["number"]),
                anchor_pr_url=str(pull_request["html_url"]),
                head_sha=str(pull_request["head_sha"]),
            )
            for pull_request in pull_requests
        )
        return build_preview_desired_state_record(
            product=product,
            context=context,
            source=source,
            discovered_at=discovered_at,
            repository=repository,
            label=label,
            anchor_repo=anchor_repo,
            preview_slug_prefix=preview_slug_prefix,
            desired_previews=desired_previews,
        )
    except click.ClickException as exc:
        return build_preview_desired_state_record(
            product=product,
            context=context,
            source=source,
            discovered_at=discovered_at,
            repository=repository,
            label=label,
            anchor_repo=anchor_repo,
            preview_slug_prefix=preview_slug_prefix,
            desired_previews=(),
            error_message=str(exc),
        )
