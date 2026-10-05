import base64
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from pydantic import JsonValue
from unittest.mock import patch

from control_plane.config_authority_audit import (
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from control_plane.product_config_authority_events import (
    request_product_config_authority_event,
    scan_product_config_authority_event,
)
from tests.test_config_authority_audit import _git, _init_repo, _commit_all
from tests.test_github_app_webhook import (
    _inventory,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record


class SnapshotTransport:
    """GitHub-shaped responses generated from real immutable Git objects."""

    def __init__(self, root: Path, repository: str) -> None:
        self.root = root
        self.repository = repository
        self.overrides: dict[str, object] = {}
        self.reads: list[str] = []
        self.pr: dict[str, object] = {}

    def get_json(self, path: str) -> object:
        self.reads.append(path)
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            return value
        prefix = f"/repos/{self.repository}"
        if path == prefix:
            return {"id": 424242, "owner": {"id": 1}, "full_name": self.repository}
        if path == f"{prefix}/pulls/7":
            return self.pr
        if path.startswith(f"{prefix}/git/commits/"):
            revision = path.rsplit("/", 1)[1]
            return {
                "sha": _git(self.root, "rev-parse", f"{revision}^{{commit}}"),
                "tree": {"sha": _git(self.root, "rev-parse", f"{revision}^{{tree}}")},
            }
        if path.startswith(f"{prefix}/git/trees/"):
            revision = path.rsplit("/", 1)[1].split("?")[0]
            entries = []
            for line in _git(self.root, "ls-tree", "-r", "-t", revision).splitlines():
                meta, filename = line.split("\t")
                mode, kind, sha = meta.split()
                entry: dict[str, object] = {
                    "path": filename,
                    "mode": mode,
                    "type": kind,
                    "sha": sha,
                }
                if kind == "blob":
                    entry["size"] = int(_git(self.root, "cat-file", "-s", sha))
                entries.append(entry)
            return {"sha": revision, "truncated": False, "tree": entries}
        if path.startswith(f"{prefix}/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            data = subprocess.run(
                ("git", "cat-file", "blob", sha), cwd=self.root, check=True, capture_output=True
            ).stdout
            return {
                "sha": sha,
                "size": len(data),
                "encoding": "base64",
                "content": base64.b64encode(data).decode(),
            }
        raise AssertionError(f"Unexpected source read: {path}")

    def get_bytes(self, path: str) -> bytes:
        raise AssertionError("No archive or working-tree reads allowed.")


class ConfigAuthorityEventTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _init_repo(self.root)
        _git(self.root, "remote", "add", "origin", "https://github.com/example/site.git")
        (self.root / "runtime.env").write_text("PRODUCT_DOMAIN=inherited.example\n")
        (self.root / "deleted.env").write_text("PRODUCT_DOMAIN=deleted.example\n")
        _commit_all(self.root)
        self.base = _git(self.root, "rev-parse", "HEAD")
        (self.root / "runtime.env").write_text(
            "PRODUCT_DOMAIN=inherited.example\n# harmless edit\n"
        )
        (self.root / "deleted.env").unlink()
        (self.root / "settings.json").write_text('{"provider_target_id":"target-new"}\n')
        _commit_all(self.root)
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.transport = SnapshotTransport(self.root, "example/site")

    def scan(
        self, event: str = "push", payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            scan_product_config_authority_event(
                transport=self.transport,
                inventory=_inventory(),
                event=event,
                payload=payload or {"before": self.base, "after": self.head},
            ),
        )

    def test_repository_names_share_cli_fixture_gate_and_ignore_dirty_files(self) -> None:
        for repository in (
            "cbusillo/repairshopr_api",
            "cbusillo/verireel",
            "cbusillo/sellyouroutboard",
        ):
            with self.subTest(repository=repository):
                self.transport.repository = repository
                inventory = _inventory(repository=repository)
                (self.root / "settings.json").write_text("{}\n")
                (self.root / "dirty.env").write_text("PRODUCT_DOMAIN=dirty.example\n")
                actual = scan_product_config_authority_event(
                    transport=self.transport,
                    inventory=inventory,
                    event="push",
                    payload={"before": self.base, "after": self.head},
                )
                audit = build_config_authority_audit(
                    control_plane_root=self.root,
                    mode="changed-files-gate",
                    base_sha=self.base,
                    head_sha=self.head,
                )
                self.assertEqual(
                    actual["gate"], evaluate_config_authority_gate(audit, profile="product-repo")
                )
                self.assertEqual(actual["coverage"], audit["coverage"])
                self.assertEqual(actual["hashes"], audit["hashes"])
                self.assertEqual(actual["status"], "fail")
                self.assertNotIn("target-new", json.dumps(actual))

    def test_only_source_edits_request_a_rescan(self) -> None:
        from control_plane.product_config_authority_events import config_authority_event_supported

        self.assertFalse(
            config_authority_event_supported(
                "pull_request", {"action": "edited", "changes": {"title": {"from": "old"}}}
            )
        )
        self.assertTrue(
            config_authority_event_supported(
                "pull_request", {"action": "edited", "changes": {"base": {"ref": {"from": "old"}}}}
            )
        )

    def test_pr_rereads_authoritative_pair_and_merge_group_uses_exact_pair(self) -> None:
        self.transport.pr = {
            "base": {"sha": self.base, "repo": {"id": 424242}},
            "head": {"sha": self.head},
        }
        actual = self.scan(
            "pull_request",
            {"number": 7, "pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}},
        )
        self.assertEqual((actual["base_sha"], actual["head_sha"]), (self.base, self.head))
        group = self.scan(
            "merge_group", {"merge_group": {"base_sha": self.base, "head_sha": self.head}}
        )
        self.assertEqual(actual["gate"], group["gate"])

    def test_same_commit_and_inherited_findings_pass(self) -> None:
        self.assertEqual(
            self.scan(payload={"before": self.base, "after": self.base})["status"], "pass"
        )
        actual = self.scan(payload={"before": self.head, "after": self.head})
        self.assertEqual(actual["status"], "pass")

    def test_missing_mismatched_truncated_and_corrupt_evidence_refuse(self) -> None:
        prefix = "/repos/example/site"
        tree = _git(self.root, "rev-parse", f"{self.head}^{{tree}}")
        blob = _git(self.root, "rev-parse", f"{self.head}:settings.json")
        cases = {
            prefix: {"id": 999, "owner": {"id": 1}, "full_name": "example/site"},
            f"{prefix}/git/commits/{self.base}": OSError("unavailable"),
            f"{prefix}/git/commits/{self.head}": {"sha": "a" * 40, "tree": {"sha": tree}},
            f"{prefix}/git/trees/{tree}?recursive=1": {"sha": tree, "truncated": True, "tree": []},
            f"{prefix}/git/blobs/{blob}": {
                "sha": blob,
                "size": len((self.root / "settings.json").read_bytes()),
                "encoding": "base64",
                "content": base64.b64encode(b"bad").decode(),
            },
        }
        for path, value in cases.items():
            with self.subTest(path=path):
                self.transport.overrides = {path: value}
                with self.assertRaises((ValueError, OSError)):
                    self.scan()
        self.transport.overrides = {}
        with self.assertRaises(ValueError):
            self.scan(payload={"before": "0" * 40, "after": self.head})

    def test_cached_blob_is_reused_but_conflicting_tree_size_refuses(self) -> None:
        from control_plane.product_config_authority_events import GitHubConfigAuthoritySource

        source = GitHubConfigAuthoritySource(self.transport, "example/site")
        source.resolve_commit(self.head)
        original = source.read_blob(self.head, "settings.json")
        source.read_blob(self.head, "settings.json")
        blob_reads = [path for path in self.transport.reads if "/git/blobs/" in path]
        self.assertEqual(len(blob_reads), 1)
        source.trees[self.head]["settings.json"]["size"] = len(original) + 1
        with self.assertRaises(ValueError):
            source.read_blob(self.head, "settings.json")

    def test_baseline_read_error_refuses_and_symlink_matches_cli(self) -> None:
        (self.root / "runtime.env").write_text("PRODUCT_DOMAIN=changed.example\n")
        _commit_all(self.root)
        self.head = _git(self.root, "rev-parse", "HEAD")
        blob = _git(self.root, "rev-parse", f"{self.base}:runtime.env")
        self.transport.overrides[f"/repos/example/site/git/blobs/{blob}"] = OSError(
            "baseline unreadable"
        )
        with self.assertRaises(OSError):
            self.scan()
        self.transport.overrides = {}
        (self.root / "runtime.env").unlink()
        (self.root / "runtime.env").symlink_to("settings.json")
        _commit_all(self.root)
        self.head = _git(self.root, "rev-parse", "HEAD")
        actual = self.scan()
        audit = build_config_authority_audit(
            control_plane_root=self.root,
            mode="changed-files-gate",
            base_sha=self.base,
            head_sha=self.head,
        )
        self.assertEqual(
            actual["gate"], evaluate_config_authority_gate(audit, profile="product-repo")
        )

    def test_enabled_delivery_is_queued_without_network_then_worker_scans_commits(self) -> None:
        from control_plane.contracts.merge_train_policy import (
            MergeTrainPolicy,
            MergeTrainPolicyRecord,
        )
        from control_plane.github_app_identity import GitHubAppInstallationToken
        from control_plane.github_app_webhook import (
            GitHubAppWebhookDependencies,
            handle_github_app_webhook_request,
        )
        from control_plane.product_config_authority_events import run_product_config_authority_once
        from control_plane.storage.postgres import PostgresRecordStore
        from tests.test_github_app_webhook import _signature
        from tests.support.stores import sqlite_database_url

        record = build_test_merge_train_policy_record(repository="example/site")
        payload = record.policy.model_dump(mode="json")
        entry = payload["policies"][0]
        entry["config_authority_events_enabled"] = True
        entry["merge_identity"] = {"kind": "github_app", "name": "test-source-app"}
        entry["github_token"] = {
            "github_app": {"app_id": 77, "repository_id": 424242, "private_key_context": "test-app"}
        }
        from copy import deepcopy

        second = deepcopy(entry)
        second["base_branch"] = "release"
        payload["policies"].append(second)
        store = PostgresRecordStore(database_url=sqlite_database_url(self.root / "lp.sqlite"))
        self.addCleanup(store.close)
        with patch("control_plane.github_app_webhook._wake_merge_train", return_value=False):
            store.ensure_schema()
            store.write_repository_inventory_record(_inventory())
            store.write_merge_train_policy_record(
                MergeTrainPolicyRecord(
                    record_id=record.record_id,
                    source="test",
                    updated_at=record.updated_at,
                    policy=MergeTrainPolicy.model_validate(payload),
                )
            )
            body = json.dumps(
                {
                    "repository": {"id": 424242},
                    "ref": "refs/heads/main",
                    "before": self.base,
                    "after": self.head,
                }
            ).encode()
            with patch(
                "control_plane.product_config_authority_events.mint_source_control_read_installation_token"
            ) as mint:
                for ref, before, created in (
                    ("refs/heads/main", "0" * 40, True),
                    ("refs/tags/release", self.base, False),
                    ("refs/heads/work/new-feature", "0" * 40, True),
                    ("refs/heads/launchplane/train/candidate", self.base, False),
                ):
                    event_body = json.dumps(
                        {
                            "repository": {"id": 424242},
                            "ref": ref,
                            "before": before,
                            "after": self.head,
                            "created": created,
                        }
                    ).encode()
                    code, response = handle_github_app_webhook_request(
                        event_body,
                        "push",
                        ref,
                        _signature(event_body),
                        store,
                        Path("."),
                        "trace",
                        dependencies=GitHubAppWebhookDependencies(
                            webhook_secret=lambda: "app-webhook-secret"
                        ),
                    )
                    self.assertEqual(code, 202)
                    self.assertEqual(
                        cast(dict[str, object], response["result"])["status"], "ignored"
                    )
                    self.assertIsNone(store.claim_next_config_authority_delivery("worker", 600))
                    mint.assert_not_called()
                status, _ = handle_github_app_webhook_request(
                    body,
                    "push",
                    "source-queued",
                    _signature(body),
                    store,
                    Path("."),
                    "trace",
                    dependencies=GitHubAppWebhookDependencies(
                        webhook_secret=lambda: "app-webhook-secret"
                    ),
                )
                self.assertEqual(status, 202)
                mint.assert_not_called()
                self.assertEqual(
                    store.read_github_app_webhook_delivery("source-queued").config_authority_state,
                    "pending",
                )
                mint.return_value = GitHubAppInstallationToken(
                    token="source-read-only",
                    app_id=77,
                    installation_id=5,
                    repository_id=424242,
                    repository="example/site",
                    expires_at="2026-10-05T14:00:00Z",
                )
                with (
                    patch(
                        "control_plane.product_config_authority_events.secrets.resolve_context_secret_value",
                        return_value="test-key",
                    ),
                    patch(
                        "control_plane.product_config_authority_events.GitHubBuildProvenanceTransport",
                        return_value=self.transport,
                    ),
                ):
                    completed = run_product_config_authority_once(
                        store, "worker", publish=lambda *_args: {"status": "projected"}
                    )
                assert completed is not None
                self.assertEqual(completed.config_authority["status"], "fail")
                self.assertEqual(completed.config_authority["head_sha"], self.head)
                self.assertEqual(completed.config_authority_state, "failed")
                self.assertEqual(mint.call_args.kwargs["repository_id"], "424242")

    def test_projection_preserves_failure_and_separates_event_scopes(self) -> None:
        from control_plane.contracts.advisory_check_projection import (
            AdvisoryCheckConclusion,
            AdvisoryCheckProjectionResult,
            is_launchplane_projected_check,
        )
        from control_plane.github_app_identity import GitHubAppIdentity, GitHubAppInstallationToken
        from control_plane.product_config_authority_events import (
            publish_product_config_authority_evidence,
        )

        names = set()
        for event in ("pull_request", "push", "merge_group"):
            for status in ("pass", "fail", "unavailable", "unavailable-retry"):
                pending = status == "unavailable-retry"
                expected_conclusion: AdvisoryCheckConclusion | None = (
                    None if pending else "success" if status == "pass" else "failure"
                )
                with self.subTest(event=event, status=status):
                    token = GitHubAppInstallationToken(
                        token="checks-only",
                        app_id=77,
                        installation_id=5,
                        repository_id=424242,
                        repository="example/site",
                        expires_at="2026-10-05T14:00:00Z",
                    )
                    with (
                        patch(
                            "control_plane.product_config_authority_events.resolve_advisory_github_app_identity",
                            return_value=GitHubAppIdentity(app_id=77, private_key="test-key"),
                        ),
                        patch(
                            "control_plane.product_config_authority_events.mint_repository_installation_token",
                            return_value=token,
                        ),
                        patch(
                            "control_plane.product_config_authority_events.write_github_check_projection"
                        ) as writer,
                    ):
                        writer.return_value = AdvisoryCheckProjectionResult(
                            status="projected",
                            name="test",
                            head_sha=self.head,
                            external_id="a" * 64,
                            app_id=77,
                            installation_id=5,
                            check_run_id=9,
                            check_status="in_progress" if pending else "completed",
                            conclusion=expected_conclusion,
                        )
                        evidence: dict[str, JsonValue] = {
                            "status": "unavailable" if pending else status,
                            "retry_pending": pending,
                            "event": event,
                            "base_sha": self.base,
                            "head_sha": self.head,
                            "base_branch": "main",
                            "delivery_id": "verified-delivery",
                        }
                        if status == "fail":
                            findings: list[JsonValue] = [{"key": "😀" * 70000}] * 300
                            evidence["gate"] = {
                                "rejected_finding_count": len(findings),
                                "rejected_findings": findings,
                            }
                        publish_product_config_authority_evidence(_inventory(), evidence, Path("."))
                        projection = writer.call_args.kwargs["projection"]
                        self.assertEqual(projection.conclusion, expected_conclusion)
                        self.assertEqual(
                            projection.check_status, "in_progress" if pending else "completed"
                        )
                        self.assertFalse(is_launchplane_projected_check(projection.name))
                        self.assertIn(
                            "repository_id=424242&delivery_id=verified-delivery", projection.summary
                        )
                        names.add(projection.name)
                        evidence["base_branch"] = "release/stable"
                        publish_product_config_authority_evidence(_inventory(), evidence, Path("."))
                        other = writer.call_args.kwargs["projection"]
                        self.assertNotEqual(other.name, projection.name)
                        self.assertNotEqual(other.external_id, projection.external_id)
                        self.assertTrue(other.name.endswith("/release%2Fstable"))
        self.assertEqual(len(names), len(("pull_request", "push", "merge_group")))

    def test_disabled_policy_mints_no_token_and_preserves_old_digest(self) -> None:
        record = build_test_merge_train_policy_record(repository="example/site")
        original_payload = record.policy.model_dump(mode="json")
        original_payload["policies"][0].pop("config_authority_events_enabled", None)
        from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord

        restored = MergeTrainPolicyRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "policy": original_payload,
            }
        )
        self.assertEqual(restored.policy_sha256, record.policy_sha256)

        class Store:
            def list_merge_train_policy_records(
                self, **kwargs: object
            ) -> tuple[MergeTrainPolicyRecord, ...]:
                return (restored,)

        with patch(
            "control_plane.product_config_authority_events.mint_source_control_read_installation_token"
        ) as mint:
            self.assertEqual(
                request_product_config_authority_event(Store(), _inventory(), "push", {}), {}
            )
        mint.assert_not_called()
