"""GitHub adapter for the actual commits in a release, independent of milestones."""

import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from control_plane.contracts.release_review import ReleaseReviewItem


GitHubRead = Callable[[str], object]


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown_lines(body: str) -> Iterator[tuple[str, re.Match[str] | None, str]]:
    """Yield each line, its heading match outside fenced code, and the open fence.

    Fences follow CommonMark: three or more backticks or tildes, closed only by
    the same character repeated at least as many times with nothing after it.
    """
    fence = ""
    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker, rest = fence_match[1], fence_match[2]
            if not fence and not (marker[0] == "`" and "`" in rest):
                fence = marker
            elif fence and marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = ""
        heading = _HEADING.match(line) if not fence else None
        yield line, heading, fence


TEST_NOTES_HEADINGS = frozenset({"client test notes", "owner test notes"})


def owner_test_notes(body: str) -> str:
    """Collect Client test notes sections, ignoring headings inside fenced examples.

    Pull requests written before the role words changed say "Owner test notes"; both
    headings are read.
    """
    lines: list[str] = []
    collecting = False
    level = 0
    for line, heading, _fence in _markdown_lines(body):
        if heading:
            if heading[2].strip().casefold() in TEST_NOTES_HEADINGS:
                collecting = True
                level = len(heading[1])
                continue
            if collecting and len(heading[1]) <= level:
                collecting = False
        if collecting:
            lines.append(line)
    return "\n".join(lines).strip()


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
    *, repository: str, production_commit: str, candidate_commit: str, read: GitHubRead
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
                item = ReleaseReviewItem(
                    pull_request_number=number,
                    title=title,
                    url=f"https://github.com/{repository}/pull/{number}",
                    head_sha=head.get("sha", ""),
                    merge_commit=pull["merge_commit_sha"],
                    owner_test_notes=owner_test_notes(pull.get("body") or ""),
                )
                if item.pull_request_number in items and items[item.pull_request_number] != item:
                    raise ValueError("Client test notes changed while compiling the release.")
                items[item.pull_request_number] = item
                covered.add(sha)
    return (
        tuple(items[number] for number in sorted(items)),
        tuple(sha for sha in commits if sha not in covered),
    )
