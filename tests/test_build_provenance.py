import io
import json
import unittest
import zipfile
from unittest.mock import MagicMock, Mock, patch

from control_plane.build_provenance import (
    BUILD_WORKFLOW_PATH,
    MAX_MANIFEST_BYTES,
    BuildProvenanceError,
    VerifiedBuildArtifact,
    record_verified_build_artifact,
    verify_build_artifact,
    verify_generic_web_build,
)
from control_plane.contracts.artifact_identity import BuildPurpose
from tests.support.artifact_manifests import artifact_manifest_v2

REPOSITORY = "example/site"
REPOSITORY_ID = "101"
IMAGE_REPOSITORY = "ghcr.io/example/site"
COMMIT = "0" * 40
TIP = "f" * 40
MERGED_BRANCH_COMMIT = "e" * 40


class FakeGitHub:
    def __init__(
        self,
        *,
        runs: list[dict[str, object]],
        first_parents: dict[str, str] | None = None,
        pull_request_head: str = COMMIT,
        manifest: dict[str, object] | None = None,
    ) -> None:
        self.runs = runs
        self.first_parents = (
            first_parents if first_parents is not None else {TIP: COMMIT, COMMIT: ""}
        )
        self.pull_request_head = pull_request_head
        self.manifest = manifest or artifact_manifest_v2(
            image_repository=IMAGE_REPOSITORY, tenant_source_repository=REPOSITORY
        ).model_dump(mode="json")

    def get_json(self, path: str) -> object:
        if path == f"/repos/{REPOSITORY}":
            return {"id": int(REPOSITORY_ID), "default_branch": "main"}
        if path.startswith(f"/repos/{REPOSITORY}/actions/runs?"):
            return {"workflow_runs": [run for run in self.runs if f"event={run['event']}" in path]}
        if path.startswith(f"/repos/{REPOSITORY}/commits?"):
            if "page=1" not in path:
                return []
            return [
                {"sha": sha, "parents": [{"sha": parent}] if parent else []}
                for sha, parent in self.first_parents.items()
            ]
        if path.startswith(f"/repos/{REPOSITORY}/pulls/"):
            return {"head": {"sha": self.pull_request_head}}
        if "/artifacts?" in path:
            run_id = path.split("/actions/runs/")[1].split("/")[0]
            return {
                "artifacts": [
                    {"id": int(run_id) * 10, "name": "artifact-manifest-1", "expired": False}
                ]
            }
        raise AssertionError(f"unexpected GitHub read {path}")

    def get_bytes(self, path: str) -> bytes:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("artifact-manifest.json", json.dumps(self.manifest))
        return archive.getvalue()


def _run(
    *,
    run_id: int = 7,
    event: str = "push",
    head_branch: str = "main",
    path: str = BUILD_WORKFLOW_PATH,
    head_repository_id: int = int(REPOSITORY_ID),
    conclusion: str = "success",
) -> dict[str, object]:
    return {
        "id": run_id,
        "run_attempt": 1,
        "event": event,
        "head_branch": head_branch,
        "head_sha": COMMIT,
        "path": path,
        "status": "completed",
        "conclusion": conclusion,
        "repository": {"id": int(REPOSITORY_ID)},
        "head_repository": {"id": head_repository_id},
    }


def _verify(
    github: FakeGitHub, *, purpose: BuildPurpose = "release", pull_request_number: int | None = None
) -> VerifiedBuildArtifact:
    return verify_build_artifact(
        transport=github,
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        commit=COMMIT,
        purpose=purpose,
        context="site",
        image_repository=IMAGE_REPOSITORY,
        pull_request_number=pull_request_number,
    )


class BuildProvenanceTests(unittest.TestCase):
    def test_json_and_manifest_body_connection_drops_are_wrapped(self) -> None:
        from http.client import IncompleteRead
        from control_plane.build_provenance import GitHubBuildProvenanceTransport

        transport = GitHubBuildProvenanceTransport(token="test-token")
        for archive in (False, True):
            with self.subTest(archive=archive):
                connection = MagicMock()
                connection.__enter__.return_value.read.side_effect = IncompleteRead(b"{", 10)
                with (
                    patch("control_plane.build_provenance.urlopen", return_value=connection),
                    patch("control_plane.build_provenance.build_opener") as opener,
                    self.assertRaises(BuildProvenanceError) as caught,
                ):
                    opener.return_value.open.return_value = connection
                    if archive:
                        transport.get_bytes("/repos/example/site/actions/artifacts/1/zip")
                    else:
                        transport.get_json("/repos/example/site")
                self.assertIsInstance(caught.exception.__cause__, IncompleteRead)

    def test_accepts_main_push_build_under_launchplane_key(self) -> None:
        manifest = _verify(FakeGitHub(runs=[_run()])).manifest

        self.assertEqual(manifest.artifact_id, "artifact-site-run-7-1")
        self.assertIsNotNone(manifest.source_build)
        assert manifest.source_build is not None
        self.assertEqual(manifest.source_build.purpose, "release")
        self.assertEqual(manifest.source_build.github_artifact_id, 70)

    def test_refuses_tag_named_like_main_at_a_merged_pull_request_commit(self) -> None:
        github = FakeGitHub(
            runs=[_run()],
            first_parents={TIP: MERGED_BRANCH_COMMIT, MERGED_BRANCH_COMMIT: ""},
        )

        with self.assertRaisesRegex(BuildProvenanceError, "first-parent"):
            _verify(github)

    def test_refuses_pull_request_run_as_release(self) -> None:
        github = FakeGitHub(runs=[_run(event="pull_request", head_branch="feature")])

        with self.assertRaisesRegex(BuildProvenanceError, "No successful push run"):
            _verify(github)

    def test_refuses_other_workflow_fork_and_failed_runs(self) -> None:
        for run in (
            _run(path=".github/workflows/other.yml"),
            _run(head_repository_id=999),
            _run(conclusion="failure"),
        ):
            with self.subTest(run=run), self.assertRaises(BuildProvenanceError):
                _verify(FakeGitHub(runs=[run]))

    def test_refuses_manifest_for_another_commit_or_image(self) -> None:
        for manifest in (
            {
                **artifact_manifest_v2(
                    image_repository="ghcr.io/example/other", tenant_source_repository=REPOSITORY
                ).model_dump(mode="json")
            },
            {
                **artifact_manifest_v2(
                    image_repository=IMAGE_REPOSITORY, tenant_source_repository="example/other"
                ).model_dump(mode="json")
            },
        ):
            with self.subTest(manifest=manifest["image"]), self.assertRaises(BuildProvenanceError):
                _verify(FakeGitHub(runs=[_run()], manifest=manifest))

    def test_accepts_preview_only_for_the_pull_request_head(self) -> None:
        run = _run(event="pull_request", head_branch="feature")
        manifest = _verify(
            FakeGitHub(runs=[run]), purpose="preview", pull_request_number=5
        ).manifest
        assert manifest.source_build is not None
        self.assertEqual(manifest.source_build.purpose, "preview")

        with self.assertRaisesRegex(BuildProvenanceError, "current head"):
            _verify(
                FakeGitHub(runs=[run], pull_request_head=TIP),
                purpose="preview",
                pull_request_number=5,
            )

    def test_record_refuses_different_content_under_the_same_key(self) -> None:
        verified = _verify(FakeGitHub(runs=[_run()]))
        record_store = Mock()
        record_store.read_artifact_manifest.return_value = verified.manifest.model_copy(
            update={"source_commit": "1" * 40}
        )

        with self.assertRaisesRegex(BuildProvenanceError, "different content"):
            record_verified_build_artifact(record_store=record_store, verified=verified)
        record_store.write_artifact_manifest.assert_not_called()

    def test_refuses_a_malformed_manifest_as_unverified(self) -> None:
        github = FakeGitHub(runs=[_run()])
        github.manifest = {"artifact_id": "not-a-manifest"}

        with self.assertRaisesRegex(BuildProvenanceError, "not a valid artifact manifest"):
            _verify(github)

    def test_refuses_an_oversized_or_compression_bomb_manifest(self) -> None:
        github = FakeGitHub(runs=[_run()])
        github.manifest = {"padding": "0" * (MAX_MANIFEST_BYTES + 1)}

        with self.assertRaisesRegex(BuildProvenanceError, "larger than allowed"):
            _verify(github)

    def test_record_refuses_a_preview_artifact(self) -> None:
        run = _run(event="pull_request", head_branch="feature")
        verified = _verify(FakeGitHub(runs=[run]), purpose="preview", pull_request_number=5)
        record_store = Mock()

        with self.assertRaisesRegex(BuildProvenanceError, "Only a release build"):
            record_verified_build_artifact(record_store=record_store, verified=verified)
        record_store.write_artifact_manifest.assert_not_called()

    def test_record_writes_a_new_artifact(self) -> None:
        verified = _verify(FakeGitHub(runs=[_run()]))
        record_store = Mock()
        record_store.read_artifact_manifest.side_effect = FileNotFoundError

        record_verified_build_artifact(record_store=record_store, verified=verified)

        record_store.write_artifact_manifest.assert_called_once_with(verified.manifest)


def _generic_web_manifest(**changes: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "generic-web",
        "source_commit": COMMIT,
        "image": {"repository": IMAGE_REPOSITORY, "digest": "sha256:" + "c" * 64},
        **changes,
    }


class GenericWebBuildProvenanceTests(unittest.TestCase):
    def test_historical_preview_proof_requires_the_exact_recorded_image(self) -> None:
        image = f"{IMAGE_REPOSITORY}@sha256:{'c' * 64}"
        github = FakeGitHub(
            runs=[_run(event="pull_request", head_branch="feature")],
            pull_request_head=TIP,
            manifest=_generic_web_manifest(),
        )
        for recorded, allowed in (("", False), (image, True), (image[:-1] + "d", False)):
            with self.subTest(recorded=recorded):

                def verify() -> str:
                    return verify_generic_web_build(
                        transport=github,
                        repository=REPOSITORY,
                        repository_id=REPOSITORY_ID,
                        commit=COMMIT,
                        purpose="preview",
                        image_repository=IMAGE_REPOSITORY,
                        pull_request_number=5,
                        recorded_image_reference=recorded,
                    ).image_reference

                if allowed:
                    self.assertEqual(verify(), image)
                else:
                    with self.assertRaises(BuildProvenanceError):
                        verify()

    def verify(self, github: FakeGitHub) -> str:
        return verify_generic_web_build(
            transport=github,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            commit=COMMIT,
            purpose="release",
            image_repository=IMAGE_REPOSITORY,
        ).image_reference

    def test_accepts_the_image_a_main_push_build_reports(self) -> None:
        github = FakeGitHub(runs=[_run()], manifest=_generic_web_manifest())

        self.assertEqual(self.verify(github), f"{IMAGE_REPOSITORY}@sha256:{'c' * 64}")

    def test_refuses_a_manifest_that_does_not_prove_this_build(self) -> None:
        odoo_manifest = artifact_manifest_v2(
            image_repository=IMAGE_REPOSITORY, tenant_source_repository=REPOSITORY
        ).model_dump(mode="json")
        for manifest in (
            _generic_web_manifest(source_commit="1" * 40),
            _generic_web_manifest(
                image={"repository": "ghcr.io/example/other", "digest": "sha256:" + "c" * 64}
            ),
            _generic_web_manifest(image={"repository": IMAGE_REPOSITORY, "digest": "latest"}),
            odoo_manifest,
        ):
            with self.subTest(manifest=manifest), self.assertRaises(BuildProvenanceError):
                self.verify(FakeGitHub(runs=[_run()], manifest=manifest))

    def test_shares_the_run_checks_of_an_odoo_build(self) -> None:
        github = FakeGitHub(
            runs=[_run()],
            first_parents={TIP: MERGED_BRANCH_COMMIT, MERGED_BRANCH_COMMIT: ""},
            manifest=_generic_web_manifest(),
        )

        with self.assertRaisesRegex(BuildProvenanceError, "first-parent"):
            self.verify(github)


if __name__ == "__main__":
    unittest.main()
