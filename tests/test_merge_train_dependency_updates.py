import json
import unittest

from control_plane.merge_train_dependency_updates import (
    classify_dependency_update as classify_update,
)


def classify_dependency_update(
    messages: list[str],
    *,
    metadata_format: str = "dependabot_trailer",
    allowed_update_types: tuple[str, ...] = ("patch", "minor", "digest"),
) -> str:
    from control_plane.contracts.merge_train_policy import MergeTrainDependencyBotPolicy

    bot = MergeTrainDependencyBotPolicy.model_validate(
        {
            "github_user_id": 123,
            "metadata_format": metadata_format,
            "allowed_update_types": allowed_update_types,
        }
    )
    return classify_update(
        messages, metadata_format=bot.metadata_format, allowed_update_types=bot.allowed_update_types
    )


# Representative Dependabot commit shapes.
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

SCOPED_MINOR = """build(deps): bump the routine-version-updates group

Updates `@types/react` from 19.2.14 to 19.3.0

---
updated-dependencies:
- dependency-name: "@types/react"
  dependency-version: 19.3.0
  dependency-type: direct:development
  update-type: version-update:semver-minor
...
"""


class DependencyUpdateClassificationTests(unittest.TestCase):
    def test_within_major_updates_qualify(self) -> None:
        for message in (INDIRECT_PATCH, GROUPED_TWO_VERSIONS, SECURITY_GROUP_MINOR):
            with self.subTest(message=message.splitlines()[0]):
                self.assertEqual(classify_dependency_update([message]), "patch_or_minor")

    def test_quoted_trailer_versions_still_match(self) -> None:
        message = (
            INDIRECT_PATCH.replace("5.0.9", "2025.1")
            .replace("5.0.12", "2025.2")
            .replace("dependency-version: 2025.2", "dependency-version: '2025.2'")
        )
        self.assertEqual(classify_dependency_update([message]), "patch_or_minor")

    def test_quoted_scoped_names_qualify_with_matching_version_proof(self) -> None:
        for name in ('"@types/react"', "'@types/react'"):
            message = SCOPED_MINOR.replace('"@types/react"', name)
            with self.subTest(name=name):
                self.assertEqual(classify_dependency_update([message]), "patch_or_minor")

    def test_quoted_names_do_not_bypass_version_proof(self) -> None:
        major = SCOPED_MINOR.replace("19.3.0", "20.0.0")
        missing_version = SCOPED_MINOR.replace("  dependency-version: 19.3.0\n", "")
        for message in (major, missing_version):
            with self.subTest(message=message):
                self.assertEqual(classify_dependency_update([message]), "needs_review")

    def test_incomplete_name_quotes_do_not_match_unquoted_version_proof(self) -> None:
        for name in ('"@types/react', "'@types/react", "\"@types/react'"):
            message = SCOPED_MINOR.replace('"@types/react"', name)
            with self.subTest(name=name):
                self.assertEqual(classify_dependency_update([message]), "needs_review")

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


def renovate_message(**updates: str) -> str:
    entry = {
        "depName": "example/package",
        "updateType": "minor",
        "currentVersion": "2.1.0",
        "newVersion": "2.2.0",
        "currentValue": "main",
        "newValue": "main",
        "currentDigest": "a" * 40,
        "newDigest": "b" * 40,
    } | updates
    return "Update dependency\n\nRenovate-Updates: " + json.dumps([entry])


class RenovateClassificationTests(unittest.TestCase):
    def test_same_safety_rule_for_versions_and_digest_pins(self) -> None:
        for changes, expected in (
            ({}, "patch_or_minor"),
            ({"updateType": "patch", "newVersion": "2.1.1"}, "patch_or_minor"),
            ({"updateType": "digest"}, "patch_or_minor"),
            ({"updateType": "major", "newVersion": "3.0.0"}, "needs_review"),
            ({"newVersion": "3.0.0"}, "needs_review"),
            ({"updateType": "unknown"}, "needs_review"),
            ({"currentVersion": "0.1.0", "newVersion": "0.2.0"}, "needs_review"),
            ({"updateType": "digest", "newValue": "other-branch"}, "needs_review"),
            ({"updateType": "digest", "currentValue": ""}, "needs_review"),
            ({"updateType": "digest", "newDigest": "missing"}, "needs_review"),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    classify_dependency_update(
                        [renovate_message(**changes)], metadata_format="renovate_metadata"
                    ),
                    expected,
                )

    def test_all_updates_and_commits_need_complete_metadata(self) -> None:
        safe = renovate_message()
        major = renovate_message(updateType="major")
        for messages in (
            [safe, major],
            [safe, "Update dependency"],
            [safe + "\nRenovate-Updates: []"],
            ["Renovate-Updates: invalid"],
            ["Renovate-Updates: [{}]"],
            ["Renovate-Updates: [null]"],
            ['Renovate-Updates: [{"depName": 3}]'],
            [safe[:-1] + "," + major.split("Renovate-Updates: [")[1]],
        ):
            with self.subTest(messages=messages):
                self.assertEqual(
                    classify_dependency_update(messages, metadata_format="renovate_metadata"),
                    "needs_review",
                )
        self.assertEqual(
            classify_dependency_update([INDIRECT_PATCH], metadata_format="renovate_metadata"),
            "needs_review",
        )
