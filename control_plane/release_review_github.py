"""GitHub adapter for the actual commits in a release, independent of milestones."""

import re
from collections.abc import Callable
from urllib.parse import quote

from control_plane.contracts.release_review import ReleaseReviewItem


GitHubRead = Callable[[str], object]


def owner_test_notes(body: str) -> str:
    """Read one Markdown heading, ignoring headings inside fenced examples."""
    lines: list[str] = []
    collecting = False
    level = 0
    fence = ""
    found = False
    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else marker if not fence else fence
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if not fence else None
        if heading:
            if heading[2].strip().casefold() == "owner test notes":
                if found:
                    raise ValueError("Pull request has more than one Owner test notes section.")
                found = collecting = True
                level = len(heading[1])
                continue
            if collecting and len(heading[1]) <= level:
                collecting = False
        if collecting:
            lines.append(line)
    return "\n".join(lines).strip()


def read_release_changes(
    *, repository: str, production_commit: str, candidate_commit: str, read: GitHubRead
) -> tuple[tuple[ReleaseReviewItem, ...], tuple[str, ...]]:
    repository_path = quote(repository, safe="/")
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
    for sha in commits:
        for page in range(1, 11):
            pulls = read(f"/repos/{repository_path}/commits/{sha}/pulls?per_page=100&page={page}")
            if not isinstance(pulls, list):
                raise ValueError("GitHub pull request coverage is unavailable.")
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
                try:
                    notes = owner_test_notes(pull.get("body") or "")
                except ValueError:
                    # Ambiguous notes are visible missing coverage, so an
                    # operator can still inspect the PR and record an override.
                    notes = ""
                item = ReleaseReviewItem(
                    pull_request_number=number,
                    title=title,
                    url=f"https://github.com/{repository}/pull/{number}",
                    head_sha=head.get("sha", ""),
                    merge_commit=pull["merge_commit_sha"],
                    owner_test_notes=notes,
                )
                if item.pull_request_number in items and items[item.pull_request_number] != item:
                    raise ValueError("Owner test notes changed while compiling the release.")
                items[item.pull_request_number] = item
                covered.add(sha)
            if len(pulls) < 100:
                break
        else:
            raise ValueError("GitHub pull request coverage exceeds the supported size.")
    return (
        tuple(items[number] for number in sorted(items)),
        tuple(sha for sha in commits if sha not in covered),
    )
