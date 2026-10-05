import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast

from click.testing import CliRunner

from tests.test_config_authority_audit import CLI_MAIN, _commit_all, _git, _init_repo


class LaunchplaneMetadataRoutingTests(unittest.TestCase):
    def _gate(self, metadata: object, *, path: str = ".github/github.json") -> dict[str, object]:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("# Product\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            source = root / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(json.dumps(metadata))
            _commit_all(root)
            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--base-sha",
                    base,
                    "--head-sha",
                    _git(root, "rev-parse", "HEAD"),
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )
        payload: dict[str, object] = json.loads(result.stdout)
        self.assertEqual(
            result.exit_code,
            0 if cast("dict[str, object]", payload["gate"])["status"] == "pass" else 1,
        )
        return payload

    def test_catalog_routing_passes_with_explicit_allow_evidence(self) -> None:
        payload = self._gate(
            {
                "launchplane": {
                    "enabled": True,
                    "service": {
                        "contextUrlEnv": "LAUNCHPLANE_CONTEXT_URL",
                        "operatorUrlEnv": "LAUNCHPLANE_OPERATOR_URL",
                        "localConfigExample": "launchplane/references/launchplane-operator.local.example.json",
                    },
                    "context": {
                        "enabled": True,
                        "helper": "launchplane/scripts/launchplane-context.py",
                    },
                    "operator": {
                        "enabled": True,
                        "helper": "launchplane/scripts/launchplane-write-action.py",
                        "requiresPrivateConfig": True,
                    },
                    "mergeTrain": {
                        "enabled": True,
                        "controller": True,
                        "readyLabel": "ready-to-merge",
                        "baseBranch": "main",
                        "githubActionsRunner": {
                            "repo": "example/train-tools",
                            "workflow": "merge-train-runner.yml",
                            "ref": "main",
                            "runnerMode": "controller",
                            "mutateDefault": False,
                            "revisionEvidenceFields": {
                                "runnerWorkflow": "workflow_run.head_sha",
                                "candidate": "result.candidate.candidate_sha",
                                "landing": "result.landing_plan.entries[].merge_commit_sha",
                            },
                        },
                    },
                },
            }
        )
        self.assertEqual(cast("dict[str, object]", payload["gate"])["status"], "pass")
        findings = payload["findings"]
        assert isinstance(findings, list)
        self.assertTrue(findings)
        for finding in findings:
            self.assertEqual(finding["classification"], "allowed")
            self.assertEqual(finding["allow_reason"], "repo_metadata_ergonomics")

    def test_runtime_values_unknown_fields_and_invalid_routing_still_fail(self) -> None:
        cases = [
            {"service": {"url": "https://control.example.test"}},
            {"service": {"contextUrlEnv": "https://control.example.test"}},
            {"service": {"localConfigExample": "/private/credentials.json"}},
            {"context": {"helper": "../../private/credentials.py"}},
            {"context": "testing"},
            {"operator": {"helper": "https://control.example.test/admin"}},
            {"operator": {"enabled": "true", "requiresPrivateConfig": 1}},
            {"operator": {"token": "secret-value"}},
            {"mergeTrain": {"githubActionsRunner": {"repo": "https://host/example/tools"}}},
            {"mergeTrain": {"githubActionsRunner": {"repo": "example/tools/runtime"}}},
            {"mergeTrain": {"githubActionsRunner": {"repo": ["example/tools"]}}},
            {"mergeTrain": {"githubActionsRunner": {"workflow": "../private.yml"}}},
            {"mergeTrain": {"githubActionsRunner": {"ref": "../production"}}},
            {"mergeTrain": {"githubActionsRunner": {"runnerMode": "production"}}},
            {
                "mergeTrain": {
                    "githubActionsRunner": {"revisionEvidenceFields": {"candidate": "a" * 40}}
                }
            },
            {"mergeTrain": {"readyLabel": "https://control.example.test"}},
            {"mergeTrain": {"readyLabel": "control.example.test:8443/admin"}},
            {"mergeTrain": {"readyLabel": "10.0.0.5:8069/web"}},
            {"mergeTrain": {"readyLabel": "/private/credentials.json"}},
            {"product": "real-product"},
            {"publicName": "Real Product"},
            {"repository": "example/runtime"},
            {"provider": "provider-live"},
            {"lane": "production"},
            {"unknown": "innocent-looking"},
            {"notes": ["unknown field"]},
        ]
        for routing in cases:
            with self.subTest(routing=routing):
                payload = self._gate({"launchplane": routing})
                self.assertEqual(cast("dict[str, object]", payload["gate"])["status"], "fail")
                self.assertTrue(cast("dict[str, object]", payload["gate"])["rejected_findings"])

    def test_catalog_root_paths_and_namespaced_labels_pass(self) -> None:
        payload = self._gate(
            {
                "launchplane": {
                    "context": {"helper": "skills/launchplane/scripts/launchplane-context.py"},
                    "operator": {
                        "helper": "skills/launchplane/scripts/launchplane-write-action.py"
                    },
                    "service": {
                        "localConfigExample": (
                            "skills/launchplane/references/launchplane-operator.local.example.json"
                        ),
                    },
                    "mergeTrain": {"readyLabel": "status/ready"},
                },
            }
        )
        self.assertEqual(cast("dict[str, object]", payload["gate"])["status"], "pass")

        for label in (
            "merge: ready",
            "🚀 ready",
            "Ready to merge!",
            "merge (ready)",
            "ready+merge",
        ):
            with self.subTest(label=label):
                namespaced_label = self._gate(
                    {
                        "launchplane": {
                            "mergeTrain": {
                                "readyLabel": label,
                                "baseBranch": "release+hotfix",
                                "githubActionsRunner": {"ref": "feature@2"},
                            }
                        }
                    }
                )
                self.assertEqual(
                    cast("dict[str, object]", namespaced_label["gate"])["status"], "pass"
                )

    def test_workflow_repository_allowance_is_scoped_to_metadata_field(self) -> None:
        routing = {
            "launchplane": {"mergeTrain": {"githubActionsRunner": {"repo": "example/train-tools"}}}
        }
        allowed = self._gate(routing)
        self.assertEqual(cast("dict[str, object]", allowed["gate"])["status"], "pass")
        other_file = self._gate(routing, path="runtime.json")
        self.assertEqual(cast("dict[str, object]", other_file["gate"])["status"], "fail")
        other_key = self._gate({"launchplane": {"runtime": {"repo": "example/train-tools"}}})
        self.assertEqual(cast("dict[str, object]", other_key["gate"])["status"], "fail")
