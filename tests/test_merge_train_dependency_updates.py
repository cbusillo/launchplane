import unittest

from control_plane.merge_train_dependency_updates import classify_dependency_update

# Shapes copied from real Dependabot commits (2026-09-30).
INDIRECT_PATCH = """build(deps-dev): bump brace-expansion from 5.0.9 to 5.0.12 in /frontend

Bumps [brace-expansion](https://github.com/juliangruber/brace-expansion) from 5.0.9 to 5.0.12.
- [Release notes](https://github.com/juliangruber/brace-expansion/releases)

---
updated-dependencies:
- dependency-name: brace-expansion
  dependency-version: 5.0.12
  dependency-type: indirect
...

Signed-off-by: dependabot[bot] <support@github.com>"""

GROUPED_TWO_VERSIONS = """build(deps): bump brace-expansion

Bumps  and [brace-expansion](https://github.com/juliangruber/brace-expansion). These dependencies needed to be updated together.

Updates `brace-expansion` from 5.0.9 to 5.0.12
- [Release notes](https://github.com/juliangruber/brace-expansion/releases)

Updates `brace-expansion` from 1.1.18 to 1.1.21
- [Release notes](https://github.com/juliangruber/brace-expansion/releases)

---
updated-dependencies:
- dependency-name: brace-expansion
  dependency-version: 5.0.12
  dependency-type: indirect
- dependency-name: brace-expansion
  dependency-version: 1.1.21
  dependency-type: indirect
...

Signed-off-by: dependabot[bot] <support@github.com>"""

SECURITY_GROUP_MINOR = """chore(deps): bump pyjwt

Bumps the all-security-updates group with 1 update in the / directory: [pyjwt](https://github.com/jpadilla/pyjwt).


Updates `pyjwt` from 2.13.0 to 2.14.0
- [Release notes](https://github.com/jpadilla/pyjwt/releases)

---
updated-dependencies:
- dependency-name: pyjwt
  dependency-version: 2.14.0
  dependency-type: indirect
  dependency-group: all-security-updates
...

Signed-off-by: dependabot[bot] <support@github.com>"""

MAJOR = """build(deps): bump nodemailer from 9.1.1 to 10.0.9

Bumps [nodemailer](https://github.com/nodemailer/nodemailer) from 9.1.1 to 10.0.9.
- [Release notes](https://github.com/nodemailer/nodemailer/releases)

---
updated-dependencies:
- dependency-name: nodemailer
  dependency-version: 10.0.9
  dependency-type: direct:production
...

Signed-off-by: dependabot[bot] <support@github.com>"""


class DependencyUpdateClassificationTests(unittest.TestCase):
    def test_within_major_updates_qualify(self) -> None:
        for message in (INDIRECT_PATCH, GROUPED_TWO_VERSIONS, SECURITY_GROUP_MINOR):
            with self.subTest(message=message.splitlines()[0]):
                self.assertEqual(classify_dependency_update([message]), "patch_or_minor")

    def test_major_version_needs_review(self) -> None:
        self.assertEqual(classify_dependency_update([MAJOR]), "needs_review")
        self.assertEqual(
            classify_dependency_update([INDIRECT_PATCH, MAJOR]),
            "needs_review",
        )

    def test_a_major_update_type_in_the_trailer_needs_review(self) -> None:
        message = INDIRECT_PATCH.replace(
            "  dependency-type: indirect\n",
            "  dependency-type: indirect\n  update-type: version-update:semver-major\n",
        )
        self.assertEqual(classify_dependency_update([message]), "needs_review")

    def test_pre_one_minor_bump_needs_review(self) -> None:
        message = INDIRECT_PATCH.replace("5.0.9", "0.4.9").replace("5.0.12", "0.5.0")
        self.assertEqual(classify_dependency_update([message]), "needs_review")

    def test_unprovable_versions_need_review(self) -> None:
        sha_pin = INDIRECT_PATCH.replace("5.0.9", "3d3c42e").replace("5.0.12", "de0fac2")
        suffix_change = INDIRECT_PATCH.replace("5.0.9", "5.0.9-slim")
        no_trailer = INDIRECT_PATCH.split("---")[0]
        for message in (sha_pin, suffix_change, no_trailer):
            with self.subTest(message=message.splitlines()[0]):
                self.assertEqual(classify_dependency_update([message]), "needs_review")
        self.assertEqual(classify_dependency_update([]), "needs_review")
