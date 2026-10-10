"""Classify the full verified artifact-input diff; never infer safe overlap."""

import hashlib
import json
from pathlib import PurePosixPath

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.artifact_release_compatibility import (
    ReleaseDatabaseCompatibility,
    ReleaseInputChange,
    ReleaseInputFile,
)


def _digest(manifest: ArtifactIdentityManifest | None) -> str:
    if manifest is None:
        return ""
    return hashlib.sha256(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _image(manifest: ArtifactIdentityManifest | None) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}" if manifest else ""


def _source_inputs(manifest: ArtifactIdentityManifest) -> dict[str, tuple[str, str]]:
    build = manifest.source_build
    if build is None or build.purpose != "release" or manifest.schema_version != 2:
        raise ValueError("unverified_artifact")
    sources = {"tenant": (build.repository, manifest.source_commit)}
    for source in manifest.addon_sources:
        name = f"addon:{source.repository}"
        if name in sources:
            raise ValueError("conflicting_source_inputs")
        sources[name] = (source.repository, source.ref)
    for base in manifest.build_provenance.base_images:
        sources[f"base:{base.role}"] = (base.source_repository, base.source_ref)
    for tool in manifest.build_provenance.build_tools:
        sources[f"tool:{tool.name}"] = (tool.source_repository, tool.source_ref)
    if manifest.dependency_provenance is not None:
        for external in manifest.dependency_provenance.external_compatibility_inputs:
            sources[f"external:{external.source_repository}:{external.dependency_file_path}"] = (
                external.source_repository,
                external.source_ref,
            )
    for selector in manifest.addon_selectors:
        if sources.get(f"addon:{selector.repository}") != (
            selector.repository,
            selector.resolved_ref,
        ):
            raise ValueError("conflicting_source_selector")
    return sources


def _files(manifest: ArtifactIdentityManifest) -> dict[tuple[str, str], ReleaseInputFile]:
    expected = _source_inputs(manifest)
    declaration = manifest.release_compatibility
    if declaration is None or not declaration.complete:
        raise ValueError("missing_complete_declaration")
    actual = {
        source.input_name: (source.repository, source.commit) for source in declaration.sources
    }
    if actual != expected:
        raise ValueError("unexamined_or_conflicting_source_inputs")
    return {
        (source.input_name, file.path): file
        for source in declaration.sources
        for file in source.files
    }


def _opaque_inputs(manifest: ArtifactIdentityManifest) -> dict[str, object]:
    # Refs are examined by the complete file inventories, while immutable base
    # digests, selectors, build flags, lock contents and installed environments
    # must agree. No dependency or framework change can hide in a tenant diff.
    payload = manifest.model_dump(mode="json")
    for key in ("artifact_id", "source_commit", "source_build", "image", "release_compatibility"):
        payload.pop(key, None)
    for source in payload["addon_sources"]:
        source.pop("ref")
    for source in payload["addon_selectors"]:
        source.pop("resolved_ref")
    for source in payload["build_provenance"]["base_images"]:
        source.pop("source_ref")
        source["image"].pop("tags")
    for source in payload["build_provenance"]["build_tools"]:
        source.pop("source_ref")
    dependency = payload.get("dependency_provenance")
    if dependency:
        for source in dependency["uv_locks"] + dependency["external_compatibility_inputs"]:
            source.pop("source_ref")
    payload.pop("odoo_install_modules")
    return payload


def _kind_conflicts(file: ReleaseInputFile) -> bool:
    path = PurePosixPath(file.path)
    if file.kind == "static":
        return "static" not in path.parts
    if file.kind == "code":
        return (
            path.suffix != ".py"
            or bool(
                {
                    "models",
                    "wizard",
                    "wizards",
                    "report",
                    "reports",
                    "security",
                    "migrations",
                    "data",
                    "views",
                }
                & set(path.parts)
            )
            or path.name in {"__manifest__.py", "__init__.py"}
        )
    if file.kind == "docs_ci":
        return not (
            path.suffix in {".md", ".rst", ".adoc"}
            or {"docs", "doc", ".github", "tests"} & set(path.parts)
            or path.name in {".gitignore", "Makefile", ".pre-commit-config.yaml"}
        )
    return False


def release_opaque_inputs_sha256(manifest: ArtifactIdentityManifest) -> str:
    """Producer/consumer fingerprint for the opaque-input examination contract."""
    return hashlib.sha256(
        json.dumps(_opaque_inputs(manifest), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _dependency_closure(roots: set[str], graph: dict[str, set[str]]) -> set[str]:
    resolved = set(roots)
    while True:
        expanded = resolved | {dep for name in resolved for dep in graph.get(name, ())}
        if expanded == resolved:
            return resolved
        resolved = expanded


def classify_release_database_compatibility(
    *, production: ArtifactIdentityManifest | None, candidate: ArtifactIdentityManifest
) -> ReleaseDatabaseCompatibility:
    """Pure comparison of immutable verified snapshots, with a resolved Odoo plan.

    Missing history/declarations retain the database-changing class, but an
    incomplete module plan is a refusal to execute, never a blanket upgrade.
    """
    reasons: set[str] = set()
    changes: list[ReleaseInputChange] = []
    changed_modules: set[str] = set()
    update_roots: set[str] = set()
    installs: set[str] = set()
    updates: set[str] = set()
    complete = False
    warm_assets = False
    if production is None:
        reasons.add("missing_production_history")
    else:
        try:
            before = _files(production)
            after = _files(candidate)
        except ValueError as error:
            reasons.add(str(error))
        else:
            complete = True
            old = production.release_compatibility
            new = candidate.release_compatibility
            assert old is not None and new is not None
            old_sources = {source.input_name: source for source in old.sources}
            for source in new.sources:
                previous = old_sources.get(source.input_name)
                if previous is not None and (
                    previous.repository == source.repository
                    and previous.commit == source.commit
                    and {file.path: file for file in previous.files}
                    != {file.path: file for file in source.files}
                ):
                    reasons.add("conflicting_immutable_source_inventory")
                    complete = False
            if (
                production.source_build is None
                or candidate.source_build is None
                or (
                    production.source_build.repository_id != candidate.source_build.repository_id
                    or production.source_build.repository != candidate.source_build.repository
                    or production.image.repository != candidate.image.repository
                )
            ):
                reasons.add("conflicting_artifact_identity")
                complete = False
            if not old.read_write_compatible or not new.read_write_compatible:
                reasons.add("read_write_compatibility_not_declared")
            if _opaque_inputs(production) != _opaque_inputs(candidate):
                reasons.add("changed_base_dependency_or_build_inputs")
                if (
                    old.opaque_inputs_sha256 != release_opaque_inputs_sha256(production)
                    or new.opaque_inputs_sha256 != release_opaque_inputs_sha256(candidate)
                    or new.database_update_modules is None
                ):
                    complete = False
                    reasons.add("unexamined_opaque_input_plan")
                else:
                    update_roots.update(new.database_update_modules)
                    changed_modules.update(new.database_update_modules)
            graph = {module.name: set(module.depends) for module in new.modules}
            old_graph = {module.name: set(module.depends) for module in old.modules}
            if old_graph != graph:
                reasons.add("changed_module_dependencies")
                changed_dependencies = {
                    name for name in graph if old_graph.get(name) != graph[name]
                }
                update_roots.update(changed_dependencies)
                changed_modules.update(changed_dependencies)
            installs = set(candidate.odoo_install_modules) - set(production.odoo_install_modules)
            if not set(candidate.odoo_install_modules) <= graph.keys():
                reasons.add("unexamined_install_requirements")
                complete = False
            if set(production.odoo_install_modules) - set(candidate.odoo_install_modules):
                reasons.add("removed_install_requirements")
                complete = False
            if installs:
                reasons.add("required_module_installs")
            for key in sorted(before.keys() | after.keys()):
                left, right = before.get(key), after.get(key)
                if left == right:
                    continue
                file = right or left
                assert file is not None
                changes.append(
                    ReleaseInputChange(
                        input_name=key[0],
                        path=key[1],
                        before_sha256=left.sha256 if left else "",
                        after_sha256=right.sha256 if right else "",
                        kind=file.kind,
                        module=file.module,
                    )
                )
                if file.module:
                    changed_modules.add(file.module)
                if left and right and (left.kind != right.kind or left.module != right.module):
                    reasons.add("conflicting_file_declarations")
                    complete = False
                if _kind_conflicts(file):
                    reasons.add("conflicting_file_declarations")
                    complete = False
                if file.kind == "manifest_assets" and (
                    left is None
                    or right is None
                    or left.manifest_database_sha256 != right.manifest_database_sha256
                ):
                    reasons.add("changed_manifest_database_fields")
                    update_roots.add(file.module)
                elif file.kind in {"static", "manifest_assets"}:
                    warm_assets = True
                elif file.kind in {"database_data", "model", "migration", "dependency"}:
                    reasons.add(f"changed_{file.kind}")
                    update_roots.add(file.module)
                elif file.kind == "unknown":
                    reasons.add("unexamined_file_input")
                    complete = False
                if left is not None and right is None and file.module and file.module not in graph:
                    reasons.add("removed_module")
                    complete = False
            if "" in update_roots:
                reasons.add("unresolved_module_for_database_change")
                complete = False
                update_roots.discard("")
            if not update_roots <= graph.keys():
                reasons.add("unresolved_changed_modules")
                complete = False
            # Follow Odoo's reverse dependency closure. Installs and their new
            # dependency closure are kept separate from already installed updates.
            required = _dependency_closure(set(candidate.odoo_install_modules), graph)
            old_required = _dependency_closure(set(production.odoo_install_modules), old_graph)
            installs = required - old_required
            if installs:
                reasons.add("required_module_installs")
            updates = set(update_roots) - installs
            while True:
                affected = {name for name in graph if graph[name] & (updates | installs)}
                expanded = updates | (affected - installs)
                if expanded == updates:
                    break
                updates = expanded
            if update_roots or installs:
                if old.read_write_compatible and new.read_write_compatible:
                    reasons.add("declaration_conflicts_with_required_database_work")
            if not reasons:
                reasons.add("verified_complete_read_write_compatible_diff")
    compatible = reasons == {"verified_complete_read_write_compatible_diff"}
    return ReleaseDatabaseCompatibility(
        production_artifact_id=production.artifact_id if production else "",
        candidate_artifact_id=candidate.artifact_id,
        production_image=_image(production),
        candidate_image=_image(candidate),
        production_manifest_sha256=_digest(production),
        candidate_manifest_sha256=_digest(candidate),
        classification="compatible" if compatible else "database_changing",
        reasons=tuple(sorted(reasons)),
        changes=tuple(changes),
        changed_modules=tuple(sorted(changed_modules)),
        install_modules=tuple(sorted(installs)),
        update_modules=tuple(sorted(updates)),
        module_plan_complete=complete,
        warm_assets=warm_assets,
        retain_previous_assets=warm_assets,
    )
