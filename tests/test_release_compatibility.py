"""Isolated full-input fixtures for release classification and module plans."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from control_plane.build_provenance import BuildProvenanceError, record_verified_build_artifact
from tests.test_build_provenance import FakeGitHub, _run, _verify
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest, ArtifactSourceBuild
from control_plane.contracts.artifact_release_compatibility import (
    ArtifactReleaseCompatibility,
    ReleaseDatabaseCompatibility,
    ReleaseInputFile,
    ReleaseModuleDeclaration,
    ReleaseSourceInventory,
)
from control_plane.contracts.release_review import ReleaseChecklist, ReleaseReviewDecisionRecord
from control_plane.release_compatibility import (
    classify_release_database_compatibility,
    release_opaque_inputs_sha256,
)
from control_plane.release_review import build_release_review, checklist_digest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.test_release_review import github_read, profile, seed


def file(
    path: str, kind: str, *, module: str = "site", digest: str = "a", semantic: str = ""
) -> ReleaseInputFile:
    return ReleaseInputFile.model_validate(
        dict(
            path=path,
            kind=kind,
            module=module,
            sha256=digest * 64,
            manifest_database_sha256=semantic * 64,
        )
    )


def artifact(
    artifact_id: str,
    *,
    files: tuple[ReleaseInputFile, ...] = (),
    shared_files: tuple[ReleaseInputFile, ...] = (),
    modules: tuple[ReleaseModuleDeclaration, ...] | None = None,
    install_modules: tuple[str, ...] = ("site",),
) -> ArtifactIdentityManifest:
    manifest = artifact_manifest_v2(
        artifact_id=artifact_id,
        tenant_source_repository="example/site",
        odoo_install_modules=install_modules,
        image_repository="ghcr.io/example/site",
    )
    manifest.source_commit = "a" * 40 if artifact_id in {"old", "artifact-prod"} else "b" * 40
    assert manifest.dependency_provenance is not None
    next(
        lock for lock in manifest.dependency_provenance.uv_locks if lock.scope == "tenant"
    ).source_ref = manifest.source_commit
    if shared_files:
        manifest.addon_sources[0].ref = manifest.source_commit
    manifest.source_build = ArtifactSourceBuild(
        repository="example/site",
        repository_id="1",
        workflow_path=".github/workflows/build.yml",
        event="push",
        purpose="release",
        run_id=1,
        run_attempt=1,
        github_artifact_id=1,
        manifest_artifact_id=artifact_id,
    )
    sources = [
        ReleaseSourceInventory(
            input_name="tenant",
            repository="example/site",
            commit=manifest.source_commit,
            files=files,
        )
    ]
    sources += [
        ReleaseSourceInventory(
            input_name=f"addon:{s.repository}",
            repository=s.repository,
            commit=s.ref,
            files=shared_files,
        )
        for s in manifest.addon_sources
    ]
    sources += [
        ReleaseSourceInventory(
            input_name=f"base:{s.role}",
            repository=s.source_repository,
            commit=s.source_ref,
            files=(),
        )
        for s in manifest.build_provenance.base_images
    ]
    sources += [
        ReleaseSourceInventory(
            input_name=f"tool:{s.name}",
            repository=s.source_repository,
            commit=s.source_ref,
            files=(),
        )
        for s in manifest.build_provenance.build_tools
    ]
    assert manifest.dependency_provenance is not None
    sources += [
        ReleaseSourceInventory(
            input_name=f"external:{s.source_repository}:{s.dependency_file_path}",
            repository=s.source_repository,
            commit=s.source_ref,
            files=(),
        )
        for s in manifest.dependency_provenance.external_compatibility_inputs
    ]
    manifest.release_compatibility = ArtifactReleaseCompatibility(
        complete=True,
        read_write_compatible=True,
        sources=tuple(sources),
        modules=modules
        or (
            ReleaseModuleDeclaration(name="base"),
            ReleaseModuleDeclaration(name="site", depends=("base",)),
        ),
    )
    return ArtifactIdentityManifest.model_validate(manifest.model_dump())


def classify(
    production: ArtifactIdentityManifest | None, candidate: ArtifactIdentityManifest
) -> ReleaseDatabaseCompatibility:
    return classify_release_database_compatibility(production=production, candidate=candidate)


class ReleaseCompatibilityTests(unittest.TestCase):
    def test_static_code_and_docs_diffs_do_not_blanket_update(self) -> None:
        for path, kind, module in (
            ("addons/site/static/src/main.scss", "static", "site"),
            ("addons/site/static/src/main.js", "static", "site"),
            ("addons/site/static/src/templates.xml", "static", "site"),
            ("addons/site/controllers/main.py", "code", "site"),
            ("docs/deployment.md", "docs_ci", ""),
            (".github/workflows/build.yml", "docs_ci", ""),
            (".gitignore", "docs_ci", ""),
            ("Makefile", "docs_ci", ""),
            (".pre-commit-config.yaml", "docs_ci", ""),
            ("README.rst", "docs_ci", ""),
            ("addons/site/tests/mock_response.json", "docs_ci", ""),
            ("addons/site/static/favicon.ico", "static", "site"),
            ("addons/site/static/font.ttf", "static", "site"),
            ("addons/site/static/font.otf", "static", "site"),
            ("addons/site/static/font.eot", "static", "site"),
            ("addons/site/static/demo.gif", "static", "site"),
            ("addons/site/static/demo.mp4", "static", "site"),
        ):
            with self.subTest(path=path):
                before = artifact("old", files=(file(path, kind, module=module),))
                after = artifact("new", files=(file(path, kind, module=module, digest="b"),))
                result = classify(before, after)
                self.assertEqual(result.classification, "compatible")
                self.assertTrue(result.module_plan_complete)
                self.assertEqual(result.update_modules, ())
                self.assertEqual(result.install_modules, ())
                self.assertEqual(result.warm_assets, kind == "static")
                self.assertEqual(result.retain_previous_assets, result.warm_assets)
                assert before.release_compatibility is not None
                self.assertEqual(
                    result.changes[0].before_sha256,
                    before.release_compatibility.sources[0].files[0].sha256,
                )

    def test_assets_manifest_semantics_distinguish_data_and_dependencies(self) -> None:
        path = "addons/site/__manifest__.py"
        before = artifact("old", files=(file(path, "manifest_assets", semantic="c"),))
        assets = artifact("new", files=(file(path, "manifest_assets", digest="b", semantic="c"),))
        result = classify(before, assets)
        self.assertEqual(result.classification, "compatible")
        self.assertTrue(result.warm_assets)
        data = artifact("new", files=(file(path, "manifest_assets", digest="b", semantic="d"),))
        result = classify(before, data)
        self.assertEqual(result.classification, "database_changing")
        self.assertEqual(result.update_modules, ("site",))
        self.assertIn("changed_manifest_database_fields", result.reasons)

    def test_db_changes_expand_only_reverse_dependents_and_preserve_noupdate(self) -> None:
        graph = (
            ReleaseModuleDeclaration(name="base"),
            ReleaseModuleDeclaration(name="site", depends=("base",)),
            ReleaseModuleDeclaration(name="extension", depends=("site",)),
            ReleaseModuleDeclaration(name="unrelated", depends=("base",)),
        )
        for path, kind in (
            ("addons/site/views/home.xml", "database_data"),
            ("addons/site/data/editor.xml", "database_data"),
            ("addons/site/models/order.py", "model"),
            ("addons/site/migrations/pre.py", "migration"),
        ):
            with self.subTest(path=path):
                before = artifact("old", files=(file(path, kind),), modules=graph)
                after = artifact("new", files=(file(path, kind, digest="b"),), modules=graph)
                result = classify(before, after)
                self.assertEqual(result.classification, "database_changing")
                self.assertEqual(result.changed_modules, ("site",))
                self.assertEqual(result.update_modules, ("extension", "site"))
                self.assertEqual(result.install_modules, ())
                self.assertTrue(result.preserve_noupdate)

    def test_installs_and_new_dependencies_are_separate_from_updates(self) -> None:
        graph = (
            ReleaseModuleDeclaration(name="base"),
            ReleaseModuleDeclaration(name="site", depends=("base",)),
            ReleaseModuleDeclaration(name="new_dependency", depends=("base",)),
            ReleaseModuleDeclaration(name="new_module", depends=("new_dependency",)),
        )
        before = artifact("old", modules=graph)
        after = artifact("new", modules=graph, install_modules=("site", "new_module"))
        result = classify(before, after)
        self.assertEqual(result.classification, "database_changing")
        self.assertEqual(result.install_modules, ("new_dependency", "new_module"))
        self.assertEqual(result.update_modules, ())
        self.assertNotIn("site", result.install_modules)

    def test_shared_module_diff_is_examined_and_base_or_dependency_changes_fail_closed(
        self,
    ) -> None:
        shared = "addons/site/models/order.py"
        before = artifact("old", shared_files=(file(shared, "model"),))
        after = artifact("new", shared_files=(file(shared, "model", digest="b"),))
        result = classify(before, after)
        self.assertEqual(result.classification, "database_changing")
        self.assertTrue(result.changes[0].input_name.startswith("addon:"))
        for mutation in ("base", "dependency", "framework"):
            with self.subTest(input=mutation):
                after = artifact("new")
                if mutation == "base":
                    after.build_provenance.base_images[0].image.digest = "sha256:" + "b" * 64
                elif mutation == "framework":
                    after.enterprise_base_digest = "sha256:" + "b" * 64
                else:
                    assert after.dependency_provenance is not None
                    after.dependency_provenance.uv_locks[0].sha256 = "b" * 64
                result = classify(artifact("old"), after)
                self.assertEqual(result.classification, "database_changing")
                self.assertFalse(result.module_plan_complete)
                self.assertIn("changed_base_dependency_or_build_inputs", result.reasons)

    def test_examined_opaque_changes_have_a_hash_bound_targeted_path(self) -> None:
        graph = (
            ReleaseModuleDeclaration(name="base"),
            ReleaseModuleDeclaration(name="site", depends=("base",)),
            ReleaseModuleDeclaration(name="unrelated"),
        )
        before, after = artifact("old", modules=graph), artifact("new", modules=graph)
        after.enterprise_base_digest = "sha256:" + "e" * 64
        assert before.release_compatibility is not None and after.release_compatibility is not None
        before.release_compatibility = before.release_compatibility.model_copy(
            update={
                "opaque_inputs_sha256": release_opaque_inputs_sha256(before),
            }
        )
        after.release_compatibility = after.release_compatibility.model_copy(
            update={
                "opaque_inputs_sha256": release_opaque_inputs_sha256(after),
                "database_update_modules": ("base",),
            }
        )
        result = classify(before, after)
        self.assertEqual(result.classification, "database_changing")
        self.assertTrue(result.module_plan_complete)
        self.assertEqual(result.update_modules, ("base", "site"))
        self.assertEqual(result.changed_modules, ("base",))
        self.assertEqual(result.install_modules, ())
        # The supported route cannot reuse a plan for different opaque inputs.
        after.enterprise_base_digest = "sha256:" + "f" * 64
        result = classify(before, after)
        self.assertFalse(result.module_plan_complete)
        self.assertIn("unexamined_opaque_input_plan", result.reasons)

    def test_non_module_build_config_requires_an_examined_plan_not_blanket_u(self) -> None:
        for path in (
            "pyproject.toml",
            "docker-compose.yml",
            ".dockerignore",
            "package.json",
            "tox.ini",
        ):
            with self.subTest(path=path):
                before = artifact("old", files=(file(path, "dependency", module=""),))
                after = artifact("new", files=(file(path, "dependency", module="", digest="b"),))
                self.assertFalse(classify(before, after).module_plan_complete)
                assert (
                    before.release_compatibility is not None
                    and after.release_compatibility is not None
                )
                before.release_compatibility = before.release_compatibility.model_copy(
                    update={
                        "opaque_inputs_sha256": release_opaque_inputs_sha256(before),
                    }
                )
                after.release_compatibility = after.release_compatibility.model_copy(
                    update={
                        "opaque_inputs_sha256": release_opaque_inputs_sha256(after),
                        "database_update_modules": (),
                    }
                )
                result = classify(before, after)
                self.assertEqual(result.classification, "database_changing")
                self.assertTrue(result.module_plan_complete)
                self.assertEqual(result.update_modules, ())
                self.assertEqual(result.install_modules, ())

    def test_missing_history_declarations_or_full_inputs_cannot_be_compatible(self) -> None:
        after = artifact("new")
        self.assertEqual(classify(None, after).classification, "database_changing")
        for missing in ("declaration", "source", "verification", "incomplete"):
            with self.subTest(missing=missing):
                before = artifact("old")
                declaration = before.release_compatibility
                assert declaration is not None
                if missing == "declaration":
                    before.release_compatibility = None
                elif missing == "source":
                    before.release_compatibility = declaration.model_copy(
                        update={"sources": declaration.sources[:-1]}
                    )
                elif missing == "verification":
                    before.source_build = None
                else:
                    before.release_compatibility = declaration.model_copy(
                        update={"complete": False}
                    )
                result = classify(before, after)
                self.assertEqual(result.classification, "database_changing")
                self.assertFalse(result.module_plan_complete)
                self.assertEqual(result.update_modules, ())

    def test_conflicting_or_unknown_declarations_do_not_hide_database_changes(self) -> None:
        for path, kind in (
            ("addons/site/models/order.py", "code"),
            ("addons/site/wizard/order.py", "code"),
            ("addons/site/report/order.py", "code"),
            ("addons/site/security/rules.py", "code"),
            ("addons/site/views/home.xml", "static"),
            ("addons/site/hook.py", "unknown"),
        ):
            with self.subTest(path=path):
                before = artifact("old", files=(file(path, kind),))
                after = artifact("new", files=(file(path, kind, digest="b"),))
                result = classify(before, after)
                self.assertEqual(result.classification, "database_changing")
                self.assertFalse(result.module_plan_complete)
        before = artifact("old")
        after = artifact("new")
        assert after.release_compatibility is not None
        after.release_compatibility = after.release_compatibility.model_copy(
            update={"read_write_compatible": False}
        )
        self.assertEqual(classify(before, after).classification, "database_changing")

    def test_declaration_survives_verified_ingestion_and_is_immutable(self) -> None:
        manifest = artifact("producer-data")
        manifest.source_commit = "0" * 40
        assert manifest.dependency_provenance is not None
        next(
            lock for lock in manifest.dependency_provenance.uv_locks if lock.scope == "tenant"
        ).source_ref = manifest.source_commit
        assert manifest.release_compatibility is not None
        sources = list(manifest.release_compatibility.sources)
        sources[0] = sources[0].model_copy(update={"commit": manifest.source_commit})
        manifest.release_compatibility = manifest.release_compatibility.model_copy(
            update={"sources": tuple(sources)}
        )
        # Ingestion assigns the identity and source build; producer values cannot
        # substitute for those checked against the source-host build record.
        payload = manifest.model_dump(mode="json")
        payload.pop("source_build")
        verified = _verify(FakeGitHub(runs=[_run()], manifest=payload))
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            record_verified_build_artifact(record_store=store, verified=verified)
            saved = store.read_artifact_manifest(verified.manifest.artifact_id)
            self.assertEqual(saved.release_compatibility, manifest.release_compatibility)
            saved.release_compatibility = None
            with self.assertRaises(BuildProvenanceError):
                record_verified_build_artifact(
                    record_store=store, verified=verified.model_copy(update={"manifest": saved})
                )

    def test_schema_rejects_ambiguous_inventory_and_incomplete_module_graph(self) -> None:
        for path in (".", "../views/home.xml", "/data/file", "static\\main.js"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                file(path, "unknown")
        with self.assertRaises(ValidationError):
            ReleaseSourceInventory(
                input_name="tenant",
                repository="example/site",
                commit="a" * 40,
                files=(file("docs/a.md", "docs_ci"),) * 2,
            )
        with self.assertRaises(ValidationError):
            ArtifactReleaseCompatibility(
                complete=True,
                read_write_compatible=True,
                sources=(),
                modules=(ReleaseModuleDeclaration(name="site", depends=("missing",)),),
            )

    def test_release_reads_back_the_exact_classification_and_refuses_tuple_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            stores = (
                FilesystemRecordStore(Path(directory) / "files"),
                PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/state.db"),
            )
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                        self.addCleanup(store.close)
                    seed(store)
                    before = artifact("artifact-prod")
                    after = artifact("artifact-testing")
                    store.write_artifact_manifest(before)
                    store.write_artifact_manifest(after)
                    # The existing release compilation path classifies without a
                    # caller-supplied class or mutable settings.
                    review = build_release_review(store=store, profile=profile(), read=github_read)
                    assert review.checklist is not None
                    expected = review.checklist.database_compatibility
                    assert expected is not None
                    self.assertEqual(
                        review.checklist_digest,
                        checklist_digest(
                            review.checklist.model_copy(update={"database_compatibility": None})
                        ),
                    )
                    decision = ReleaseReviewDecisionRecord(
                        record_id="decision",
                        product=profile().product,
                        checklist=review.checklist,
                        checklist_digest=review.checklist_digest,
                        decision="accepted",
                        actor_github_id=profile().owner.github_id,
                        actor_github_login="client",
                        decided_at="2026-10-09T00:00:00Z",
                    )
                    saved = store.create_release_review_decision_record_if_absent(decision)
                    actual = store.read_release_review_decision_record(
                        product=decision.product, record_id=saved.record_id
                    )
                    self.assertEqual(actual.checklist.database_compatibility, expected)
                    self.assertEqual(
                        expected.candidate_image, f"{after.image.repository}@{after.image.digest}"
                    )
                    self.assertEqual(expected.classification, "compatible")
                    payload = review.checklist.model_dump(mode="json")
                    payload["database_compatibility"]["candidate_artifact_id"] = "another-release"
                    with self.assertRaises(ValidationError):
                        ReleaseChecklist.model_validate(payload)
                    # An artifact change still changes exact-tuple acceptance.
                    unexamined = after.model_copy(
                        deep=True,
                        update={"artifact_id": "unexamined", "release_compatibility": None},
                    )
                    store.write_artifact_manifest(unexamined)
                    current_tuple = store.read_release_tuple_record(
                        context_name="example-site", channel_name="testing"
                    )
                    store.write_release_tuple_record(
                        current_tuple.model_copy(update={"artifact_id": unexamined.artifact_id})
                    )
                    conservative = build_release_review(
                        store=store, profile=profile(), read=github_read
                    )
                    self.assertNotEqual(conservative.checklist_digest, review.checklist_digest)
