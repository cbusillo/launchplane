from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import TestCase

from scripts.verify_repository_pins import (
    UNTRUSTED_RUN_EXPRESSION_PATTERN,
    _requires_conjunct,
    _runner_expression_excludes_forks,
    workflow_violations,
)
from tests.support.workflows import load_workflow

_SAME_REPO_CONDITION = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


def _safe_workflow() -> str:
    return (
        '"on": [pull_request, pull_request_target]\n'
        "jobs:\n  arbitrary_job:\n"
        "    runs-on: [self-hosted, fixture]\n"
        f"    if: {_SAME_REPO_CONDITION}\n"
        f"    container: postgres:17@sha256:{'b' * 64}\n"
        "    steps:\n"
        f"      - uses: actions/checkout@{'a' * 40} # v1.0.0\n"
        '      - run: echo "$TITLE"\n'
        "        env:\n          TITLE: ${{ github.event.pull_request.title }}\n"
        f"      - run: docker run --volume /var/lib/postgresql/data:rw postgres:17@sha256:{'b' * 64}\n"
    )


class GitHubActionsSecurityTests(TestCase):
    def test_arbitrary_workflow_with_all_safety_properties_passes(self) -> None:
        self.assertEqual(
            workflow_violations(_safe_workflow(), path=Path(".github/workflows/unrelated.yaml")), []
        )

    def test_environment_names_are_not_action_or_script_fields(self) -> None:
        workflow = _safe_workflow() + "env:\n  uses: fixture-value\n  run: ${{ inputs.title }}\n"
        self.assertEqual(
            workflow_violations(workflow, path=Path(".github/workflows/unrelated.yml")), []
        )

    def test_unsafe_workflow_properties_are_refused(self) -> None:
        safe = _safe_workflow()
        cases = (
            ("mutable action", safe.replace("a" * 40, "main"), "action_reference_not_immutable"),
            (
                "untrusted checkout",
                safe.replace(
                    " # v1.0.0",
                    " # v1.0.0\n        with:\n          ref: ${{ github.event.pull_request.head.sha }}",
                ),
                "pull_request_target_checkout_untrusted",
            ),
            ("missing digest", safe.replace("@sha256:" + "b" * 64, ""), "container_digest_missing"),
            (
                "fork runner",
                safe.replace(f"    if: {_SAME_REPO_CONDITION}\n", ""),
                "runner_fork_isolation_missing",
            ),
            (
                "fork guard bypass",
                safe.replace(
                    f"if: {_SAME_REPO_CONDITION}",
                    f"if: ({_SAME_REPO_CONDITION}) || always()",
                ),
                "runner_fork_isolation_missing",
            ),
            (
                "unproven dynamic runner",
                safe.replace("[self-hosted, fixture]", "${{ matrix.runner }}").replace(
                    f"    if: {_SAME_REPO_CONDITION}\n", ""
                ),
                "runner_fork_isolation_missing",
            ),
            (
                "untrusted shell input",
                safe.replace('echo "$TITLE"', 'echo "${{ github.event.pull_request.title }}"'),
                "untrusted_run_expression",
            ),
            (
                "unclassified action",
                safe.replace("actions/checkout@", "unknown/action@"),
                "action_source_unclassified",
            ),
            (
                "unclassified image",
                safe.replace("postgres:17", "unknown/image:17"),
                "container_source_unclassified",
            ),
            (
                "service image",
                safe.replace(
                    "    steps:", "    services:\n      db:\n        image: postgres:17\n    steps:"
                ),
                "container_digest_missing",
            ),
            (
                "script image",
                safe.replace('echo "$TITLE"', "docker run postgres:17"),
                "container_digest_missing",
            ),
            (
                "embedded topology",
                safe.replace('echo "$TITLE"', "echo odoo-tenant-cm"),
                "product_topology_embedded",
            ),
        )
        for name, workflow, code in cases:
            with self.subTest(name=name):
                self.assertIn(
                    code,
                    {
                        v.code
                        for v in workflow_violations(
                            workflow, path=Path(".github/workflows/unrelated.yaml")
                        )
                    },
                )

    def test_local_request_action_requires_prior_trusted_checkout(self) -> None:
        safe = _safe_workflow().replace(
            '      - run: echo "$TITLE"',
            '      - uses: ./.github/actions/launchplane-request\n      - run: echo "$TITLE"',
        )
        self.assertEqual(
            workflow_violations(safe, path=Path(".github/workflows/unrelated.yml")), []
        )
        for workflow in (
            safe.replace("pull_request, pull_request_target", "workflow_call"),
            safe.replace(" # v1.0.0", " # v1.0.0\n        with:\n          ref: refs/heads/other"),
            safe.replace(
                "    steps:\n", "    steps:\n      - uses: ./.github/actions/launchplane-request\n"
            ),
        ):
            with self.subTest(workflow=workflow):
                self.assertIn(
                    "local_request_checkout_untrusted",
                    {
                        v.code
                        for v in workflow_violations(
                            workflow, path=Path(".github/workflows/unrelated.yml")
                        )
                    },
                )

    def test_guard_text_inside_a_string_does_not_isolate_forks(self) -> None:
        text = "'x && github.event.pull_request.head.repo.full_name == github.repository && y'"
        workflow = _safe_workflow().replace(
            f"if: {_SAME_REPO_CONDITION}", f'if: "{text} == {text}"'
        )
        self.assertIn(
            "runner_fork_isolation_missing",
            {
                item.code
                for item in workflow_violations(
                    workflow, path=Path(".github/workflows/unrelated.yml")
                )
            },
        )

    def test_trusted_pull_request_target_refs_pass(self) -> None:
        for ref in (
            "${{ github.sha }}",
            "${{ github.event.pull_request.base.sha }}",
            "refs/heads/reviewed-base",
            "c" * 40,
        ):
            with self.subTest(ref=ref):
                workflow = _safe_workflow().replace(
                    " # v1.0.0", f" # v1.0.0\n        with:\n          ref: {ref}"
                )
                self.assertEqual(
                    workflow_violations(workflow, path=Path(".github/workflows/unrelated.yml")), []
                )

    def test_reusable_checkout_of_pr_head_requires_excluding_target_event(self) -> None:
        for checkout in (
            "ref: ${{ github.event.pull_request.head.sha }}",
            "ref: ${{ github['event']['pull_request']['head']['sha'] }}",
            "ref: ${{ github . event . pull_request . head . sha }}",
            "ref: ${{ github.head_ref }}",
            "repository: ${{ github.event.pull_request.head.repo.full_name }}",
        ):
            workflow = (
                _safe_workflow()
                .replace("pull_request, pull_request_target", "workflow_call")
                .replace(" # v1.0.0", f" # v1.0.0\n        with:\n          {checkout}")
            )
            with self.subTest(checkout=checkout):
                self.assertIn(
                    "pull_request_target_checkout_untrusted",
                    {item.code for item in workflow_violations(workflow, path=Path("fixture.yml"))},
                )
                ordinary_only = workflow.replace(
                    f"if: {_SAME_REPO_CONDITION}",
                    "if: github.event_name != 'pull_request_target'",
                )
                self.assertEqual(workflow_violations(ordinary_only, path=Path("fixture.yml")), [])

    def test_browser_images_match_the_supplied_dependency_version(self) -> None:
        for version in ("9.8.7", "8.7.6"):
            workflow = _safe_workflow().replace(
                "postgres:17", f"mcr.microsoft.com/playwright:v{version}-noble"
            )
            with self.subTest(version=version):
                self.assertEqual(
                    workflow_violations(
                        workflow, path=Path("fixture.yml"), playwright_version=version
                    ),
                    [],
                )
                for dependency_version in (None, "0.0.1"):
                    self.assertIn(
                        "browser_image_version_mismatch",
                        {
                            item.code
                            for item in workflow_violations(
                                workflow,
                                path=Path("fixture.yml"),
                                playwright_version=dependency_version,
                            )
                        },
                    )

    def test_composites_share_checkout_and_request_action_trust(self) -> None:
        base = (
            "runs:\n  using: composite\n  steps:\n"
            f"    - uses: actions/checkout@{'a' * 40} # v1.0.0\n"
        )
        unsafe = base + "      with:\n        ref: ${{ github.event.pull_request.head.sha }}\n"
        for action, code in (
            (unsafe, "pull_request_target_checkout_untrusted"),
            (
                base + "    - uses: ./.github/actions/launchplane-request\n",
                "local_request_checkout_untrusted",
            ),
            (
                "runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/launchplane-request\n",
                "local_request_checkout_untrusted",
            ),
        ):
            with self.subTest(code=code, action=action):
                self.assertIn(
                    code,
                    {item.code for item in workflow_violations(action, path=Path("action.yml"))},
                )
        ordinary_only = unsafe.replace(
            "      with:", "      if: github.event_name != 'pull_request_target'\n      with:"
        )
        self.assertEqual(workflow_violations(ordinary_only, path=Path("action.yml")), [])
        remote_request = base + (
            "    - uses: cbusillo/launchplane/.github/actions/launchplane-request@"
            f"{'a' * 40} # launchplane-request\n"
        )
        self.assertEqual(workflow_violations(remote_request, path=Path("action.yml")), [])

    def test_one_bad_image_is_reported_once(self) -> None:
        workflow = _safe_workflow().replace("@sha256:" + "b" * 64, "", 1)
        self.assertEqual(
            [item.code for item in workflow_violations(workflow, path=Path("fixture.yml"))],
            ["container_digest_missing"],
        )

    def test_static_runner_choice_requires_trust_before_self_hosted_selection(self) -> None:
        guard = f"({_SAME_REPO_CONDITION})"
        for condition, fallback, safe in (
            (f"{guard} && startsWith(github.head_ref, 'candidate/')", '["ubuntu-latest"]', True),
            ("startsWith(github.head_ref, 'candidate/')", '["ubuntu-latest"]', False),
            (f"{guard} || startsWith(github.head_ref, 'candidate/')", '["ubuntu-latest"]', False),
            (guard, '["self-hosted","general"]', False),
        ):
            expression = (
                "${{ fromJSON("
                + condition
                + ' && \'["self-hosted","reserved"]\' || \''
                + fallback
                + "') }}"
            )
            with self.subTest(condition=condition, fallback=fallback):
                self.assertEqual(_runner_expression_excludes_forks(expression), safe)

    def test_same_repository_guard_must_hold_for_the_whole_condition(self) -> None:
        guard = f"({_SAME_REPO_CONDITION})"
        for expression, required in (
            (f"needs.a.outputs.verified != 'true' && {guard}", True),
            (f"{guard} && always()", True),
            (f"({guard})", True),
            (f"{guard} || needs.a.outputs.verified != 'true'", False),
            (f"(needs.a.outputs.verified != 'true' || {guard}) && always()", False),
            ("needs.a.outputs.verified != 'true'", False),
            (f"${{{{ needs.a.outputs.verified != 'true' && {guard} }}}}", True),
            (f"(always() && {guard}) && needs.a.result == 'success'", True),
            (f"(always() || {guard}) && needs.a.result == 'success'", False),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(_requires_conjunct(expression, guard), required)

    def test_untrusted_expression_pattern_covers_index_and_multiline_forms(self) -> None:
        for script, matches in (
            ('echo "${{ inputs.command }}"', True),
            ("echo \"${{ inputs['command'] }}\"", True),
            ("echo \"${{ github['event']['pull_request']['title'] }}\"", True),
            ('echo "${{\n  inputs.command }}"', True),
            ('echo "${{ github.head_ref }}"', True),
            ('echo "${{ github.event_name }}"', False),
            ('echo "${{ steps.request.outputs.url }}"', False),
            ('echo "$INPUT_COMMAND"', False),
        ):
            with self.subTest(script=script):
                self.assertEqual(
                    UNTRUSTED_RUN_EXPRESSION_PATTERN.search(script) is not None, matches
                )

    def test_product_repo_config_authority_revision_validation_fails_closed(self) -> None:
        workflow = load_workflow(".github/workflows/reusable-product-repo-config-authority.yml")
        step = workflow.step_named(
            "launchplane-config-authority",
            "Validate Launchplane audit tool revision",
        )
        assert step is not None

        revision = "a" * 40
        cases: tuple[tuple[str, str, dict[str, object], int], ...] = (
            ("valid", revision, {"workflow_sha": revision}, 0),
            ("missing", revision, {}, 1),
            ("non-string", revision, {"workflow_sha": 123}, 1),
            ("malformed", revision, {"workflow_sha": "a" * 39}, 1),
            ("uppercase", revision, {"workflow_sha": "A" * 40}, 1),
            ("non-hex", revision, {"workflow_sha": "g" * 40}, 1),
            ("trailing-newline", revision, {"workflow_sha": f"{revision}\n"}, 1),
            ("mismatch", revision, {"workflow_sha": "b" * 40}, 1),
        )
        for case_name, input_revision, job_context, expected_exit_code in cases:
            with self.subTest(case_name=case_name):
                result = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-e",
                        "-o",
                        "pipefail",
                        "-c",
                        step.run,
                    ],
                    capture_output=True,
                    env=os.environ
                    | {
                        "JOB_CONTEXT_JSON": json.dumps(job_context),
                        "LAUNCHPLANE_REVISION": input_revision,
                    },
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    expected_exit_code,
                    f"stdout={result.stdout}\nstderr={result.stderr}",
                )
