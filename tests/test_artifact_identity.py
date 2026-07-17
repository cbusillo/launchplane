import copy
import unittest

from pydantic import ValidationError

from control_plane.contracts.artifact_dependency_provenance import (
    ArtifactPythonPackage,
    artifact_python_package_inventory_sha256,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from tests.support.artifact_manifests import artifact_manifest_v2


class ArtifactIdentityManifestTests(unittest.TestCase):
    def test_v1_manifest_remains_readable_without_dependency_provenance(self) -> None:
        manifest = ArtifactIdentityManifest.model_validate(
            {
                "artifact_id": "artifact-cm-v1",
                "source_commit": "legacy-short-ref",
                "enterprise_base_digest": "sha256:legacy",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                    "digest": "sha256:legacy",
                },
            }
        )

        self.assertEqual(manifest.schema_version, 1)
        self.assertIsNone(manifest.dependency_provenance)

    def test_v1_manifest_rejects_unversioned_dependency_provenance(self) -> None:
        payload = artifact_manifest_v2().model_dump(mode="json")
        payload["schema_version"] = 1

        with self.assertRaisesRegex(
            ValidationError,
            "dependency_provenance requires schema_version 2",
        ):
            ArtifactIdentityManifest.model_validate(payload)

    def test_v2_manifest_accepts_complete_dependency_provenance(self) -> None:
        manifest = artifact_manifest_v2()
        provenance = manifest.dependency_provenance

        assert provenance is not None
        self.assertEqual(
            tuple(lock.scope for lock in provenance.uv_locks),
            ("support_runtime", "tenant"),
        )
        self.assertEqual(tuple(provenance.python_environments), provenance.target_platforms)
        package_source = provenance.python_environments["linux/amd64"].packages[1].source
        self.assertEqual(package_source.repository, "https://github.com/cbusillo/simple_zpl2.git")
        self.assertEqual(package_source.commit, "7" * 40)
        self.assertEqual(
            set(package_source.model_dump(mode="json")),
            {"kind", "repository", "commit"},
        )

    def test_v2_manifest_requires_dependency_and_build_provenance(self) -> None:
        missing_dependency = artifact_manifest_v2().model_dump(mode="json")
        missing_dependency.pop("dependency_provenance")
        with self.assertRaisesRegex(ValidationError, "requires dependency_provenance"):
            ArtifactIdentityManifest.model_validate(missing_dependency)

        missing_build = artifact_manifest_v2().model_dump(mode="json")
        missing_build["build_provenance"] = {"base_images": [], "build_tools": []}
        with self.assertRaisesRegex(ValidationError, "runtime and devtools"):
            ArtifactIdentityManifest.model_validate(missing_build)

    def test_v2_manifest_rejects_duplicate_or_missing_lock_scopes(self) -> None:
        duplicate = artifact_manifest_v2().model_dump(mode="json")
        provenance = duplicate["dependency_provenance"]
        assert isinstance(provenance, dict)
        locks = provenance["uv_locks"]
        assert isinstance(locks, list)
        locks[1]["scope"] = "support_runtime"
        with self.assertRaisesRegex(ValidationError, "duplicate uv lock scopes"):
            ArtifactIdentityManifest.model_validate(duplicate)

        missing = artifact_manifest_v2().model_dump(mode="json")
        missing_provenance = missing["dependency_provenance"]
        assert isinstance(missing_provenance, dict)
        missing_provenance["uv_locks"] = missing_provenance["uv_locks"][:1]
        with self.assertRaisesRegex(ValidationError, "requires support_runtime and tenant"):
            ArtifactIdentityManifest.model_validate(missing)

    def test_v2_manifest_binds_lock_refs_to_tenant_and_build_tool_sources(self) -> None:
        tenant_mismatch = artifact_manifest_v2().model_dump(mode="json")
        tenant_provenance = tenant_mismatch["dependency_provenance"]
        assert isinstance(tenant_provenance, dict)
        tenant_provenance["uv_locks"][1]["source_ref"] = "1" * 40
        with self.assertRaisesRegex(ValidationError, "must match source_commit"):
            ArtifactIdentityManifest.model_validate(tenant_mismatch)

        support_mismatch = artifact_manifest_v2().model_dump(mode="json")
        support_provenance = support_mismatch["dependency_provenance"]
        assert isinstance(support_provenance, dict)
        support_provenance["uv_locks"][0]["source_ref"] = "1" * 40
        with self.assertRaisesRegex(ValidationError, "must match build tool provenance"):
            ArtifactIdentityManifest.model_validate(support_mismatch)

    def test_v2_manifest_requires_unique_exact_source_build_tools(self) -> None:
        version_only = artifact_manifest_v2().model_dump(mode="json")
        version_only["build_provenance"]["build_tools"] = [
            {"name": "odoo-devkit", "version": "2026.7"}
        ]
        with self.assertRaisesRegex(ValidationError, "exact build tool source provenance"):
            ArtifactIdentityManifest.model_validate(version_only)

        duplicate = artifact_manifest_v2().model_dump(mode="json")
        duplicate["build_provenance"]["build_tools"].append(
            copy.deepcopy(duplicate["build_provenance"]["build_tools"][0])
        )
        with self.assertRaisesRegex(ValidationError, "duplicate build tools"):
            ArtifactIdentityManifest.model_validate(duplicate)

    def test_v2_manifest_rejects_malformed_hashes_and_mutable_refs(self) -> None:
        malformed_hash = artifact_manifest_v2().model_dump(mode="json")
        provenance = malformed_hash["dependency_provenance"]
        assert isinstance(provenance, dict)
        provenance["uv_locks"][0]["sha256"] = "tampered"
        with self.assertRaisesRegex(ValidationError, "64-character SHA-256"):
            ArtifactIdentityManifest.model_validate(malformed_hash)

        mutable_ref = artifact_manifest_v2().model_dump(mode="json")
        mutable_provenance = mutable_ref["dependency_provenance"]
        assert isinstance(mutable_provenance, dict)
        mutable_provenance["uv_locks"][0]["source_ref"] = "main"
        with self.assertRaisesRegex(ValidationError, "exact lowercase 40-character git commit"):
            ArtifactIdentityManifest.model_validate(mutable_ref)

        malformed_image_digest = artifact_manifest_v2().model_dump(mode="json")
        malformed_image_digest["image"]["digest"] = "sha256:tampered"
        with self.assertRaisesRegex(ValidationError, "lowercase sha256 digest"):
            ArtifactIdentityManifest.model_validate(malformed_image_digest)

    def test_v2_manifest_rejects_local_or_credential_bearing_sources(self) -> None:
        repository_values = (
            "/tmp/checkout",
            "file:///tmp/checkout",
            "https://token@github.com/cbusillo/private.git",
            "https://github.com/cbusillo/private.git?token=secret",
            "https://github.com/cbusillo/\ud800",
        )
        for repository in repository_values:
            with self.subTest(repository=repository):
                payload = artifact_manifest_v2().model_dump(mode="json")
                provenance = payload["dependency_provenance"]
                assert isinstance(provenance, dict)
                environments = provenance["python_environments"]
                environments["linux/amd64"]["packages"][1]["source"]["repository"] = repository
                with self.assertRaises(ValidationError):
                    ArtifactIdentityManifest.model_validate(payload)

    def test_v2_manifest_normalizes_safe_ssh_repository_identities(self) -> None:
        for repository in (
            "git@github.com:cbusillo/simple_zpl2.git",
            "ssh://git@github.com/cbusillo/simple_zpl2.git",
        ):
            with self.subTest(repository=repository):
                payload = artifact_manifest_v2().model_dump(mode="json")
                provenance = payload["dependency_provenance"]
                assert isinstance(provenance, dict)
                environments = provenance["python_environments"]
                environment = environments["linux/amd64"]
                environment["packages"][1]["source"]["repository"] = repository
                packages = tuple(
                    ArtifactPythonPackage.model_validate(package)
                    for package in environment["packages"]
                )
                environment["packages_sha256"] = artifact_python_package_inventory_sha256(packages)
                manifest = ArtifactIdentityManifest.model_validate(payload)
                dependency_provenance = manifest.dependency_provenance
                assert dependency_provenance is not None
                source = dependency_provenance.python_environments["linux/amd64"].packages[1].source
                self.assertEqual(
                    source.repository,
                    "ssh://git@github.com/cbusillo/simple_zpl2.git",
                )

    def test_v2_manifest_rejects_repository_path_traversal(self) -> None:
        for repository in (
            "owner/..",
            "https://github.com/owner/../repo.git",
            "git@github.com:owner/../repo.git",
        ):
            with self.subTest(repository=repository):
                payload = artifact_manifest_v2().model_dump(mode="json")
                provenance = payload["dependency_provenance"]
                assert isinstance(provenance, dict)
                environments = provenance["python_environments"]
                environments["linux/amd64"]["packages"][1]["source"]["repository"] = repository
                with self.assertRaisesRegex(ValidationError, "path traversal"):
                    ArtifactIdentityManifest.model_validate(payload)

    def test_v2_manifest_rejects_local_dependency_file_paths(self) -> None:
        payload = artifact_manifest_v2().model_dump(mode="json")
        provenance = payload["dependency_provenance"]
        assert isinstance(provenance, dict)
        provenance["external_compatibility_inputs"][0]["dependency_file_path"] = "../pyproject.toml"

        with self.assertRaisesRegex(ValidationError, "without traversal"):
            ArtifactIdentityManifest.model_validate(payload)

    def test_v2_manifest_rejects_incomplete_platform_evidence(self) -> None:
        payload = artifact_manifest_v2().model_dump(mode="json")
        provenance = payload["dependency_provenance"]
        assert isinstance(provenance, dict)
        provenance["python_environments"].pop("linux/arm64")

        with self.assertRaisesRegex(ValidationError, "evidence for every target platform"):
            ArtifactIdentityManifest.model_validate(payload)

    def test_v2_manifest_rejects_tampered_package_inventory(self) -> None:
        payload = artifact_manifest_v2().model_dump(mode="json")
        provenance = payload["dependency_provenance"]
        assert isinstance(provenance, dict)
        environment = provenance["python_environments"]["linux/amd64"]
        environment["packages"][0]["version"] = "99.0.0"

        with self.assertRaisesRegex(ValidationError, "inventory digest does not match"):
            ArtifactIdentityManifest.model_validate(payload)

        count_mismatch = artifact_manifest_v2().model_dump(mode="json")
        count_provenance = count_mismatch["dependency_provenance"]
        assert isinstance(count_provenance, dict)
        count_provenance["python_environments"]["linux/amd64"]["package_count"] = 99
        with self.assertRaisesRegex(ValidationError, "package_count does not match"):
            ArtifactIdentityManifest.model_validate(count_mismatch)

    def test_v2_manifest_rejects_noncanonical_package_identity(self) -> None:
        invalid_name = artifact_manifest_v2().model_dump(mode="json")
        name_provenance = invalid_name["dependency_provenance"]
        assert isinstance(name_provenance, dict)
        name_provenance["python_environments"]["linux/amd64"]["packages"][0]["name"] = (
            "not a package!"
        )
        with self.assertRaisesRegex(ValidationError, "canonical package syntax"):
            ArtifactIdentityManifest.model_validate(invalid_name)

        invalid_version = artifact_manifest_v2().model_dump(mode="json")
        version_provenance = invalid_version["dependency_provenance"]
        assert isinstance(version_provenance, dict)
        version_provenance["python_environments"]["linux/amd64"]["packages"][0]["version"] = ">=1"
        with self.assertRaisesRegex(ValidationError, "exact installed version"):
            ArtifactIdentityManifest.model_validate(invalid_version)

    def test_package_inventory_digest_is_canonical_across_input_order(self) -> None:
        manifest = artifact_manifest_v2()
        provenance = manifest.dependency_provenance
        assert provenance is not None
        packages = provenance.python_environments["linux/amd64"].packages

        self.assertEqual(
            artifact_python_package_inventory_sha256(packages),
            artifact_python_package_inventory_sha256(tuple(reversed(packages))),
        )

    def test_v2_manifest_rejects_duplicate_canonical_package_names(self) -> None:
        payload = artifact_manifest_v2().model_dump(mode="json")
        provenance = payload["dependency_provenance"]
        assert isinstance(provenance, dict)
        environment = provenance["python_environments"]["linux/amd64"]
        duplicate = copy.deepcopy(environment["packages"][0])
        environment["packages"].append(duplicate)
        environment["package_count"] += 1

        with self.assertRaisesRegex(ValidationError, "duplicate packages"):
            ArtifactIdentityManifest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
