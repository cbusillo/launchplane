from pathlib import Path
import unittest

from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    GitHubChangeImpactRepositoryEvidenceProvider,
)
from control_plane.change_impact_service import evaluate_change_impact
from control_plane.contracts.change_impact import (
    ChangeImpactChangedFileEvidence,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from tests.test_change_impact import _policy
from tests.test_change_impact_github import REPOSITORY, _GitHubApi


def _resolve(files: list[dict[str, object]]) -> ChangeImpactRepositoryEvidence:
    class GitHubApi(_GitHubApi):
        def __call__(self, *, path: str, token: str) -> object:
            if "/files?" in path:
                return files
            return super().__call__(path=path, token=token)

    return GitHubChangeImpactRepositoryEvidenceProvider(
        control_plane_root=Path("."),
        github_token=lambda **_: "server-token",
        github_api=GitHubApi(),
        token_context="launchplane",
    ).resolve(ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2000))


class ChangeImpactRenameTests(unittest.TestCase):
    def test_rename_pair_preserves_both_legacy_product_and_sensitive_boundaries(self) -> None:
        evidence = _resolve(
            [
                {
                    "filename": "src/runtime/rules.py",
                    "previous_filename": "control_plane/billing/rules.py",
                    "status": "renamed",
                }
            ]
        )
        self.assertEqual(
            evidence.changed_files,
            (
                ChangeImpactChangedFileEvidence(
                    path="control_plane/billing/rules.py", change_kind="removed"
                ),
                ChangeImpactChangedFileEvidence(
                    path="src/runtime/rules.py",
                    change_kind="renamed",
                    previous_path="control_plane/billing/rules.py",
                ),
            ),
        )
        impact = evaluate_change_impact(repository_evidence=evidence, policies=(_policy(),))
        self.assertEqual(
            (impact.status, impact.owner_impact, impact.required_engineering_review_count),
            ("success", "required", 2),
        )
        self.assertEqual(tuple(p.product for p in impact.affected_products), ("generic-web-a",))

    def test_rename_requires_nonempty_string_origin(self) -> None:
        origins: tuple[object, ...] = (None, "", " ", 123, [], {})
        for origin in origins:
            with (
                self.subTest(origin=origin),
                self.assertRaisesRegex(ChangeImpactRepositoryEvidenceError, "previous_filename"),
            ):
                _resolve(
                    [{"filename": "docs/new.md", "status": "renamed", "previous_filename": origin}]
                )

    def test_duplicate_real_destinations_cannot_overwrite_evidence(self) -> None:
        renamed: dict[str, object] = {
            "filename": "docs/new.md",
            "status": "renamed",
            "previous_filename": "src/old.py",
        }
        duplicates: tuple[dict[str, object], ...] = (
            renamed,
            {"filename": "docs/new.md", "status": "modified"},
            {"filename": "/docs/new.md", "status": "modified"},
        )
        for duplicate in duplicates:
            with (
                self.subTest(duplicate=duplicate),
                self.assertRaisesRegex(ChangeImpactRepositoryEvidenceError, "repeats a path"),
            ):
                _resolve([renamed, duplicate])

    def test_real_recreated_origin_keeps_its_kind_in_either_payload_order(self) -> None:
        renamed: dict[str, object] = {
            "filename": "b.py",
            "status": "renamed",
            "previous_filename": "a.py",
        }
        for kind in ("added", "modified", "removed"):
            real: dict[str, object] = {"filename": "a.py", "status": kind}
            for files in ([renamed, real], [real, renamed]):
                with self.subTest(kind=kind, files=files):
                    evidence = _resolve(files)
                    self.assertEqual(
                        [(f.path, f.change_kind, f.previous_path) for f in evidence.changed_files],
                        [("a.py", kind, None), ("b.py", "renamed", "a.py")],
                    )

    def test_swap_renames_retain_both_explicit_pairs(self) -> None:
        files: list[dict[str, object]] = [
            {"filename": "a.py", "status": "renamed", "previous_filename": "b.py"},
            {"filename": "b.py", "status": "renamed", "previous_filename": "a.py"},
        ]
        forward = _resolve(files)
        self.assertEqual(forward, _resolve(list(reversed(files))))
        self.assertEqual(
            [(f.path, f.change_kind, f.previous_path) for f in forward.changed_files],
            [("a.py", "renamed", "b.py"), ("b.py", "renamed", "a.py")],
        )
