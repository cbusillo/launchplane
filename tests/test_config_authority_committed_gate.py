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
            for operation in ("rev-parse", "diff", "ls-tree", "show"):
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
