import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import subprocess
import sys
import unittest
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.http_app import idempotency_scope
from control_plane.privileged_operation_worker import execute_approved_privileged_operations_once
from control_plane.service_auth import LocalOperatorIdentity, LocalOperatorPolicyRule
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.http import request
from tests.test_http_app_product_retirement import (
    NOW,
    _absent_observation,
    _apply_payload,
    _observation,
    _plan_payload,
)
from tests import test_http_app_product_retirement as retirement_http
from tests import test_privileged_operation_http as privileged_http
from tests.test_privileged_operation_http import _policy, _policy_record
from tests.test_privileged_operation_worker import _fernet_key


def _exercise_filesystem_retirement_disable(root: str) -> None:
    store = FilesystemRecordStore(Path(root))
    original = SecretRecord(
        secret_id="local-secret",
        scope="global",
        integration="fixture",
        name="fixture",
        current_version_id="v1",
        created_at=NOW,
        updated_at=NOW,
    )
    store.write_secret_record(original)
    assert store.disable_product_retirement_secret(
        expected_record=original, updated_at=NOW, updated_by="retirement"
    )
    disabled = store.read_secret_record(original.secret_id)
    assert disabled.status == "disabled"
    later = (datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat()
    assert store.disable_product_retirement_secret(
        expected_record=original, updated_at=later, updated_by="retry"
    )
    assert store.read_secret_record(original.secret_id) == disabled
    changed = disabled.model_copy(update={"current_version_id": "v2"})
    store.write_secret_record(changed)
    assert not store.disable_product_retirement_secret(
        expected_record=original, updated_at=later, updated_by="stale-retirement"
    )
    assert store.read_secret_record(original.secret_id) == changed
    assert not store.disable_product_retirement_secret(
        expected_record=original.model_copy(update={"secret_id": "missing"}),
        updated_at=later,
        updated_by="retirement",
    )


class FilesystemRetirementDisableTests(unittest.TestCase):
    def test_disable_preserves_authority_and_matching_replay_without_nested_lock(self) -> None:
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from tests.test_retirement_approved_rotation import "
                    "_exercise_filesystem_retirement_disable; "
                    "_exercise_filesystem_retirement_disable(sys.argv[1])",
                    directory,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=Path(__file__).resolve().parents[1],
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class RetirementApprovedRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_rotation_survives_stale_retirement(self) -> None:
        with TemporaryDirectory() as directory:
            await self.assert_approved_rotation_survives(
                f"sqlite+pysqlite:///{Path(directory) / 'rotation.sqlite3'}"
            )

    async def test_partial_disable_preserves_rotation_and_refuses_changed_authority(self) -> None:
        with TemporaryDirectory() as directory:
            await self.assert_approved_rotation_survives(
                f"sqlite+pysqlite:///{Path(directory) / 'partial-rotation.sqlite3'}",
                partial_disable=True,
            )

    async def assert_approved_rotation_survives(
        self, database_url: str, *, partial_disable: bool = False
    ) -> None:
        fixture = retirement_http.ProductRetirementHttpTests()
        store = fixture._store(Path("."), database_url=database_url)
        try:
            secret = SecretRecord(
                secret_id="rotation-secret",
                scope="context_instance",
                integration="fixture",
                name="fixture",
                context="example-site",
                instance="prod",
                current_version_id="v1",
                created_at=NOW,
                updated_at=NOW,
            )
            store.write_secret_record(secret)
            store.write_secret_version(
                SecretVersion(
                    version_id="v1",
                    secret_id=secret.secret_id,
                    created_at=NOW,
                    key_id="old-root",
                    ciphertext="opaque-old-fixture",
                )
            )
            store.write_secret_binding(
                SecretBinding(
                    binding_id="rotation-binding",
                    secret_id=secret.secret_id,
                    integration="fixture",
                    binding_key="FIXTURE_TOKEN",
                    context="example-site",
                    instance="prod",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            first_secret = secret.model_copy(
                update={
                    "secret_id": "z-first-secret",
                    "name": "first-fixture",
                    "current_version_id": "first-v1",
                }
            )
            if partial_disable:
                store.write_secret_record(first_secret)
                store.write_secret_version(
                    store.read_secret_version("v1").model_copy(
                        update={"secret_id": first_secret.secret_id, "version_id": "first-v1"}
                    )
                )
                store.write_secret_binding(
                    store.list_secret_bindings(integration="fixture")[0].model_copy(
                        update={
                            "binding_id": "first-binding",
                            "secret_id": first_secret.secret_id,
                            "binding_key": "FIRST_FIXTURE_TOKEN",
                        }
                    )
                )
            policy = _policy().model_copy(
                update={
                    "local_operators": (
                        LocalOperatorPolicyRule(
                            subjects=("local-owner-agent",),
                            token_labels=("local-owner-write",),
                            products=("example-site",),
                            contexts=("example-site",),
                            instances=("prod",),
                            actions=("product_retirement.plan", "product_retirement.apply"),
                        ),
                    )
                }
            )
            policy_record = _policy_record(policy)
            store.seed_authz_policy_if_absent(policy_record)
            app = fixture._app(
                store, actions=("product_retirement.plan", "product_retirement.apply")
            )
            with patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                return_value=_observation(),
            ):
                planned = await request(
                    app,
                    "POST",
                    "/v1/product-retirement",
                    headers=fixture.headers,
                    payload=_plan_payload(),
                )
            self.assertEqual(planned.status_code, 202, planned.text)
            retirement_payload = _apply_payload(planned.json())
            rotation_app = privileged_http.PrivilegedOperationHttpTests()._app(
                store=store, policy=policy
            )
            environment = {
                secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                    {
                        "active_key_id": "new-root",
                        "keys": {"old-root": _fernet_key(0), "new-root": _fernet_key(32)},
                    }
                )
            }
            # Only the cryptographic boundary is stubbed. Planning, approval,
            # fresh authorization, executor and atomic storage are real.
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("control_plane.secrets._decrypt_secret_value", return_value="fixture-value"),
                patch(
                    "control_plane.secrets._encrypt_secret_value",
                    return_value=("opaque-new-fixture", "new-root"),
                ),
            ):

                async def approve_rotation() -> None:
                    rotation_plan = await request(
                        rotation_app,
                        "POST",
                        "/v1/privileged-operations/plans",
                        payload={
                            "descriptor_id": "managed-secret-reencryption",
                            "source_event_id": "retirement-rotation-plan",
                            "request": {"reason": "Fixture approved rotation overlap"},
                        },
                    )
                    self.assertEqual(rotation_plan.status_code, 200, rotation_plan.text)
                    operation_id = rotation_plan.json()["record"]["operation_id"]
                    approved = await request(
                        rotation_app,
                        "POST",
                        f"/v1/privileged-operations/plans/{operation_id}/approve",
                        payload={
                            "source_event_id": "retirement-rotation-approval",
                            "reason": "Reviewed fixture rotation",
                        },
                    )
                    self.assertEqual(approved.status_code, 200, approved.text)
                    self.assertEqual(approved.json()["record"]["status"], "approved")

                if not partial_disable:
                    await approve_rotation()
                entered, released = Event(), Event()
                bindings_before = store.list_secret_bindings(integration="fixture")
                paused = False
                original_write = store.disable_product_retirement_secret

                def write(
                    *, expected_record: SecretRecord, updated_at: str, updated_by: str
                ) -> bool:
                    nonlocal paused
                    if expected_record.secret_id == secret.secret_id and not paused:
                        paused = True
                        entered.set()
                        if not released.wait(30):
                            raise TimeoutError("retirement secret pause timed out")
                    return original_write(
                        expected_record=expected_record,
                        updated_at=updated_at,
                        updated_by=updated_by,
                    )

                with (
                    patch.object(store, "disable_product_retirement_secret", write),
                    patch(
                        "control_plane.product_retirement.observe_tracked_dokploy_application",
                        return_value=_absent_observation(),
                    ),
                    patch(
                        "control_plane.product_retirement.dokploy_api.delete_dokploy_application"
                    ) as delete,
                ):
                    attempt = asyncio.create_task(
                        request(
                            app,
                            "POST",
                            "/v1/product-retirement",
                            headers=fixture.headers,
                            payload=retirement_payload,
                            capture_server_error_response=True,
                        )
                    )
                    try:
                        self.assertTrue(
                            await asyncio.to_thread(entered.wait, 10), "no secret pause"
                        )
                        if partial_disable:
                            self.assertEqual(
                                store.read_secret_record(first_secret.secret_id).status, "disabled"
                            )
                            self.assertEqual(
                                tuple(
                                    event.event_type
                                    for event in store.list_secret_audit_events(
                                        secret_id=first_secret.secret_id
                                    )
                                ),
                                ("disabled",),
                            )
                        if partial_disable:
                            await approve_rotation()
                        executed = await asyncio.to_thread(
                            execute_approved_privileged_operations_once,
                            record_store=store,
                        )
                        self.assertEqual(tuple(record.status for record in executed), ("executed",))
                        rotated = store.read_secret_record(secret.secret_id)
                        self.assertNotEqual(rotated.current_version_id, "v1")
                        rotation_events = store.list_secret_audit_events(secret_id=secret.secret_id)
                        self.assertEqual(
                            tuple(event.event_type for event in rotation_events), ("rotated",)
                        )
                        self.assertEqual(
                            rotation_events[0].metadata["new_version_id"],
                            rotated.current_version_id,
                        )
                    finally:
                        released.set()
                        response = await attempt
                    delete.assert_not_called()
                after = store.read_secret_record(secret.secret_id)
                self.assertEqual(after, rotated)
                bindings_after = store.list_secret_bindings(integration="fixture")
                expected_bindings = tuple(
                    binding.model_copy(
                        update={
                            "status": "disabled",
                            "updated_at": store.read_secret_record(
                                first_secret.secret_id
                            ).updated_at,
                        }
                    )
                    if partial_disable and binding.secret_id == first_secret.secret_id
                    else binding
                    for binding in bindings_before
                )
                self.assertEqual(
                    {binding.binding_id: binding for binding in bindings_after},
                    {binding.binding_id: binding for binding in expected_bindings},
                )
                if partial_disable:
                    disabled_first = store.read_secret_record(first_secret.secret_id)
                    first_events = store.list_secret_audit_events(secret_id=first_secret.secret_id)
                    self.assertEqual(disabled_first.status, "disabled")
                    self.assertEqual(
                        disabled_first.current_version_id, first_secret.current_version_id
                    )
                    self.assertEqual(
                        store.list_runtime_environment_records(
                            context_name="example-site", instance_name="prod"
                        ),
                        (),
                    )
                self.assertEqual(
                    store.read_product_profile_record("example-site").lifecycle_state, "retiring"
                )
                checkpoints = tuple(
                    record
                    for record in store.list_product_retirement_records(product="example-site")
                    if record.mutation_evidence.finalization_profile_sha256
                )
                self.assertTrue(checkpoints)
                checkpoint = checkpoints[0]
                self.assertTrue(checkpoint.mutation_evidence.provider_absence_verified)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "mutation_reconciliation_required"
                )
                self.assertEqual(
                    store.list_secret_audit_events(secret_id=secret.secret_id), rotation_events
                )
                self.assertEqual(
                    store.read_secret_version(rotated.current_version_id).key_id, "new-root"
                )
                reservation = store.read_idempotency_record(
                    scope=idempotency_scope(
                        LocalOperatorIdentity(
                            subject="local-owner-agent", token_label="local-owner-write"
                        )
                    ),
                    route_path="/v1/product-retirement",
                    idempotency_key=fixture.headers["Idempotency-Key"],
                )
                assert reservation is not None
                self.assertEqual(reservation.state, "reconcile_required")
                expired_at = (
                    datetime.fromisoformat(reservation.lease_expires_at.replace("Z", "+00:00"))
                    + timedelta(seconds=1)
                ).isoformat()
                with (
                    patch.object(store, "_database_mutation_timestamp", return_value=expired_at),
                    patch(
                        "control_plane.product_retirement.observe_tracked_dokploy_application",
                        return_value=_absent_observation(),
                    ),
                    patch(
                        "control_plane.product_retirement.dokploy_api.delete_dokploy_application"
                    ) as delete,
                ):
                    retry = await request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers=fixture.headers,
                        payload=retirement_payload,
                    )
                    replan = await request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers={**fixture.headers, "Idempotency-Key": "replacement-plan"},
                        payload=_plan_payload(),
                    )
                    conflict = await request(
                        app,
                        "POST",
                        "/v1/product-retirement",
                        headers={**fixture.headers, "Idempotency-Key": "other-retirement"},
                        payload=retirement_payload,
                    )
                self.assertEqual(replan.status_code, 409, replan.text)
                self.assertEqual(replan.json()["error"]["code"], "product_retirement_blocked")
                self.assertEqual(retry.status_code, 409, retry.text)
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertEqual(retry.json()["error"]["code"], "mutation_reconciliation_required")
                self.assertEqual(
                    conflict.json()["error"]["code"], "mutation_reconciliation_required"
                )
                self.assertEqual(store.read_secret_record(secret.secret_id), rotated)
                self.assertEqual(
                    store.list_secret_audit_events(secret_id=secret.secret_id), rotation_events
                )
                self.assertEqual(store.list_secret_bindings(integration="fixture"), bindings_after)
                self.assertEqual(
                    store.read_product_retirement_record(checkpoint.record_id), checkpoint
                )
                held = store.read_idempotency_record(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                )
                assert held is not None
                self.assertEqual(held.state, "reconcile_required")
                self.assertEqual(held.request_fingerprint, reservation.request_fingerprint)
                self.assertEqual(held.provider_target_key, reservation.provider_target_key)
                self.assertEqual(held.reconciliation_key, reservation.reconciliation_key)
                if partial_disable:
                    self.assertEqual(
                        store.read_secret_record(first_secret.secret_id), disabled_first
                    )
                    self.assertEqual(
                        store.list_secret_audit_events(secret_id=first_secret.secret_id),
                        first_events,
                    )
                delete.assert_not_called()
        finally:
            store.close()
