from control_plane.contracts.artifact_dependency_provenance import (
    ArtifactDependencyProvenance,
    ArtifactExternalCompatibilityInput,
    ArtifactPythonEnvironmentProvenance,
    ArtifactPythonPackage,
    ArtifactPythonPackageSource,
    ArtifactUvLockProvenance,
    artifact_python_package_inventory_sha256,
)
from control_plane.contracts.artifact_identity import (
    ArtifactAddonSource,
    ArtifactBaseImageProvenance,
    ArtifactBuildProvenance,
    ArtifactBuildToolProvenance,
    ArtifactIdentityManifest,
    ArtifactImageReference,
)


def artifact_dependency_provenance_v2(
    *, tenant_source_repository: str = "cbusillo/odoo-tenant-cm"
) -> ArtifactDependencyProvenance:
    packages = (
        ArtifactPythonPackage(
            name="httpx",
            version="0.28.1",
            source=ArtifactPythonPackageSource(kind="registry"),
        ),
        ArtifactPythonPackage(
            name="simple-zpl2",
            version="0.3.0",
            source=ArtifactPythonPackageSource(
                kind="vcs",
                repository="https://github.com/cbusillo/simple_zpl2.git",
                commit="7" * 40,
            ),
        ),
    )
    package_digest = artifact_python_package_inventory_sha256(packages)
    environment = ArtifactPythonEnvironmentProvenance(
        python_version="3.13.5",
        packages=packages,
        package_count=len(packages),
        packages_sha256=package_digest,
    )
    return ArtifactDependencyProvenance(
        target_platforms=("linux/amd64", "linux/arm64"),
        uv_locks=(
            ArtifactUvLockProvenance(
                scope="support_runtime",
                source_repository="cbusillo/odoo-devkit",
                source_ref="6" * 40,
                path="uv.lock",
                sha256="a" * 64,
            ),
            ArtifactUvLockProvenance(
                scope="tenant",
                source_repository=tenant_source_repository,
                source_ref="0" * 40,
                path="uv.lock",
                sha256="b" * 64,
            ),
        ),
        python_environments={
            "linux/amd64": environment,
            "linux/arm64": environment.model_copy(deep=True),
        },
        external_compatibility_inputs=(
            ArtifactExternalCompatibilityInput(
                source_repository="https://github.com/cbusillo/external-addon.git",
                source_ref="8" * 40,
                dependency_file_path="addons/external/pyproject.toml",
                dependency_file_sha256="9" * 64,
                format="pyproject_toml",
                resolution_posture="exact_source_unlocked",
            ),
        ),
    )


def artifact_manifest_v2(
    *,
    artifact_id: str = "artifact-cm-v2",
    image_repository: str = "ghcr.io/cbusillo/odoo-tenant-cm",
    odoo_install_modules: tuple[str, ...] = (),
    tenant_source_repository: str = "cbusillo/odoo-tenant-cm",
) -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        schema_version=2,
        artifact_id=artifact_id,
        source_commit="0" * 40,
        enterprise_base_digest=f"sha256:{'c' * 64}",
        odoo_install_modules=odoo_install_modules,
        addon_sources=(
            ArtifactAddonSource(
                repository="cbusillo/odoo-shared-addons",
                ref="3" * 40,
            ),
        ),
        build_provenance=ArtifactBuildProvenance(
            base_images=(
                ArtifactBaseImageProvenance(
                    role="runtime",
                    image=ArtifactImageReference(
                        repository="ghcr.io/cbusillo/odoo-runtime",
                        digest=f"sha256:{'d' * 64}",
                    ),
                    source_repository="cbusillo/odoo-docker",
                    source_ref="4" * 40,
                ),
                ArtifactBaseImageProvenance(
                    role="devtools",
                    image=ArtifactImageReference(
                        repository="ghcr.io/cbusillo/odoo-devtools",
                        digest=f"sha256:{'e' * 64}",
                    ),
                    source_repository="cbusillo/odoo-docker",
                    source_ref="5" * 40,
                ),
            ),
            build_tools=(
                ArtifactBuildToolProvenance(
                    name="odoo-devkit",
                    source_repository="cbusillo/odoo-devkit",
                    source_ref="6" * 40,
                ),
            ),
        ),
        dependency_provenance=artifact_dependency_provenance_v2(
            tenant_source_repository=tenant_source_repository
        ),
        image=ArtifactImageReference(
            repository=image_repository,
            digest=f"sha256:{'f' * 64}",
            tags=("v2-test",),
        ),
    )
