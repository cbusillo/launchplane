"""Client review intent comes from explicit change notes, with a label override."""

from dataclasses import dataclass
import re
from typing import Iterable

from collections.abc import Iterator

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


NOTHING_TO_TEST = "Nothing for the Client to test"


@dataclass(frozen=True)
class ClientTestNotes:
    review_required: bool
    error: str = ""


def client_test_notes(notes: str, *, review_override: bool = False) -> ClientTestNotes:
    sections = re.split(r"(?m)^### #([0-9]+)[^\n]*\n", notes)
    if len(sections) > 1:
        members = tuple(
            client_test_notes(section, review_override=review_override)
            for section in sections[2::2]
        )
        return ClientTestNotes(
            any(member.review_required for member in members),
            next((member.error for member in members if member.error), ""),
        )
    # Template comments and empty placeholders are not instructions for a Client.
    value = re.sub(r"<!--[\s\S]*?-->", "", notes).strip()
    if not value or value.casefold() in {"none", "n/a", "todo", "tbd"}:
        return ClientTestNotes(True, "Client test notes are missing.")
    if value.casefold().startswith(NOTHING_TO_TEST.casefold()):
        if (
            not value.startswith(NOTHING_TO_TEST + ":")
            or not value[len(NOTHING_TO_TEST) + 1 :].strip()
        ):
            return ClientTestNotes(
                True, "Use Nothing for the Client to test: <reason> with a nonempty reason."
            )
        if review_override:
            return ClientTestNotes(True, "The Client review label requires real Client test notes.")
        return ClientTestNotes(False)
    return ClientTestNotes(True)


def change_client_review(*, body: str, labels: Iterable[str], review_label: str) -> ClientTestNotes:
    return client_test_notes(
        owner_test_notes(body),
        review_override=review_label.strip().casefold() in {label.casefold() for label in labels},
    )
