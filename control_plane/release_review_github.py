"""GitHub adapter for the actual commits in a release, independent of milestones."""

import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, urlsplit

from control_plane.client_test_notes import _markdown_lines, change_client_review
from control_plane.client_test_notes import owner_test_notes as owner_test_notes
from control_plane.contracts.release_review import ReleaseReviewItem
from control_plane.contracts.preview_record import PreviewRecord


GitHubRead = Callable[[str], object]


def recorded_preview_hosts(records: Iterable[PreviewRecord], *, repository: str) -> tuple[str, ...]:
    """A bare anchor name alone does not identify a product across owners/contexts."""
    return tuple(
        host
        for record in records
        if record.anchor_pr_url.rstrip("/")
        == f"https://github.com/{repository}/pull/{record.anchor_pr_number}"
        if (host := urlsplit(record.canonical_url).hostname)
    )


_NOTE_LINK = re.compile(
    r"!?\[[^]\n]*]\((?P<markdown>https?://[^\s)]+|/ui/owner-review\?[^\s)]+)\)"
    r"|<(?P<autolink>https?://[^\s<>]+)>"
    r"|(?P<bare>https?://[^\s<>\"'`\])]+|/ui/owner-review\?[^\s<>\"'`\])]+)",
    re.IGNORECASE,
)


def release_test_notes(notes: str, *, preview_hosts: tuple[str, ...] = ()) -> tuple[str, bool]:
    """Replace preview/revision-review links without changing the source PR body."""
    recorded_hosts = {host.casefold().rstrip(".") for host in preview_hosts if host}
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        url = next(value for value in match.groupdict().values() if value is not None)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        labels = host.split(".")
        preview = (
            host in recorded_hosts
            or any(label.endswith("-preview") for label in labels)
            or (bool(re.fullmatch(r"pr-\d+", labels[0])) and "preview" in labels)
        )
        query = parse_qs(parsed.query)
        per_pr_review = (
            parsed.path.rstrip("/") == "/ui/owner-review"
            and bool(query.get("repository"))
            and any(value.isdecimal() and int(value) > 0 for value in query.get("pull_request", ()))
        )
        if not preview and not per_pr_review:
            return match[0]
        changed = True
        return "\nCheck this on the testing site.\n"

    displayed = _NOTE_LINK.sub(replace, notes).strip()
    return displayed, changed


# Batch PRs written before the role words changed say "Owner"; both are read.
_MISSING_NOTES = re.compile(r"^#(\d+) has no (?:Client|Owner) test notes\.$", re.MULTILINE)


def missing_owner_test_notes(pull_request_number: int) -> str:
    """The line a merge-train batch PR carries for a constituent without notes."""
    return f"#{pull_request_number} has no Client test notes."


def pull_requests_missing_owner_test_notes(notes: str) -> tuple[int, ...]:
    """Constituents a batch PR's notes name as having none, so release review still blocks."""
    return tuple(int(match[1]) for match in _MISSING_NOTES.finditer(notes))


def nest_owner_test_notes(notes: str, *, min_heading_level: int) -> str:
    """Demote headings so collected notes stay inside an enclosing notes section.

    Headings keep their relative order down to level 6, and a fence left open
    is closed so it cannot swallow the enclosing document.
    """
    levels = [len(heading[1]) for _line, heading, _fence in _markdown_lines(notes) if heading]
    shift = max(0, min_heading_level - min(levels, default=min_heading_level))
    lines: list[str] = []
    fence = ""
    for line, heading, fence in _markdown_lines(notes):
        if heading:
            hashes = "#" * min(6, len(heading[1]) + shift)
            line = line[: heading.start(1)] + hashes + line[heading.end(1) :]
        lines.append(line)
    if fence:
        lines.append(fence)
    return "\n".join(lines)


def read_release_changes(
    *,
    repository: str,
    production_commit: str,
    candidate_commit: str,
    read: GitHubRead,
    preview_hosts: tuple[str, ...] = (),
    review_label: str = "owner-review",
) -> tuple[tuple[ReleaseReviewItem, ...], tuple[str, ...]]:
    repository_path = quote(repository)
    commits: list[str] = []
    total = None
    for page in range(1, 51):
        comparison = read(
            f"/repos/{repository_path}/compare/{production_commit}...{candidate_commit}"
            f"?per_page=100&page={page}"
        )
        if not isinstance(comparison, dict) or comparison.get("status") not in {
            "ahead",
            "identical",
        }:
            raise ValueError("Testing must descend from the commit currently in production.")
        count = comparison.get("total_commits")
        batch = comparison.get("commits")
        if not isinstance(count, int) or count < 0 or not isinstance(batch, list):
            raise ValueError("GitHub release comparison is incomplete.")
        if total is not None and count != total:
            raise ValueError("GitHub release comparison changed while reading pages.")
        total = count
        for commit in batch:
            sha = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha in commits:
                raise ValueError("GitHub release comparison contains invalid or repeated commits.")
            commits.append(sha)
        if len(commits) == total:
            break
        if not batch or len(commits) > total:
            raise ValueError("GitHub release comparison is incomplete.")
    else:
        raise ValueError("Release exceeds the supported comparison size.")

    commit_set = set(commits)
    items: dict[int, ReleaseReviewItem] = {}
    covered: set[str] = set()

    def read_commit_pulls(commit_sha: str) -> list[object]:
        result: list[object] = []
        for coverage_page in range(1, 11):
            batch_pulls = read(
                f"/repos/{repository_path}/commits/{commit_sha}/pulls"
                f"?per_page=100&page={coverage_page}"
            )
            if not isinstance(batch_pulls, list):
                raise ValueError("GitHub pull request coverage is unavailable.")
            result.extend(batch_pulls)
            if len(batch_pulls) < 100:
                return result
        raise ValueError("GitHub pull request coverage exceeds the supported size.")

    # Coverage reads are independent network requests. Bound concurrency, then
    # aggregate in commit order so validation and checklist digests stay stable.
    with ThreadPoolExecutor(max_workers=8) as executor:
        for sha, pulls in zip(commits, executor.map(read_commit_pulls, commits), strict=True):
            for pull in pulls:
                if not isinstance(pull, dict):
                    raise ValueError("GitHub returned an invalid pull request.")
                base = pull.get("base", {})
                if not isinstance(base, dict) or not isinstance(base.get("repo"), dict):
                    raise ValueError("GitHub pull request repository is unavailable.")
                if base["repo"].get("full_name", "").casefold() != repository.casefold():
                    continue
                if not pull.get("merged_at") or pull.get("merge_commit_sha") not in commit_set:
                    continue
                head = pull.get("head", {})
                if not isinstance(head, dict):
                    raise ValueError("GitHub pull request source is unavailable.")
                number = pull.get("number")
                title = pull.get("title")
                if (
                    not isinstance(number, int)
                    or number < 1
                    or not isinstance(title, str)
                    or not title
                ):
                    raise ValueError("GitHub pull request number or title is unavailable.")
                notes, preview_era_notes = release_test_notes(
                    owner_test_notes(pull.get("body") or ""), preview_hosts=preview_hosts
                )
                intent = change_client_review(
                    body=str(pull.get("body") or ""),
                    historical=True,
                    labels=(
                        str(label.get("name") or "")
                        for label in pull.get("labels", ())
                        if isinstance(label, dict)
                    ),
                    review_label=review_label,
                )
                item = ReleaseReviewItem(
                    client_test_notes_error=intent.error,
                    pull_request_number=number,
                    title=title,
                    url=f"https://github.com/{repository}/pull/{number}",
                    head_sha=head.get("sha", ""),
                    merge_commit=pull["merge_commit_sha"],
                    owner_test_notes=notes,
                    preview_era_notes=preview_era_notes,
                )
                if item.pull_request_number in items and items[item.pull_request_number] != item:
                    raise ValueError("Client test notes changed while compiling the release.")
                items[item.pull_request_number] = item
                covered.add(sha)
    return (
        tuple(items[number] for number in sorted(items)),
        tuple(sha for sha in commits if sha not in covered),
    )
