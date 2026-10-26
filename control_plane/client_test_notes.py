"""Client review intent comes from the shared notes contract and a label override."""

from collections.abc import Iterable

from github_plan_status.client_test_notes import (
    LEGACY_NOTHING_TO_TEST as LEGACY_NOTHING_TO_TEST,
    NOTHING_TO_TEST as NOTHING_TO_TEST,
    ClientTestNotes as ClientTestNotes,
    _markdown_lines as _markdown_lines,
    body_client_test_notes,
    client_test_notes as client_test_notes,
    open_client_questions as open_client_questions,
    owner_test_notes as owner_test_notes,
)


class ClientTestNotesRequired(ValueError):
    """Readable notes explicitly failed validation, rather than a provider read."""


def change_client_review(
    *,
    body: str,
    labels: Iterable[str],
    review_label: str,
    pure_dependency_update: bool = False,
    director_standing: bool = False,
) -> ClientTestNotes:
    override = review_label.strip().casefold() in {label.casefold() for label in labels}
    if pure_dependency_update and not override and not open_client_questions(body):
        return ClientTestNotes(False)
    intent = body_client_test_notes(body, review_override=override)
    if director_standing and not override:
        return ClientTestNotes(False, intent.error)
    return intent
