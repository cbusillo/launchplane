import unittest

from control_plane.source_control_change import change_fingerprint

_PATCH = "@@ -79,3 +79,4 @@ Palette\n Colors\n-Old headline\n+New headline\n+Second line"
# A base edit above the change moved it and changed its context; the change is the same.
_MOVED_PATCH = (
    "@@ -133,3 +133,4 @@ Brand\n Brand colors\n-Old headline\n+New headline\n+Second line"
)


def _fingerprint(patch: str, blob: str, **options: bool) -> object:
    def read(_path: str) -> object:
        return {
            "files": [{"filename": "README.md", "status": "modified", "sha": blob, "patch": patch}]
        }

    return change_fingerprint(
        repository="every/example", base="b" * 40, head="h" * 40, read=read, **options
    )


class ChangeFingerprintTests(unittest.TestCase):
    def test_only_changed_lines_mode_ignores_moved_hunks_and_context(self) -> None:
        self.assertEqual(
            _fingerprint(_PATCH, "a" * 40, changed_lines_only=True),
            _fingerprint(_MOVED_PATCH, "c" * 40, changed_lines_only=True),
        )

    def test_patch_modes_still_compare_hunk_positions_and_context(self) -> None:
        # Dependency refreshes keep the stricter rule: a moved hunk needs review.
        for options in ({}, {"include_blobs": False}):
            with self.subTest(**options):
                self.assertNotEqual(
                    _fingerprint(_PATCH, "a" * 40, **options),
                    _fingerprint(_MOVED_PATCH, "a" * 40, **options),
                )


if __name__ == "__main__":
    unittest.main()
