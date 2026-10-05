from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.config_authority_audit import (
    build_config_authority_audit,
    evaluate_config_authority_gate,
)
from tests.test_config_authority_audit import _init_repo, _commit_all, _git


class CommittedConfigAuthorityGateTests(unittest.TestCase):
    def test_unchanged_links_scan_changed_targets_under_the_runtime_path(self) -> None:
        for target_change in ("authority", "safe", "preexisting", "deleted", "retargeted"):
            with self.subTest(target_change=target_change), TemporaryDirectory() as directory:
                root = Path(directory)
                _init_repo(root)
                fixture = root / "tests/fixtures/runtime.env"
                fixture.parent.mkdir(parents=True)
                fixture.write_text(
                    "PRODUCT_DOMAIN=live.example\n"
                    if target_change == "preexisting"
                    else "# safe\n"
                )
                (root / "tests/fixtures/alias").symlink_to("runtime.env")
                (root / "runtime.env").symlink_to("tests/fixtures/alias")
                _commit_all(root)
                base = _git(root, "rev-parse", "HEAD")
                if target_change == "deleted":
                    fixture.unlink()
                elif target_change == "retargeted":
                    alternate = root / "tests/fixtures/alternate.env"
                    alternate.write_text("PRODUCT_DOMAIN=live.example\n")
                    alias = root / "tests/fixtures/alias"
                    alias.unlink()
                    alias.symlink_to("alternate.env")
                else:
                    fixture.write_text(
                        "# intended edit\n"
                        + ("PRODUCT_DOMAIN=live.example\n" if target_change != "safe" else "")
                    )
                _commit_all(root)
                head = _git(root, "rev-parse", "HEAD")
                if target_change == "deleted":
                    with self.assertRaisesRegex(ValueError, "does not resolve"):
                        build_config_authority_audit(
                            control_plane_root=root,
                            mode="changed-files-gate",
                            base_sha=base,
                            head_sha=head,
                        )
                    continue
                payload = build_config_authority_audit(
                    control_plane_root=root,
                    mode="changed-files-gate",
                    base_sha=base,
                    head_sha=head,
                )
                expected = "pass" if target_change in {"safe", "preexisting"} else "fail"
                self.assertEqual(
                    evaluate_config_authority_gate(payload, profile="product-repo")["status"],
                    expected,
                )
                files = payload["source_files"]
                assert isinstance(files, list)
                self.assertIn("runtime.env", {file["path"] for file in files})
                fixture.write_text("# dirty local repair\n")
                self.assertEqual(
                    payload,
                    build_config_authority_audit(
                        control_plane_root=root,
                        mode="changed-files-gate",
                        base_sha=base,
                        head_sha=head,
                    ),
                )

    def test_unchanged_link_follows_a_retargeted_directory_component(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            for name, content in (("safe", "# safe\n"), ("live", "PRODUCT_DOMAIN=live.example\n")):
                fixture = root / f"tests/fixtures/{name}/runtime.env"
                fixture.parent.mkdir(parents=True)
                fixture.write_text(content)
            directory_link = root / "tests/fixtures/selected"
            directory_link.symlink_to("safe")
            (root / "runtime.env").symlink_to("tests/fixtures/selected/runtime.env")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            directory_link.unlink()
            directory_link.symlink_to("live")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(
                evaluate_config_authority_gate(payload, profile="product-repo")["status"], "fail"
            )

    def test_unrelated_edit_does_not_scan_existing_broken_link(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "runtime.env").symlink_to("missing.env")
            (root / "README.md").write_text("# Before\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            (root / "README.md").write_text("# Intended documentation edit\n")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(evaluate_config_authority_gate(payload)["status"], "pass")

    def test_submodule_bump_refuses_an_unchanged_link_through_the_gitlink(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("# First\n")
            _commit_all(root)
            first = _git(root, "rev-parse", "HEAD")
            (root / "README.md").write_text("# Second\n")
            _commit_all(root)
            second = _git(root, "rev-parse", "HEAD")
            (root / "runtime.env").symlink_to("vendor/shared/runtime.env")
            _git(root, "update-index", "--add", "--cacheinfo", f"160000,{first},vendor/shared")
            _git(root, "add", "runtime.env")
            _git(root, "commit", "-m", "linked gitlink")
            base = _git(root, "rev-parse", "HEAD")
            _git(root, "update-index", "--cacheinfo", f"160000,{second},vendor/shared")
            _git(root, "commit", "-m", "bumped gitlink")
            with self.assertRaisesRegex(ValueError, "cannot resolve through a submodule"):
                build_config_authority_audit(
                    control_plane_root=root,
                    mode="changed-files-gate",
                    base_sha=base,
                    head_sha=_git(root, "rev-parse", "HEAD"),
                )

    def test_file_replaced_by_directory_reports_only_its_new_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            path = root / "settings.env"
            path.write_text("# safe\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            path.unlink()
            path.mkdir()
            (path / "safe.env").write_text("# safe\n")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(evaluate_config_authority_gate(payload)["status"], "pass")
            coverage = payload["coverage"]
            assert isinstance(coverage, dict)
            self.assertFalse(coverage["gaps"])

    def test_full_audit_refuses_commit_arguments(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            for base_sha, head_sha in (("a" * 40, None), (None, "b" * 40)):
                with self.subTest(base_sha=base_sha, head_sha=head_sha):
                    with self.assertRaisesRegex(ValueError, "require changed-files-gate"):
                        build_config_authority_audit(
                            control_plane_root=root, base_sha=base_sha, head_sha=head_sha
                        )

    def test_dirty_edits_cannot_hide_committed_authority(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            path = root / "settings.env"
            path.write_text("# empty\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            path.write_text("PRODUCT_DOMAIN=live.example\n")
            _commit_all(root)
            head = _git(root, "rev-parse", "HEAD")
            path.unlink()
            first = build_config_authority_audit(
                control_plane_root=root, mode="changed-files-gate", base_sha=base, head_sha=head
            )
            path.write_text("# local removal\n")
            second = build_config_authority_audit(
                control_plane_root=root, mode="changed-files-gate", base_sha=base, head_sha=head
            )
            self.assertEqual(first, second)
            self.assertEqual(
                evaluate_config_authority_gate(first, profile="product-repo")["status"], "fail"
            )

    def test_every_git_failure_refuses_verification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "settings.env").write_text("# empty\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            (root / "settings.env").write_text("PRODUCT_DOMAIN=live.example\n")
            _commit_all(root)
            head = _git(root, "rev-parse", "HEAD")
            original = subprocess.run
            for operation in ("rev-parse", "diff", "ls-tree", "cat-file", "show"):
                for failure in ("exit", "timeout", "oserror"):
                    with self.subTest(operation=operation, failure=failure):

                        def failing_run(
                            args: tuple[str, ...], **kwargs: object
                        ) -> subprocess.CompletedProcess[str]:
                            if args[1] == operation:
                                if failure == "timeout":
                                    raise subprocess.TimeoutExpired(args, 10)
                                if failure == "oserror":
                                    raise OSError("planted read fault")
                                return subprocess.CompletedProcess(
                                    args, 1, "", "planted read fault"
                                )
                            return original(args, **kwargs)  # type: ignore[call-overload,no-any-return]

                        with patch(
                            "control_plane.config_authority_audit.subprocess.run",
                            side_effect=failing_run,
                        ):
                            with self.assertRaisesRegex(ValueError, "git read failed"):
                                build_config_authority_audit(
                                    control_plane_root=root,
                                    mode="changed-files-gate",
                                    base_sha=base,
                                    head_sha=head,
                                )

    def test_explicit_commits_are_required_even_with_dirty_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "untracked.env").write_text("PRODUCT_DOMAIN=dirty.example\n")
            with self.assertRaisesRegex(ValueError, "requires explicit"):
                build_config_authority_audit(control_plane_root=root, mode="changed-files-gate")

    def test_committed_bytes_preserve_hash_and_existing_coverage_gaps(self) -> None:
        import hashlib
        from control_plane.config_authority_audit import MAX_SCANNED_FILE_BYTES

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / ".gitattributes").write_text("*.env -text\n")
            (root / "settings.env").write_bytes(b"# empty\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            content = b"# unchanged authority\r\n"
            (root / "settings.env").write_bytes(content)
            (root / "large.json").write_bytes(b" " * (MAX_SCANNED_FILE_BYTES + 1))
            (root / "binary.env").write_bytes(b"bad\x00content")
            (root / "encoding.env").write_bytes(b"# Latin-1 \xff\n")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            files = payload["source_files"]
            assert isinstance(files, list)
            self.assertEqual(files[0]["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(files[0]["size"], len(content))
            coverage = payload["coverage"]
            assert isinstance(coverage, dict)
            self.assertEqual(
                {item["reason"] for item in coverage["gaps"]},
                {"skipped_large_file", "skipped_binary_file", "decode_failure"},
            )

    def test_symlink_cannot_hide_runtime_authority_in_an_allowed_fixture(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            fixture = root / "tests/fixtures/runtime.env"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("PRODUCT_DOMAIN=live.example\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            runtime = root / "runtime.env"
            runtime.symlink_to("tests/fixtures/runtime.env")
            _commit_all(root)
            linked = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(
                evaluate_config_authority_gate(linked, profile="product-repo")["status"], "fail"
            )
            linked_files = linked["source_files"]
            assert isinstance(linked_files, list)
            self.assertEqual(linked_files[0]["resolved_git_path"], "tests/fixtures/runtime.env")
            runtime.unlink()
            runtime.write_bytes(fixture.read_bytes())
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(
                evaluate_config_authority_gate(payload, profile="product-repo")["status"], "fail"
            )

    def test_symlink_chains_resolve_in_committed_tree_and_unsafe_links_refuse(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "AGENTS.md").write_text("# Guidance\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            link = root / "CLAUDE.md"
            link.symlink_to("AGENTS.md")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(evaluate_config_authority_gate(payload)["status"], "pass")
            link.unlink()
            (root / "alias.md").symlink_to("AGENTS.md")
            (root / "guide").symlink_to(".")
            link.symlink_to("guide/alias.md")
            _commit_all(root)
            chained = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(evaluate_config_authority_gate(chained)["status"], "pass")
            link.unlink()
            for target in ("../outside.md", "/outside.md", "CLAUDE.md"):
                with self.subTest(target=target):
                    link.symlink_to(target)
                    _commit_all(root)
                    with self.assertRaises(ValueError):
                        build_config_authority_audit(
                            control_plane_root=root,
                            mode="changed-files-gate",
                            base_sha=base,
                            head_sha=_git(root, "rev-parse", "HEAD"),
                        )
                    link.unlink()

    def test_parent_component_follows_the_prior_symlink_before_dotdot(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "tests/fixtures/deep").mkdir(parents=True)
            (root / "tests/fixtures/deep/keep.txt").write_text("# directory\n")
            (root / "tests/fixtures/real.env").write_text("PRODUCT_DOMAIN=live.example\n")
            (root / "real.env").write_text("# harmless\n")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            (root / "a").symlink_to("tests/fixtures/deep")
            (root / "runtime.env").symlink_to("a/../real.env")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(
                evaluate_config_authority_gate(payload, profile="product-repo")["status"], "fail"
            )

    def test_broken_baseline_link_can_be_repaired_and_expanding_cycles_stop(self) -> None:
        from control_plane.config_authority_audit import MAX_COMMITTED_SYMLINK_HOPS
        import control_plane.config_authority_audit as audit

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            link = root / "CLAUDE.md"
            link.symlink_to("../AGENTS.md")
            _commit_all(root)
            base = _git(root, "rev-parse", "HEAD")
            link.unlink()
            link.write_text("# repaired\n")
            _commit_all(root)
            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
                base_sha=base,
                head_sha=_git(root, "rev-parse", "HEAD"),
            )
            self.assertEqual(evaluate_config_authority_gate(payload)["status"], "pass")
            (root / "loop").symlink_to("loop/x")
            (root / "runtime.env").symlink_to("loop/runtime.env")
            _commit_all(root)
            original = audit._git_bytes
            calls = 0

            def bounded_read(path: Path, *arguments: str, strict: bool = False) -> bytes:
                nonlocal calls
                if arguments[0] == "show":
                    calls += 1
                    if calls > MAX_COMMITTED_SYMLINK_HOPS + 1:
                        raise RuntimeError("Resolver did not stop within its hop budget")
                return original(path, *arguments, strict=strict)

            with patch.object(audit, "_git_bytes", side_effect=bounded_read):
                with self.assertRaisesRegex(ValueError, "symlink hops"):
                    build_config_authority_audit(
                        control_plane_root=root,
                        mode="changed-files-gate",
                        base_sha=base,
                        head_sha=_git(root, "rev-parse", "HEAD"),
                    )
