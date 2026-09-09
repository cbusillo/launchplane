"""Candidate checks retain provider policy, integration and base bindings."""

from copy import deepcopy
import json
import unittest

from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_landing_checks import read_landing_checks
from control_plane.ordinary_agent_landing_graphql import OrdinaryLandingGraphQLObservation


class LandingChecksTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "ref": {
                "name": "main",
                "branchProtectionRule": {
                    "requiresStatusChecks": True,
                    "requiresStrictStatusChecks": True,
                    "requiredStatusChecks": [{"context": "ci", "app": {"databaseId": 100}}],
                },
                "compare": {
                    "status": "AHEAD",
                    "baseTarget": {"oid": "base"},
                    "headTarget": {"oid": "candidate"},
                },
            },
            "candidate": {
                "oid": "candidate",
                "statusCheckRollup": {
                    "contexts": {
                        "totalCount": 1,
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "__typename": "CheckRun",
                                "name": "ci",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                                "checkSuite": {"app": {"databaseId": 100}},
                            }
                        ],
                    }
                },
            },
        }

    def read(self, payload, rules=()):
        transport = DeadlineMergeTrainGitHubTransport(
            transport=RecordingMergeTrainGitHubTransport(responses=(list(rules),)),
            work_deadline=75,
            token_deadline=100,
            monotonic=lambda: 0,
        )
        return read_landing_checks(
            transport=transport,
            observation=OrdinaryLandingGraphQLObservation(
                observed_at=1000,
                repository_json=json.dumps(payload),
                identity_sha256="a" * 64,
            ),
            repository="example/project",
            base_branch="main",
            base_sha="base",
            candidate_sha="candidate",
        )

    def test_required_app_and_actual_base_control_candidate_readiness(self):
        checks, evidence = self.read(self.payload)
        self.assertEqual(checks.status, "pass")
        self.assertEqual(
            checks.required_checks[0].app_id, evidence.required_checks[0].integration_id
        )
        changed = deepcopy(self.payload)
        changed["candidate"]["statusCheckRollup"]["contexts"]["nodes"][0]["checkSuite"]["app"][
            "databaseId"
        ] = 200
        self.assertEqual(self.read(changed)[0].status, "unavailable")
        changed = deepcopy(self.payload)
        changed["ref"]["compare"]["status"] = "DIVERGED"
        self.assertEqual(self.read(changed)[0].status, "fail")

    def test_rules_add_required_checks_instead_of_replacing_classic_policy(self):
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [{"context": "security", "integration_id": 100}],
                },
            }
        ]
        checks, _ = self.read(self.payload, rules)
        self.assertEqual(checks.status, "unavailable")
        self.assertTrue(checks.strict)
        self.assertEqual({c.name for c in checks.required_checks}, {"ci", "security"})

    def test_missing_required_signals_and_unreadable_policy_never_pass(self):
        for field in ("missing_context", "truncated", "missing_policy", "wrong_candidate"):
            with self.subTest(field=field):
                changed = deepcopy(self.payload)
                if field == "missing_context":
                    contexts = changed["candidate"]["statusCheckRollup"]["contexts"]
                    contexts["nodes"] = []
                    contexts["totalCount"] = 0
                    self.assertEqual(self.read(changed)[0].status, "unavailable")
                    continue
                if field == "truncated":
                    changed["candidate"]["statusCheckRollup"]["contexts"]["pageInfo"][
                        "hasNextPage"
                    ] = True
                elif field == "missing_policy":
                    del changed["ref"]["branchProtectionRule"]
                else:
                    changed["candidate"]["oid"] = "other-candidate"
                with self.assertRaises(OrdinaryAgentProviderEvidenceError):
                    self.read(changed)

    def test_rules_only_policy_still_binds_integration(self):
        changed = deepcopy(self.payload)
        changed["ref"]["branchProtectionRule"] = None
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "ci", "integration_id": 100}],
                },
            }
        ]
        checks, evidence = self.read(changed, rules)
        self.assertEqual(checks.status, "pass")
        self.assertEqual(evidence.source, "evaluated_rules")
        self.assertIsNone(evidence.classic_sha256)
        self.assertEqual(self.read(changed)[0].status, "unavailable")

    def test_present_but_unreadable_required_app_cannot_become_any_app(self):
        for app in ({}, {"databaseId": None}):
            with self.subTest(app=app):
                changed = deepcopy(self.payload)
                changed["ref"]["branchProtectionRule"]["requiredStatusChecks"][0]["app"] = app
                with self.assertRaises(OrdinaryAgentProviderEvidenceError):
                    self.read(changed)

    def test_explicit_null_rollup_is_unavailable_but_missing_field_is_malformed(self):
        changed = deepcopy(self.payload)
        changed["candidate"]["statusCheckRollup"] = None
        self.assertEqual(self.read(changed)[0].status, "unavailable")
        del changed["candidate"]["statusCheckRollup"]
        with self.assertRaises(OrdinaryAgentProviderEvidenceError):
            self.read(changed)

    def test_documented_optional_rules_integration_id_allows_any_app(self):
        changed = deepcopy(self.payload)
        changed["ref"]["branchProtectionRule"] = None
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "ci"}],
                },
            }
        ]
        self.assertEqual(self.read(changed, rules)[0].status, "pass")

    def test_any_app_rule_does_not_weaken_same_context_classic_app_binding(self):
        changed = deepcopy(self.payload)
        changed["candidate"]["statusCheckRollup"]["contexts"]["nodes"][0]["checkSuite"]["app"][
            "databaseId"
        ] = 200
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [{"context": "ci"}],
                },
            }
        ]
        self.assertEqual(self.read(changed, rules)[0].status, "unavailable")
