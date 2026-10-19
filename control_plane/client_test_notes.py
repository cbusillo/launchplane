"""Client review intent comes from the shared notes contract and a label override."""

from collections.abc import Iterable

from github_plan_status.client_test_notes import (
    LEGACY_NOTHING_TO_TEST as LEGACY_NOTHING_TO_TEST,
    NOTHING_TO_TEST as NOTHING_TO_TEST,
    TEST_NOTE_TITLES,
    ClientTestNotes as ClientTestNotes,
    _markdown_lines as _markdown_lines,
    body_client_test_notes,
    client_test_notes as client_test_notes,
)

TEST_NOTES_HEADINGS = frozenset(TEST_NOTE_TITLES)


class ClientTestNotesRequired(ValueError):
    """Readable notes explicitly failed validation, rather than a provider read."""


def owner_test_notes(body: str) -> str:
    """Collect Client test notes sections, ignoring headings inside fenced examples.

    Pull requests written before the role words changed say "Owner test notes"; both
    headings are read.
    """
    # Keep display whitespace: existing accepted checklist digests bind these notes.
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


def change_client_review(
    *,
    body: str,
    labels: Iterable[str],
    review_label: str,
    pure_dependency_update: bool = False,
    director_standing: bool = False,
) -> ClientTestNotes:
    override = review_label.strip().casefold() in {label.casefold() for label in labels}
    if pure_dependency_update and not override:
        return ClientTestNotes(False)
    intent = body_client_test_notes(body, review_override=override)
    return ClientTestNotes(False, intent.error) if director_standing else intent
