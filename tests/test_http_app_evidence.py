from control_plane.http_app import create_launchplane_fastapi_app
from tests.async_case import AsyncTestCase
from tests.http_app_test_support import (
    _backup_gate_evidence_payload,
    _backup_gate_write_identity,
    _backup_gate_write_policy,
    _BackupGateEvidenceOnlyStore,
    _deployment_evidence_payload,
    _deployment_write_identity,
    _deployment_write_policy,
    _DeploymentEvidenceOnlyStore,
    _IdempotencyOnlyBackupGateReplayStore,
    _IdempotencyOnlyPreviewDestroyedReplayStore,
    _IdempotencyOnlyPreviewGenerationReplayStore,
    _IdempotencyOnlyPromotionReplayStore,
    _IdempotencyOnlyReplayStore,
    _IdempotencyOnlyRunnerHostHygieneAuditReplayStore,
    _IdempotencyOnlyRunnerLaneRegistrationAuditReplayStore,
    _MissingProductReadStore,
    _post_backup_gate_evidence,
    _post_deployment_evidence,
    _post_preview_destroyed_evidence,
    _post_preview_generation_evidence,
    _post_promotion_evidence,
    _post_runner_host_hygiene_audit_evidence,
    _post_runner_lane_registration_audit_evidence,
    _preview_destroyed_evidence_payload,
    _preview_destroyed_write_identity,
    _preview_destroyed_write_policy,
    _preview_generation_evidence_payload,
    _preview_generation_write_identity,
    _preview_generation_write_policy,
    _preview_record_for_destroy,
    _promotion_evidence_payload,
    _promotion_write_identity,
    _promotion_write_policy,
    _PromotionEvidenceOnlyStore,
    _runner_host_hygiene_audit_payload,
    _runner_host_hygiene_audit_write_identity,
    _runner_host_hygiene_audit_write_policy,
    _runner_lane_registration_audit_payload,
    _runner_lane_registration_audit_write_identity,
    _runner_lane_registration_audit_write_policy,
)
from tests.test_service import _StubVerifier


class FastApiDeploymentEvidenceStoreGateTests(AsyncTestCase):
    async def test_deployment_evidence_accepts_store_without_promotion_methods(self) -> None:
        store = _DeploymentEvidenceOnlyStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _post_deployment_evidence(app, _deployment_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-example-site-prod")
        self.assertEqual(
            store.deployment_records["deployment-example-site-prod"]["context"],
            "example-site",
        )
        self.assertEqual(
            store.environment_inventories[0]["deployment_record_id"],
            "deployment-example-site-prod",
        )

    async def test_deployment_evidence_replays_idempotency_before_deployment_gate(self) -> None:
        store = _IdempotencyOnlyReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _deployment_evidence_payload()

        first_response = await _post_deployment_evidence(
            app,
            request_payload,
            idempotency_key="deployment-example-site-prod",
        )
        store.write_deployment_record = None
        store.write_environment_inventory = None
        second_response = await _post_deployment_evidence(
            app,
            request_payload,
            idempotency_key="deployment-example-site-prod",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_deployment_calls, 1)
        self.assertEqual(store.write_environment_inventory_calls, 1)


class FastApiBackupGateEvidenceStoreGateTests(AsyncTestCase):
    async def test_backup_gate_evidence_accepts_store_with_only_backup_gate_method(self) -> None:
        store = _BackupGateEvidenceOnlyStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["backup_gate_record_id"], "backup-gate-example-site-prod"
        )
        self.assertEqual(
            store.backup_gate_records["backup-gate-example-site-prod"]["context"],
            "example-site",
        )

    async def test_backup_gate_evidence_requires_backup_gate_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_backup_gate_evidence_replays_idempotency_before_backup_gate_gate(self) -> None:
        store = _IdempotencyOnlyBackupGateReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _backup_gate_evidence_payload()

        first_response = await _post_backup_gate_evidence(
            app,
            request_payload,
            idempotency_key="backup-gate-example-site-prod",
        )
        store.write_backup_gate_record = None
        second_response = await _post_backup_gate_evidence(
            app,
            request_payload,
            idempotency_key="backup-gate-example-site-prod",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_backup_gate_calls, 1)


class FastApiPromotionEvidenceStoreGateTests(AsyncTestCase):
    async def test_promotion_evidence_accepts_record_only_store_without_deployment_methods(
        self,
    ) -> None:
        store = _PromotionEvidenceOnlyStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _post_promotion_evidence(
            app,
            _promotion_evidence_payload(link_deployment=False),
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {"promotion_record_id": "promotion-example-site-testing-to-prod"},
        )
        self.assertEqual(
            store.promotion_records["promotion-example-site-testing-to-prod"]["context"],
            "example-site",
        )

    async def test_promotion_evidence_requires_linked_deployment_store_methods(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: _PromotionEvidenceOnlyStore(),
        )

        response = await _post_promotion_evidence(app, _promotion_evidence_payload())

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_promotion_evidence_requires_promotion_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_promotion_evidence(
            app,
            _promotion_evidence_payload(link_deployment=False),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_promotion_evidence_replays_idempotency_before_promotion_gate(self) -> None:
        store = _IdempotencyOnlyPromotionReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _promotion_evidence_payload(link_deployment=False)

        first_response = await _post_promotion_evidence(
            app,
            request_payload,
            idempotency_key="promotion-example-site-testing-to-prod",
        )
        # The second request must replay before capability checks or write calls.
        store.write_promotion_record = None
        second_response = await _post_promotion_evidence(
            app,
            request_payload,
            idempotency_key="promotion-example-site-testing-to-prod",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_promotion_calls, 1)


class FastApiPreviewGenerationEvidenceStoreGateTests(AsyncTestCase):
    async def test_preview_generation_evidence_requires_preview_generation_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_generation_write_identity()),
            authz_policy=_preview_generation_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_generation_evidence(
            app,
            _preview_generation_evidence_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_preview_generation_evidence_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyPreviewGenerationReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_generation_write_identity()),
            authz_policy=_preview_generation_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _preview_generation_evidence_payload()

        first_response = await _post_preview_generation_evidence(
            app,
            request_payload,
            idempotency_key="preview-generation-example-site-pr-42",
        )
        store.write_preview_generation_evidence_records = None
        second_response = await _post_preview_generation_evidence(
            app,
            request_payload,
            idempotency_key="preview-generation-example-site-pr-42",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_preview_generation_evidence_calls, 1)


class FastApiPreviewDestroyedEvidenceStoreGateTests(AsyncTestCase):
    async def test_preview_destroyed_evidence_requires_preview_destroyed_store(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_destroyed_write_identity()),
            authz_policy=_preview_destroyed_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_destroyed_evidence(
            app,
            _preview_destroyed_evidence_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_preview_destroyed_evidence_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyPreviewDestroyedReplayStore()
        store.seed_preview(_preview_record_for_destroy())
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_destroyed_write_identity()),
            authz_policy=_preview_destroyed_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _preview_destroyed_evidence_payload()

        first_response = await _post_preview_destroyed_evidence(
            app,
            request_payload,
            idempotency_key="preview-destroyed-example-site-pr-42",
        )
        store.write_preview_record = None
        second_response = await _post_preview_destroyed_evidence(
            app,
            request_payload,
            idempotency_key="preview-destroyed-example-site-pr-42",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_preview_record_calls, 1)


class FastApiRunnerHostHygieneAuditEvidenceStoreGateTests(AsyncTestCase):
    async def test_runner_host_hygiene_audit_evidence_requires_audit_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
            authz_policy=_runner_host_hygiene_audit_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_runner_host_hygiene_audit_evidence(
            app,
            _runner_host_hygiene_audit_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_runner_host_hygiene_audit_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyRunnerHostHygieneAuditReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
            authz_policy=_runner_host_hygiene_audit_write_policy(),
            record_store_factory=lambda: store,
        )
        request_payload = _runner_host_hygiene_audit_payload()

        first_response = await _post_runner_host_hygiene_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-host-hygiene:chris-testing:planned",
        )
        store.write_runner_host_hygiene_audit_record = None
        second_response = await _post_runner_host_hygiene_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-host-hygiene:chris-testing:planned",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_runner_host_hygiene_audit_calls, 1)


class FastApiRunnerLaneRegistrationAuditEvidenceStoreGateTests(AsyncTestCase):
    async def test_runner_lane_registration_audit_evidence_requires_audit_store(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
            authz_policy=_runner_lane_registration_audit_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_runner_lane_registration_audit_evidence(
            app,
            _runner_lane_registration_audit_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_runner_lane_registration_audit_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyRunnerLaneRegistrationAuditReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
            authz_policy=_runner_lane_registration_audit_write_policy(),
            record_store_factory=lambda: store,
        )
        request_payload = _runner_lane_registration_audit_payload()

        first_response = await _post_runner_lane_registration_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-lane-registration:cm-website:planned",
        )
        store.write_runner_lane_registration_audit_record = None
        second_response = await _post_runner_lane_registration_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-lane-registration:cm-website:planned",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_runner_lane_registration_audit_calls, 1)
