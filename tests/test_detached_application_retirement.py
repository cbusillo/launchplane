import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Mapping
from typing import cast
from unittest.mock import patch

from control_plane.contracts.detached_application_retirement import (
    DetachedApplicationAuthorityAbsenceProof,
    DetachedApplicationAuthorityAbsenceSource,
    DetachedApplicationProtectedTargetSnapshot,
    DetachedApplicationProviderObservation,
    DetachedApplicationRetirementIdentity,
    DetachedApplicationRetirementRecord,
    DetachedApplicationRetirementRequest,
)
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.detached_application_retirement import (
    DetachedApplicationDiscovery,
    DetachedApplicationRetirementBlockedError,
    DetachedApplicationRetirementStore,
    DokployDetachedApplicationRetirementAdapter,
    build_detached_application_retirement_plan_record,
    discover_detached_application,
    prove_detached_application_authority_absence,
    redacted_detached_application_retirement_error,
    redacted_detached_application_retirement_response,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


NOW = "2026-08-11T12:00:00Z"
CANDIDATE_ID = "application-detached"
PROTECTED_IDS = ("application-production", "application-testing")


def _digest(value: str) -> str:
    from control_plane.contracts.product_retirement import provider_identifier_sha256

    return provider_identifier_sha256(value)


class _Lease:
    def __init__(self) -> None:
        self.phases: list[str] = []

    def assert_current(self) -> None:
        return None

    def checkpoint_effect(self, phase: str) -> None:
        self.phases.append(phase)


class _Store:
    def __init__(self) -> None:
        self.sources: dict[str, tuple[object, ...]] = {}
        self.records: dict[str, object] = {}

    def __getattr__(self, name: str) -> object:
        if name.startswith("list_"):
            source = name.removeprefix("list_").removesuffix("_records")
            if source == "secret_bindings":
                source = "secret_binding"
            if source == "preview_inventory_scan":
                source = "preview_inventory"
            if source == "runtime_environment_delete_events":
                source = "runtime_environment_delete"
            if source == "odoo_stable_target_replacement_operation":
                source = "stable_target_replacement"
            if source == "odoo_stable_bootstrap_operation":
                source = "odoo_stable_bootstrap"
            if source == "verireel_prod_backup_gate_operation":
                source = "verireel_prod_backup_gate"
            return lambda **_kwargs: self.sources.get(source, ())
        raise AttributeError(name)

    def write_detached_application_retirement_record(self, record: object) -> object:
        self.records[str(getattr(record, "record_id"))] = record
        return None


def _request(*, mode: str = "plan", **overrides: object) -> DetachedApplicationRetirementRequest:
    payload: dict[str, object] = {
        "mode": mode,
        "project_name": "example-project",
        "environment_name": "testing",
        "application_name": "detached-application",
        "candidate_target_sha256": _digest(CANDIDATE_ID),
        "expected_protected_target_sha256": tuple(sorted(_digest(item) for item in PROTECTED_IDS)),
        "reason": "Remove detached provider application.",
        "related_issue": "cbusillo/launchplane#2100",
    }
    payload.update(overrides)
    return DetachedApplicationRetirementRequest.model_validate(payload)


def _projects(*, include_candidate: bool = True) -> tuple[dict[str, object], ...]:
    testing_applications: list[dict[str, object]] = [
        {
            "applicationId": PROTECTED_IDS[1],
            "name": "tracked-testing-application",
            "environmentId": "environment-testing",
            "projectId": "project-1",
        }
    ]
    if include_candidate:
        testing_applications.append(
            {
                "applicationId": CANDIDATE_ID,
                "name": "detached-application",
                "environmentId": "environment-testing",
                "projectId": "project-1",
            }
        )
    return (
        {
            "projectId": "project-1",
            "name": "example-project",
            "environments": [
                {
                    "environmentId": "environment-testing",
                    "name": "testing",
                    "applications": testing_applications,
                },
                {
                    "environmentId": "environment-production",
                    "name": "production",
                    "applications": [
                        {
                            "applicationId": PROTECTED_IDS[0],
                            "name": "production-application",
                            "environmentId": "environment-production",
                            "projectId": "project-1",
                        }
                    ],
                },
            ],
        },
    )


def _payloads() -> dict[str, dict[str, object]]:
    return {
        CANDIDATE_ID: {
            "applicationId": CANDIDATE_ID,
            "name": "detached-application",
            "projectId": "project-1",
            "environmentId": "environment-testing",
            "applicationStatus": "idle",
            "environment": {
                "environmentId": "environment-testing",
                "name": "testing",
                "project": {"projectId": "project-1", "name": "example-project"},
            },
        },
        PROTECTED_IDS[0]: {
            "applicationId": PROTECTED_IDS[0],
            "name": "production-application",
            "projectId": "project-1",
            "environmentId": "environment-production",
            "applicationStatus": "running",
            "environment": {
                "environmentId": "environment-production",
                "name": "production",
                "project": {"projectId": "project-1", "name": "example-project"},
            },
        },
        PROTECTED_IDS[1]: {
            "applicationId": PROTECTED_IDS[1],
            "name": "tracked-testing-application",
            "projectId": "project-1",
            "environmentId": "environment-testing",
            "applicationStatus": "idle",
            "environment": {
                "environmentId": "environment-testing",
                "name": "testing",
                "project": {"projectId": "project-1", "name": "example-project"},
            },
        },
    }


def _discovery() -> DetachedApplicationDiscovery:
    protected = tuple(
        sorted(
            (
                DetachedApplicationProtectedTargetSnapshot(
                    application_id=target_id,
                    target_id_sha256=_digest(target_id),
                    application_name=f"protected-{index}",
                    application_fingerprint_sha256=_digest(f"fingerprint-{index}"),
                    domain_count=0,
                    domains_sha256=_digest(f"domains-{index}"),
                    deployment_history_state="no_history",
                    deployment_history_sha256=_digest(f"history-{index}"),
                )
                for index, target_id in enumerate(PROTECTED_IDS)
            ),
            key=lambda item: item.target_id_sha256,
        )
    )
    return DetachedApplicationDiscovery(
        candidate=DetachedApplicationProviderObservation(
            observed_at=NOW,
            project_id="project-1",
            project_name="example-project",
            environment_id="environment-testing",
            environment_name="testing",
            application_id=CANDIDATE_ID,
            application_name="detached-application",
            target_id_sha256=_digest(CANDIDATE_ID),
            state="present",
            application_state="idle",
            application_fingerprint_sha256=_digest("candidate-fingerprint"),
            deployment_history_state="no_history",
            safe_to_retire=True,
        ),
        protected_targets=protected,
    )


def _proof() -> DetachedApplicationAuthorityAbsenceProof:
    source = DetachedApplicationAuthorityAbsenceSource(
        source="provider_target",
        record_count=0,
        records_sha256=_digest("empty-source"),
    )
    return DetachedApplicationAuthorityAbsenceProof(
        candidate_target_sha256=_digest(CANDIDATE_ID),
        candidate_application_name_sha256=_digest("detached-application"),
        source_count=1,
        record_count=0,
        sources=(source,),
    )


def _plan(store: _Store | None = None) -> DetachedApplicationRetirementRecord:
    del store
    return build_detached_application_retirement_plan_record(
        request=_request(),
        identity=DetachedApplicationRetirementIdentity(actor="test", identity_kind="test"),
        trace_id="plan-trace",
        idempotency_key="retire-detached",
        requested_at=NOW,
        discovery=_discovery(),
        authority_absence_proof=_proof(),
    )


class DetachedApplicationRetirementTests(unittest.TestCase):
    def _discover(
        self,
        *,
        projects: object | None = None,
        payload_updates: Mapping[str, object] | None = None,
        domains: Mapping[str, object] | None = None,
        history_state: str = "no_history",
        request: DetachedApplicationRetirementRequest | None = None,
        absent: bool = False,
        payload_overrides: Mapping[str, Mapping[str, object]] | None = None,
        search_name_overrides: Mapping[str, str] | None = None,
        inventory_ids: tuple[str, ...] | None = None,
    ) -> DetachedApplicationDiscovery:
        payloads = _payloads()
        if payload_updates:
            payloads[CANDIDATE_ID].update(payload_updates)
        for application_id, updates in (payload_overrides or {}).items():
            payloads[application_id].update(updates)

        def fetch_payload(**kwargs: object) -> dict[str, object]:
            target_id = cast(str, kwargs["target_id"])
            if absent and target_id == CANDIDATE_ID:
                raise dokploy_api.DokployRequestFailed(
                    method="GET",
                    path="/api/application.one",
                    detail="not found",
                    status_code=404,
                )
            return payloads[target_id]

        def domain_request(**kwargs: object) -> object:
            application_id = cast(dict[str, str], kwargs["query"])["applicationId"]
            return (domains or {}).get(application_id, [])

        def search_applications(**kwargs: object) -> tuple[dict[str, object], ...]:
            selected = inventory_ids or (
                (PROTECTED_IDS[0], PROTECTED_IDS[1]) if absent else (CANDIDATE_ID, *PROTECTED_IDS)
            )
            return tuple(
                {
                    "applicationId": application_id,
                    "name": (search_name_overrides or {}).get(
                        application_id, cast(str, payloads[application_id]["name"])
                    ),
                }
                for application_id in selected
            )

        history = dokploy_api.DeploymentHistory(
            state=cast(dokploy_api.DeploymentHistoryState, history_state),
            latest_deployment=None,
        )
        with (
            patch(
                "control_plane.detached_application_retirement.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.invalid", "token"),
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.fetch_dokploy_projects",
                return_value=projects if projects is not None else _projects(),
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch_payload,
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.search_dokploy_applications",
                side_effect=search_applications,
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.dokploy_request",
                side_effect=domain_request,
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.deployment_history_for_target",
                return_value=history,
            ),
        ):
            return discover_detached_application(
                control_plane_root=Path("."),
                request=request or _request(),
                observed_at=NOW,
                absent_application_id=CANDIDATE_ID if absent else "",
                absent_project_id="project-1" if absent else "",
                absent_environment_id="environment-testing" if absent else "",
            )

    def test_request_requires_sorted_protected_set_and_excludes_candidate(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique and sorted"):
            _request(
                expected_protected_target_sha256=tuple(
                    reversed(_request().expected_protected_target_sha256)
                )
            )
        with self.assertRaisesRegex(ValueError, "cannot be a protected"):
            _request(expected_protected_target_sha256=(_digest(CANDIDATE_ID),))
        with self.assertRaisesRegex(ValueError, "apply-only"):
            _request(reviewed_plan_record_id="unexpected")

    def test_discovery_requires_idle_no_history_zero_domains_and_exact_protected_set(self) -> None:
        discovery = self._discover()
        self.assertTrue(discovery.candidate.safe_to_retire)
        self.assertEqual(
            tuple(item.target_id_sha256 for item in discovery.protected_targets),
            _request().expected_protected_target_sha256,
        )
        for payload_updates, domains, history_state, message in (
            ({"applicationStatus": "running"}, None, "no_history", "idle"),
            (
                {},
                {CANDIDATE_ID: [{"domainId": "domain-1", "host": "example.test"}]},
                "no_history",
                "domainless",
            ),
            ({}, None, "present", "no-history"),
        ):
            discovery = self._discover(
                payload_updates=payload_updates,
                domains=domains,
                history_state=history_state,
            )
            with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, message):
                build_detached_application_retirement_plan_record(
                    request=_request(),
                    identity=DetachedApplicationRetirementIdentity(
                        actor="test", identity_kind="test"
                    ),
                    trace_id="trace",
                    idempotency_key="key",
                    requested_at=NOW,
                    discovery=discovery,
                    authority_absence_proof=_proof(),
                )

    def test_discovery_fails_closed_on_ambiguity_malformed_and_digest_drift(self) -> None:
        duplicate = list(_projects())
        duplicate.append(
            {
                "projectId": "project-2",
                "name": "other-project",
                "environments": [
                    {
                        "environmentId": "environment-2",
                        "name": "testing",
                        "applications": [
                            {
                                "applicationId": "duplicate-name",
                                "name": "detached-application",
                            }
                        ],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, "globally unique"):
            self._discover(projects=tuple(duplicate))
        malformed = ({"projectId": "project-1", "name": "example-project"},)
        with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, "environments"):
            self._discover(projects=malformed)
        with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, "digest"):
            self._discover(request=_request(candidate_target_sha256="f" * 64))

    def test_discovery_falls_back_to_service_search_when_project_listing_is_inaccessible(
        self,
    ) -> None:
        discovery = self._discover(projects=())
        self.assertEqual(discovery.candidate.application_id, CANDIDATE_ID)
        self.assertEqual(
            tuple(target.target_id_sha256 for target in discovery.protected_targets),
            _request().expected_protected_target_sha256,
        )

    def test_discovery_uses_candidate_payload_to_disambiguate_duplicate_project_names(self) -> None:
        duplicate_projects = (
            *_projects(),
            {
                "projectId": "project-2",
                "name": "example-project",
                "environments": [
                    {
                        "environmentId": "environment-other",
                        "name": "production",
                        "applications": [],
                    }
                ],
            },
        )
        discovery = self._discover(projects=duplicate_projects)
        self.assertEqual(discovery.candidate.application_id, CANDIDATE_ID)
        self.assertEqual(discovery.candidate.project_id, "project-1")

    def test_service_search_protects_same_named_projects_across_distinct_ids(self) -> None:
        discovery = self._discover(
            projects=(),
            payload_overrides={
                PROTECTED_IDS[0]: {
                    "projectId": "project-2",
                    "environmentId": "environment-production",
                    "environment": {
                        "environmentId": "environment-production",
                        "name": "production",
                        "project": {"projectId": "project-2", "name": "example-project"},
                    },
                },
                PROTECTED_IDS[1]: {
                    "projectId": "project-3",
                    "environmentId": "environment-testing-other",
                    "environment": {
                        "environmentId": "environment-testing-other",
                        "name": "production",
                        "project": {"projectId": "project-3", "name": "example-project"},
                    },
                },
            },
        )
        self.assertEqual(
            tuple(target.target_id_sha256 for target in discovery.protected_targets),
            _request().expected_protected_target_sha256,
        )

    def test_project_listing_path_protects_same_named_projects_across_distinct_ids(self) -> None:
        candidate_only_project = (
            {
                "projectId": "project-1",
                "name": "example-project",
                "environments": [
                    {
                        "environmentId": "environment-testing",
                        "name": "testing",
                        "applications": [
                            {
                                "applicationId": CANDIDATE_ID,
                                "name": "detached-application",
                            }
                        ],
                    }
                ],
            },
        )
        discovery = self._discover(
            projects=candidate_only_project,
            payload_overrides={
                PROTECTED_IDS[0]: {
                    "projectId": "project-2",
                    "environmentId": "environment-production",
                    "environment": {
                        "environmentId": "environment-production",
                        "name": "production",
                        "project": {"projectId": "project-2", "name": "example-project"},
                    },
                },
                PROTECTED_IDS[1]: {
                    "projectId": "project-3",
                    "environmentId": "environment-testing-other",
                    "environment": {
                        "environmentId": "environment-testing-other",
                        "name": "production",
                        "project": {"projectId": "project-3", "name": "example-project"},
                    },
                },
            },
        )
        self.assertEqual(
            tuple(target.target_id_sha256 for target in discovery.protected_targets),
            _request().expected_protected_target_sha256,
        )

    def test_project_listing_path_rejects_candidate_omitted_from_inventory(self) -> None:
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError,
            "project listing contains application evidence missing from search inventory",
        ):
            self._discover(inventory_ids=PROTECTED_IDS)

    def test_project_listing_path_rejects_protected_target_omitted_from_inventory(self) -> None:
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError,
            "project listing contains application evidence missing from search inventory",
        ):
            self._discover(inventory_ids=(CANDIDATE_ID, PROTECTED_IDS[0]))

    def test_project_listing_path_rejects_duplicate_name_in_inventory(self) -> None:
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError,
            "globally unique accessible application name",
        ):
            self._discover(
                payload_overrides={PROTECTED_IDS[0]: {"name": "detached-application"}},
            )

    def test_service_search_rejects_search_payload_identity_drift(self) -> None:
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError,
            "application.search disagrees with application.one",
        ):
            self._discover(
                projects=(),
                search_name_overrides={PROTECTED_IDS[0]: "stale-application-name"},
            )

    def test_service_search_rejects_project_listing_target_omitted_from_inventory(self) -> None:
        duplicate_projects = (
            *_projects(),
            {
                "projectId": "project-2",
                "name": "example-project",
                "environments": [],
            },
        )
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError,
            "project listing contains application evidence missing from search inventory",
        ):
            self._discover(
                projects=duplicate_projects,
                inventory_ids=(CANDIDATE_ID, PROTECTED_IDS[0]),
            )

    def test_service_search_reconciliation_proves_reviewed_target_absent(self) -> None:
        discovery = self._discover(projects=(), absent=True)
        self.assertEqual(discovery.candidate.state, "absent")
        self.assertEqual(discovery.candidate.project_id, "project-1")
        self.assertEqual(discovery.candidate.environment_id, "environment-testing")

    def test_service_search_reconciliation_binds_absent_identifier_digest(self) -> None:
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError,
            "Stored detached application identifier does not match reviewed digest",
        ):
            self._discover(
                projects=(),
                absent=True,
                request=_request(candidate_target_sha256="f" * 64),
            )

    def test_project_listing_reconciliation_proves_reviewed_target_absent(self) -> None:
        discovery = self._discover(projects=_projects(include_candidate=False), absent=True)
        self.assertEqual(discovery.candidate.state, "absent")
        self.assertEqual(discovery.candidate.project_id, "project-1")
        self.assertEqual(discovery.candidate.environment_id, "environment-testing")

    def test_authority_absence_scans_every_required_source_and_preserves_target_name_distinction(
        self,
    ) -> None:
        store = _Store()
        proof = prove_detached_application_authority_absence(
            record_store=cast(DetachedApplicationRetirementStore, store),
            candidate_target_id=CANDIDATE_ID,
            candidate_application_name="detached-application",
        )
        self.assertGreaterEqual(proof.source_count, 18)
        source_names = tuple(source.source for source in proof.sources)
        for source_name in source_names:
            store = _Store()
            method_source = source_name
            if source_name == "environment_inventory":
                method_source = "environment_inventory"
            store.sources[method_source] = ({"target_id": CANDIDATE_ID},)
            with self.assertRaisesRegex(
                DetachedApplicationRetirementBlockedError,
                source_name,
            ):
                prove_detached_application_authority_absence(
                    record_store=cast(DetachedApplicationRetirementStore, store),
                    candidate_target_id=CANDIDATE_ID,
                    candidate_application_name="detached-application",
                )
        store = _Store()
        store.sources["dokploy_target"] = (
            DokployTargetRecord(
                context="example",
                instance="testing",
                target_type="application",
                target_name="detached-application",
                updated_at=NOW,
            ),
        )
        proof = prove_detached_application_authority_absence(
            record_store=cast(DetachedApplicationRetirementStore, store),
            candidate_target_id=CANDIDATE_ID,
            candidate_application_name="detached-application",
        )
        self.assertEqual(proof.match_count, 0)
        store.sources["provider_target"] = ({"display_name": "detached-application"},)
        with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, "provider_target"):
            prove_detached_application_authority_absence(
                record_store=cast(DetachedApplicationRetirementStore, store),
                candidate_target_id=CANDIDATE_ID,
                candidate_application_name="detached-application",
            )

    def test_deployment_authority_uses_consistent_application_target_identity(self) -> None:
        store = _Store()
        protected_target_id = PROTECTED_IDS[0]
        store.sources["deployment"] = (
            {
                "resolved_target": {
                    "target_type": "application",
                    "target_id": protected_target_id,
                    "target_name": "detached-application",
                },
                "deployed_target": {
                    "target_category": "application",
                    "provider_target_type": "application",
                    "target_id": protected_target_id,
                    "display_name": "detached-application",
                },
            },
        )
        proof = prove_detached_application_authority_absence(
            record_store=cast(DetachedApplicationRetirementStore, store),
            candidate_target_id=CANDIDATE_ID,
            candidate_application_name="detached-application",
        )
        deployment_source = next(
            source for source in proof.sources if source.source == "deployment"
        )
        self.assertEqual(deployment_source.match_count, 0)

    def test_deployment_authority_still_blocks_candidate_target_identity(self) -> None:
        store = _Store()
        store.sources["deployment"] = (
            {
                "resolved_target": {
                    "target_type": "application",
                    "target_id": CANDIDATE_ID,
                    "target_name": "detached-application",
                }
            },
        )
        with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, "deployment"):
            prove_detached_application_authority_absence(
                record_store=cast(DetachedApplicationRetirementStore, store),
                candidate_target_id=CANDIDATE_ID,
                candidate_application_name="detached-application",
            )

    def test_deployment_authority_retains_name_matching_without_consistent_application_id(
        self,
    ) -> None:
        payloads = (
            {"display_name": "detached-application"},
            {
                "deployed_target": {
                    "target_category": "compose",
                    "target_id": PROTECTED_IDS[0],
                    "display_name": "detached-application",
                }
            },
            {
                "resolved_target": {
                    "target_type": "application",
                    "target_id": PROTECTED_IDS[0],
                    "target_name": "detached-application",
                },
                "deployed_target": {
                    "target_category": "application",
                    "target_id": PROTECTED_IDS[1],
                    "display_name": "detached-application",
                },
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                store = _Store()
                store.sources["deployment"] = (payload,)
                with self.assertRaisesRegex(
                    DetachedApplicationRetirementBlockedError, "deployment"
                ):
                    prove_detached_application_authority_absence(
                        record_store=cast(DetachedApplicationRetirementStore, store),
                        candidate_target_id=CANDIDATE_ID,
                        candidate_application_name="detached-application",
                    )

    def test_provider_target_authority_uses_application_target_identity(self) -> None:
        store = _Store()
        store.sources["provider_target"] = (
            {
                "provider_id": "dokploy",
                "target_category": "application",
                "provider_target_type": "application",
                "target_id": PROTECTED_IDS[0],
                "display_name": "detached-application",
            },
        )
        proof = prove_detached_application_authority_absence(
            record_store=cast(DetachedApplicationRetirementStore, store),
            candidate_target_id=CANDIDATE_ID,
            candidate_application_name="detached-application",
        )
        self.assertEqual(proof.match_count, 0)

    def test_provider_target_authority_retains_candidate_and_weak_evidence_blocks(self) -> None:
        payloads = (
            {
                "target_category": "application",
                "provider_target_type": "application",
                "target_id": CANDIDATE_ID,
                "display_name": "detached-application",
            },
            {
                "target_category": "unknown",
                "provider_target_type": "application",
                "target_id": PROTECTED_IDS[0],
                "display_name": "detached-application",
            },
            {
                "target_category": "application",
                "provider_target_type": "",
                "target_id": PROTECTED_IDS[0],
                "display_name": "detached-application",
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                store = _Store()
                store.sources["provider_target"] = (payload,)
                with self.assertRaisesRegex(
                    DetachedApplicationRetirementBlockedError, "provider_target"
                ):
                    prove_detached_application_authority_absence(
                        record_store=cast(DetachedApplicationRetirementStore, store),
                        candidate_target_id=CANDIDATE_ID,
                        candidate_application_name="detached-application",
                    )

    def test_non_deployment_sources_retain_provider_name_authority(self) -> None:
        source_names = tuple(
            source.source
            for source in prove_detached_application_authority_absence(
                record_store=cast(DetachedApplicationRetirementStore, _Store()),
                candidate_target_id=CANDIDATE_ID,
                candidate_application_name="detached-application",
            ).sources
        )
        for source_name in source_names:
            if source_name in {"deployment", "provider_target"}:
                continue
            with self.subTest(source=source_name):
                store = _Store()
                store.sources[source_name] = ({"display_name": "detached-application"},)
                with self.assertRaisesRegex(DetachedApplicationRetirementBlockedError, source_name):
                    prove_detached_application_authority_absence(
                        record_store=cast(DetachedApplicationRetirementStore, store),
                        candidate_target_id=CANDIDATE_ID,
                        candidate_application_name="detached-application",
                    )

    def test_plan_is_deterministic_private_and_redacted(self) -> None:
        plan = _plan()
        replay = _plan()
        self.assertEqual(plan.record_id, replay.record_id)
        self.assertEqual(plan.plan_sha256, replay.plan_sha256)
        private_payload = plan.model_dump(mode="json")
        self.assertEqual(private_payload["candidate_observation"]["application_id"], CANDIDATE_ID)
        public_payload = redacted_detached_application_retirement_response(plan)
        serialized = json.dumps(public_payload, sort_keys=True)
        self.assertNotIn(CANDIDATE_ID, serialized)
        self.assertNotIn("application_id", serialized)
        result_payload = cast(dict[str, object], public_payload["result"])
        self.assertEqual(result_payload["authority_write_count"], 0)

    def test_adapter_deletes_once_after_checkpoint_and_verifies_protected_targets(self) -> None:
        store = _Store()
        plan = _plan()
        request = _request(
            mode="apply",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            confirmation=_request().expected_confirmation,
        )
        absent_candidate = DetachedApplicationProviderObservation(
            observed_at=NOW,
            project_id="project-1",
            project_name="example-project",
            environment_id="environment-testing",
            environment_name="testing",
            application_id=CANDIDATE_ID,
            application_name="detached-application",
            target_id_sha256=_digest(CANDIDATE_ID),
            state="absent",
        )
        absent = DetachedApplicationDiscovery(
            candidate=absent_candidate,
            protected_targets=plan.protected_targets,
        )
        adapter = DokployDetachedApplicationRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(DetachedApplicationRetirementStore, store),
            request=request,
            plan=plan,
            identity=DetachedApplicationRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="apply-trace",
            idempotency_key="apply-key",
            requested_at=NOW,
        )
        lease = _Lease()
        with (
            patch(
                "control_plane.detached_application_retirement.discover_detached_application",
                side_effect=(_discovery(), absent),
            ),
            patch(
                "control_plane.detached_application_retirement.prove_detached_application_authority_absence",
                return_value=plan.authority_absence_proof,
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.delete_dokploy_application"
            ) as delete_application,
        ):
            result = adapter.apply("provider-operation", lease)
        self.assertEqual(lease.phases, ["delete_application"])
        delete_application.assert_called_once_with(
            host="host",
            token="token",
            application_id=CANDIDATE_ID,
        )
        self.assertTrue(result.provider_effect_performed)
        terminal = next(iter(store.records.values()))
        self.assertEqual(getattr(terminal, "authority_write_count"), 0)
        self.assertEqual(
            adapter.target_key(),
            f"provider-target:dokploy:application:{CANDIDATE_ID}",
        )

    def test_adapter_handles_already_absent_and_unknown_outcome(self) -> None:
        store = _Store()
        plan = _plan()
        request = _request(
            mode="apply",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            confirmation=_request().expected_confirmation,
        )
        absent = DetachedApplicationDiscovery(
            candidate=plan.candidate_observation.model_copy(
                update={"state": "absent", "safe_to_retire": False}
            ),
            protected_targets=plan.protected_targets,
        )
        adapter = DokployDetachedApplicationRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(DetachedApplicationRetirementStore, store),
            request=request,
            plan=plan,
            identity=DetachedApplicationRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="absent-trace",
            idempotency_key="absent-key",
            requested_at=NOW,
        )
        with (
            patch(
                "control_plane.detached_application_retirement.discover_detached_application",
                return_value=absent,
            ),
            patch(
                "control_plane.detached_application_retirement.prove_detached_application_authority_absence",
                return_value=plan.authority_absence_proof,
            ),
        ):
            result = adapter.apply("operation", _Lease())
        self.assertFalse(result.provider_effect_performed)

        adapter = DokployDetachedApplicationRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(DetachedApplicationRetirementStore, _Store()),
            request=request,
            plan=plan,
            identity=DetachedApplicationRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="unknown-trace",
            idempotency_key="unknown-key",
            requested_at=NOW,
        )
        with (
            patch(
                "control_plane.detached_application_retirement.discover_detached_application",
                return_value=_discovery(),
            ),
            patch(
                "control_plane.detached_application_retirement.prove_detached_application_authority_absence",
                return_value=plan.authority_absence_proof,
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.detached_application_retirement.dokploy_api.delete_dokploy_application",
                side_effect=TimeoutError,
            ),
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("operation", _Lease())

    def test_reconciliation_adopts_verified_absence_after_delete_checkpoint(self) -> None:
        store = _Store()
        plan = _plan()
        request = _request(
            mode="apply",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            confirmation=_request().expected_confirmation,
        )
        absent = DetachedApplicationDiscovery(
            candidate=plan.candidate_observation.model_copy(
                update={"state": "absent", "safe_to_retire": False}
            ),
            protected_targets=plan.protected_targets,
        )
        adapter = DokployDetachedApplicationRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(DetachedApplicationRetirementStore, store),
            request=request,
            plan=plan,
            identity=DetachedApplicationRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="reconcile-trace",
            idempotency_key="reconcile-key",
            requested_at=NOW,
        )
        with (
            patch(
                "control_plane.detached_application_retirement.discover_detached_application",
                return_value=absent,
            ),
            patch(
                "control_plane.detached_application_retirement.prove_detached_application_authority_absence",
                return_value=plan.authority_absence_proof,
            ),
        ):
            observation = adapter.observe(
                "provider-operation", "delete_application", adapter.reconciliation_key()
            )
        self.assertEqual(observation.outcome, "present")
        result_payload = cast(dict[str, object], observation.response_payload["result"])
        self.assertEqual(result_payload["outcome"], "retired")
        terminal = cast(
            DetachedApplicationRetirementRecord,
            next(iter(store.records.values())),
        )
        self.assertTrue(terminal.mutation_evidence.provider_effect_attempted)
        self.assertTrue(terminal.mutation_evidence.provider_effect_performed)
        self.assertEqual(terminal.mutation_evidence.provider_effect_phases, ("delete_application",))

    def test_reconciliation_retries_present_candidate_and_tolerates_unrelated_authority_churn(
        self,
    ) -> None:
        plan = _plan()
        request = _request(
            mode="apply",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            confirmation=_request().expected_confirmation,
        )
        changed_proof = plan.authority_absence_proof.model_copy(
            update={
                "record_count": plan.authority_absence_proof.record_count + 1,
                "sources": tuple(
                    source.model_copy(update={"record_count": source.record_count + 1})
                    for source in plan.authority_absence_proof.sources
                ),
            }
        )
        adapter = DokployDetachedApplicationRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(DetachedApplicationRetirementStore, _Store()),
            request=request,
            plan=plan,
            identity=DetachedApplicationRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="retry-trace",
            idempotency_key="retry-key",
            requested_at=NOW,
        )
        with (
            patch(
                "control_plane.detached_application_retirement.discover_detached_application",
                return_value=_discovery(),
            ),
            patch(
                "control_plane.detached_application_retirement.prove_detached_application_authority_absence",
                return_value=changed_proof,
            ),
        ):
            observation = adapter.observe(
                "provider-operation", "delete_application", adapter.reconciliation_key()
            )
        self.assertEqual(observation.outcome, "absent")
        self.assertTrue(observation.retry_safe)

    def test_provider_error_redaction_excludes_raw_identifiers_and_detail(self) -> None:
        raw_identifier = "provider-secret-identifier"
        error = dokploy_api.DokployRequestFailed(
            method="POST",
            path=f"/api/application.delete?applicationId={raw_identifier}",
            detail=f"failed for {raw_identifier}",
            status_code=500,
        )
        message = redacted_detached_application_retirement_error(error)
        self.assertNotIn(raw_identifier, message)
        self.assertNotIn("applicationId", message)
        self.assertEqual(message, "Dokploy API POST request failed (500).")

    def test_authority_absence_detects_embedded_target_id_but_not_embedded_name(self) -> None:
        store = _Store()
        store.sources["product_retirement"] = (
            {"evidence": f"provider target was {CANDIDATE_ID} during retirement"},
        )
        with self.assertRaisesRegex(
            DetachedApplicationRetirementBlockedError, "product_retirement"
        ):
            prove_detached_application_authority_absence(
                record_store=cast(DetachedApplicationRetirementStore, store),
                candidate_target_id=CANDIDATE_ID,
                candidate_application_name="detached-application",
            )
        store.sources["product_retirement"] = ({"note": "prefix-detached-application-suffix"},)
        proof = prove_detached_application_authority_absence(
            record_store=cast(DetachedApplicationRetirementStore, store),
            candidate_target_id=CANDIDATE_ID,
            candidate_application_name="detached-application",
        )
        self.assertEqual(proof.match_count, 0)

    def test_filesystem_and_postgres_append_only_parity(self) -> None:
        plan = _plan()
        with TemporaryDirectory() as temporary_directory_name:
            filesystem = FilesystemRecordStore(Path(temporary_directory_name) / "state")
            filesystem.write_detached_application_retirement_record(plan)
            filesystem.write_detached_application_retirement_record(plan)
            self.assertEqual(
                filesystem.read_detached_application_retirement_record(plan.record_id),
                plan,
            )
            changed = plan.model_copy(update={"reason": "changed", "continuity_sha256": "f" * 64})
            with self.assertRaisesRegex(ValueError, "append-only"):
                filesystem.write_detached_application_retirement_record(changed)

            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            postgres = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{database_path}")
            postgres.ensure_schema()
            postgres.write_detached_application_retirement_record(plan)
            postgres.write_detached_application_retirement_record(plan)
            self.assertEqual(
                postgres.list_detached_application_retirement_records(
                    candidate_target_sha256=plan.candidate_observation.target_id_sha256
                ),
                (plan,),
            )
            with self.assertRaisesRegex(ValueError, "append-only"):
                postgres.write_detached_application_retirement_record(changed)
            postgres.close()


if __name__ == "__main__":
    unittest.main()
