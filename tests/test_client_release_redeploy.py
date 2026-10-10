"""Isolated mid-step self-deploy rehearsal; no live providers or Client records."""

from datetime import UTC, datetime, timedelta
import os
import subprocess
import sys
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.client_release import read_client_release_run
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionRunResult
from control_plane.contracts.odoo_prod_rollback_operation import OdooProdRollbackResult
from control_plane.service_deploy_drain import (
    ServiceDeployDraining,
    ServiceDeployOutcomeUnknown,
    confirm_startup,
    prepare,
    read_status,
    record_dispatch,
)
from control_plane.storage.postgres import PostgresRecordStore, LaunchplaneServiceDeployDrainRow
from control_plane.workflows.odoo_stable_target_replacement import (
    TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
)
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests import test_client_release as odoo
from tests import test_generic_web_client_release as web
from tests.test_production_backup_provider import BackupHost, setup_memory_files

IMAGE = "example.invalid/launchplane@sha256:" + "a" * 64
MARKER = "isolated-replacement"


def _prepare(
    store: PostgresRecordStore, key: str = "rehearsal"
) -> tuple[Any, tuple[str, ...], bool]:
    return prepare(
        store,
        request_fingerprint=key,
        target_type="compose",
        target_id="isolated-control-plane",
        image_reference=IMAGE,
        deployment_marker=MARKER,
    )


def _replacement_env() -> dict[str, str]:
    return {"DOCKER_IMAGE_REFERENCE": IMAGE, "LAUNCHPLANE_DEPLOYMENT_MARKER": MARKER}


class ClientReleaseRedeployTests(unittest.TestCase):
    def setUp(self) -> None:
        setup_memory_files(self)

    def fixture(self, family: str) -> Any:
        fixture = (
            odoo.ClientReleaseTests(methodName="runTest")
            if family == "odoo"
            else web.GenericWebClientReleaseTests(methodName="runTest")
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.switch("promote_with_rollback_drill")
        return fixture

    def restart(self, fixture: Any) -> None:
        # Reopen durable records; replacement API startup is a separate process below.
        url = fixture.store.database_url
        fixture.store.close()
        fixture.store = PostgresRecordStore(database_url=url)
        self.addCleanup(fixture.store.close)
        fixture.store.ensure_schema()

    def start_replacement_api(self, fixture: Any) -> None:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.support.release_replacement_process",
                fixture.store.database_url,
            ],
            env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8", **_replacement_env()},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(read_status(fixture.store)["state"], "confirmed")

    def capture(self, fixture: Any, interrupt: Any = None) -> BackupHost:
        host = BackupHost(after_snapshot=interrupt)
        with (
            patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._utc_now_timestamp",
                side_effect=lambda: datetime.now(UTC).isoformat(),
            ),
            patch(
                "control_plane.workflows.production_backup_gate.control_plane_secrets.resolve_lane_worker_secret_values",
                return_value={
                    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "synthetic",
                    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "synthetic",
                },
            ),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ),
        ):
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=fixture.store,
                control_plane_root_path=fixture.root,
                lease_owner="isolated-backup-worker",
            )
        self.assertTrue(result.terminal_write_committed)
        return host

    def test_mid_step_replacement_drains_and_resumes_both_complete_drills_once(self) -> None:
        for family in ("odoo", "generic-web"):
            for interrupted in ("backup", "promote", "rollback", "post-check"):
                with self.subTest(family=family, interrupted=interrupted):
                    fixture = self.fixture(family)
                    accepted = fixture.accept()
                    effects: list[str] = []
                    drain_seen: list[str] = []

                    def interrupt(kind: str) -> None:
                        if kind != interrupted or drain_seen:
                            return
                        record, running, dispatch = _prepare(fixture.store)
                        self.assertEqual(record.state, "draining")
                        self.assertTrue(running, "an admitted step must remain a drain blocker")
                        self.assertFalse(
                            dispatch, "replacement must not dispatch during the effect"
                        )
                        self.assertEqual(fixture.advance(), ())
                        _, same_running, again = _prepare(fixture.store)
                        self.assertEqual(same_running, running)
                        self.assertFalse(again)
                        drain_seen.append(kind)

                    def promotion(**kwargs: Any) -> OdooProdPromotionRunResult:
                        kwargs["phase_checkpoint"]("validated")
                        kwargs["phase_checkpoint"]("promotion_started")
                        kwargs["provider_effect_checkpoint"](
                            TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE
                        )
                        effects.append("promote")
                        interrupt("promote")
                        fixture.set_prod(accepted.checklist.candidate.artifact_id)
                        fixture.store.write_deployment_record(
                            odoo._deployment(
                                f"deployment-{len(effects)}",
                                accepted.checklist.candidate.artifact_id,
                            )
                        )
                        # The target and durable deployment now exist; post-checks
                        # are still inside the admitted, unfinished operation.
                        interrupt("post-check")
                        return OdooProdPromotionRunResult(
                            context=odoo.CONTEXT,
                            from_instance="testing",
                            to_instance="prod",
                            request_id=kwargs["request"].request_id,
                            run_status="pass",
                            input_status="ready",
                            artifact_id=accepted.checklist.candidate.artifact_id,
                        )

                    def rollback(**kwargs: Any) -> OdooProdRollbackResult:
                        kwargs["provider_effect_checkpoint"](
                            TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE
                        )
                        effects.append("rollback")
                        interrupt("rollback")
                        fixture.set_prod(accepted.checklist.production.artifact_id)
                        return OdooProdRollbackResult(
                            context=odoo.CONTEXT,
                            instance="prod",
                            source_channel="previous-deployment",
                            artifact_id=accepted.checklist.production.artifact_id,
                            promotion_record_id="isolated-promotion",
                            rollback_status="pass",
                            rollback_health_status="pass",
                        )

                    if family == "odoo":
                        promotion_patch = patch(
                            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_promotion_run",
                            side_effect=promotion,
                        )
                        rollback_patch = patch(
                            "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_rollback",
                            side_effect=rollback,
                        )
                    else:
                        original_deploy = fixture.provider.execute_artifact_deploy

                        def deploy(**kwargs: Any) -> None:
                            original_deploy(**kwargs)
                            kind = (
                                "rollback"
                                if kwargs["runtime_identity"].artifact_id
                                == accepted.checklist.production.artifact_id
                                else "promote"
                            )
                            effects.append(kind)
                            interrupt(kind)

                        def health(**kwargs: Any) -> Any:
                            interrupt("post-check")
                            return fixture.healthcheck(**kwargs)

                        promotion_patch = patch.object(
                            fixture.provider, "execute_artifact_deploy", side_effect=deploy
                        )
                        rollback_patch = patch(
                            "control_plane.workflows.generic_web_promotion.wait_for_runtime_identity_healthcheck_with_retry",
                            side_effect=health,
                        )
                    with promotion_patch, rollback_patch:
                        for step in ("backup", "promote", "rollback", "backup", "promote"):
                            (operation_id,) = fixture.advance()
                            if step == "backup":
                                host = self.capture(fixture, lambda: interrupt("backup"))
                                self.assertEqual(
                                    sum(command[:1] == ["vzdump"] for command in host.commands), 1
                                )
                            elif family == "odoo":
                                fixture.run_release_worker()
                            if drain_seen and read_status(fixture.store)["state"] == "draining":
                                record, running, dispatch = _prepare(fixture.store)
                                self.assertFalse(running)
                                self.assertTrue(dispatch)
                                record_dispatch(fixture.store, record.request_fingerprint)
                                self.assertEqual(
                                    fixture.advance(),
                                    (),
                                    "old worker cannot admit during replacement",
                                )
                                self.restart(fixture)
                                confirm_startup(fixture.store)  # The old image cannot unlock it.
                                self.assertEqual(read_status(fixture.store)["state"], "requested")
                                self.start_replacement_api(fixture)
                                self.assertEqual(
                                    fixture.advance(),
                                    (),
                                    "old workers stay fenced after new API startup",
                                )
                                fixture.enterContext(patch.dict(os.environ, _replacement_env()))
                            else:
                                self.restart(fixture)
                            # Restart/poll retains the identity and completed step outcome.
                            run = read_client_release_run(
                                store=fixture.store,
                                profile=fixture.store.read_product_profile_record(accepted.product),
                                decision=accepted,
                            )
                            assert run is not None
                            self.assertEqual(
                                next(
                                    view.status
                                    for view in run.steps
                                    if view.operation_id == operation_id
                                ),
                                "pass",
                            )
                        self.assertEqual(drain_seen, [interrupted])
                        self.assertEqual(effects, ["promote", "rollback", "promote"])
                        self.assertEqual(fixture.advance(), ())
                        assert run is not None
                        self.assertEqual(run.state, "passed")
                        self.assertTrue(all(view.status == "pass" for view in run.steps))
                    fixture.doCleanups()

    def test_admission_racing_replacement_is_fenced_and_new_client_decision_waits(self) -> None:
        fixture = self.fixture("odoo")
        accepted = fixture.accept()
        (operation_id,) = fixture.advance()
        record, running, dispatch = _prepare(fixture.store)
        self.assertTrue(dispatch)
        self.assertFalse(running)
        record_dispatch(fixture.store, record.request_fingerprint)
        self.assertIsNone(
            fixture.store.claim_next_verireel_prod_backup_gate_operation_record(
                lease_owner="racing-worker",
                lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                claimed_at=datetime.now(UTC).isoformat(),
            )
        )
        with self.assertRaises(ServiceDeployDraining):
            fixture.store.reserve_mutation(
                scope="client-release",
                route_path="/isolated",
                idempotency_key="racing",
                request_fingerprint="racing",
                lease_owner="racing",
            )
        self.assertEqual(
            fixture.store.read_verireel_prod_backup_gate_operation_record(operation_id).status,
            "pending",
        )
        self.assertIsNotNone(
            read_client_release_run(
                store=fixture.store,
                profile=fixture.store.read_product_profile_record(accepted.product),
                decision=accepted,
            )
        )

    def test_mid_recovery_replacement_keeps_failure_truthful_and_never_repeats_effects(
        self,
    ) -> None:
        for family in ("odoo", "generic-web"):
            with self.subTest(family=family):
                fixture = self.fixture(family)
                drains: list[str] = []

                def drain() -> None:
                    record, running, dispatch = _prepare(fixture.store)
                    self.assertEqual(record.state, "draining")
                    self.assertTrue(running)
                    self.assertFalse(dispatch)
                    drains.append(record.state)

                if family == "odoo":
                    accepted, source = fixture.queued_promotion()
                    with fixture.promotion_providers(failure="health"):
                        fixture.run_release_worker()
                    (recovery,) = fixture.store.list_odoo_prod_rollback_operation_records()

                    def recover(**kwargs: Any) -> OdooProdRollbackResult:
                        kwargs["provider_effect_checkpoint"](
                            TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE
                        )
                        drain()
                        fixture.set_prod(recovery.target.artifact_id)
                        return OdooProdRollbackResult(
                            context=odoo.CONTEXT,
                            instance="prod",
                            source_channel="previous-deployment",
                            artifact_id=recovery.target.artifact_id,
                            promotion_record_id=recovery.request.promotion_record_id,
                            rollback_status="pass",
                            rollback_health_status="pass",
                        )

                    with patch(
                        "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_rollback",
                        side_effect=recover,
                    ) as apply:
                        fixture.run_release_worker()
                        self.assertEqual(apply.call_count, 1)
                    self.assertEqual(
                        fixture.store.read_odoo_prod_promotion_operation_record(
                            source.operation_id
                        ).status,
                        "fail",
                    )
                    self.assertEqual(
                        fixture.store.read_odoo_prod_rollback_operation_record(
                            recovery.operation_id
                        ).status,
                        "pass",
                    )
                else:
                    accepted = fixture.accept()
                    fixture.advance()
                    fixture.capture()
                    fixture.fail_health = True
                    original = fixture.provider.execute_artifact_deploy

                    def deploy(**kwargs: Any) -> None:
                        original(**kwargs)
                        if (
                            kwargs["runtime_identity"].artifact_id
                            == accepted.checklist.production.artifact_id
                        ):
                            drain()

                    with patch.object(
                        fixture.provider, "execute_artifact_deploy", side_effect=deploy
                    ):
                        fixture.advance()
                    self.assertEqual(
                        fixture.provider.deployed_artifacts,
                        [
                            accepted.checklist.candidate.artifact_id,
                            accepted.checklist.production.artifact_id,
                        ],
                    )
                    (promotion,) = fixture.store.list_promotion_records()
                    self.assertEqual(promotion.rollback.status, "pass")
                self.assertEqual(drains, ["draining"])
                record, running, dispatch = _prepare(fixture.store)
                self.assertTrue(dispatch)
                self.assertFalse(running)
                record_dispatch(fixture.store, record.request_fingerprint)
                self.restart(fixture)
                with patch.dict(os.environ, _replacement_env()):
                    confirm_startup(fixture.store)
                    self.assertEqual(fixture.advance(), ())
                    run = read_client_release_run(
                        store=fixture.store,
                        profile=fixture.store.read_product_profile_record(accepted.product),
                        decision=accepted,
                    )
                    assert run is not None
                    self.assertEqual(
                        run.state,
                        "stopped",
                        "recovery must not rewrite a failed forward release as passed",
                    )
                fixture.doCleanups()

    def test_lost_dispatch_response_is_not_replayed_and_wrong_startup_cannot_unlock(self) -> None:
        fixture = self.fixture("odoo")
        _, _, dispatch = _prepare(fixture.store)
        self.assertTrue(dispatch)
        self.restart(fixture)
        with self.assertRaises(ServiceDeployOutcomeUnknown):
            _prepare(fixture.store)
        with patch.dict(
            os.environ, {**_replacement_env(), "LAUNCHPLANE_DEPLOYMENT_MARKER": "wrong"}
        ):
            confirm_startup(fixture.store)
        self.assertEqual(read_status(fixture.store)["state"], "dispatching")
        with patch.dict(os.environ, _replacement_env()):
            confirm_startup(fixture.store)
            self.assertEqual(read_status(fixture.store)["state"], "confirmed")
            _, _, dispatch = _prepare(fixture.store)
            self.assertFalse(dispatch)

    def test_a_later_replacement_does_not_erase_an_earlier_dispatch_receipt(self) -> None:
        fixture = self.fixture("odoo")
        _prepare(fixture.store)
        record_dispatch(fixture.store, "rehearsal")
        self.start_replacement_api(fixture)
        with patch.dict(os.environ, _replacement_env()):
            prepare(
                fixture.store,
                request_fingerprint="second-replacement",
                target_type="compose",
                target_id="isolated-control-plane",
                image_reference=IMAGE,
                deployment_marker="second-marker",
            )
            record_dispatch(fixture.store, "second-replacement")
        with patch.dict(
            os.environ, {**_replacement_env(), "LAUNCHPLANE_DEPLOYMENT_MARKER": "second-marker"}
        ):
            confirm_startup(fixture.store)
            self.restart(fixture)
            prior, _, dispatch = _prepare(fixture.store)
            self.assertFalse(dispatch, "replaying the old request cannot replace the service again")
            self.assertEqual(prior.state, "confirmed")
            self.assertEqual(
                read_status(fixture.store)["request_fingerprint"], "second-replacement"
            )

    def test_marker_bound_repair_replaces_uncertain_dispatch_without_replaying_it(self) -> None:
        for original_state in ("dispatching", "requested"):
            with self.subTest(original_state=original_state):
                fixture = self.fixture("odoo")
                _prepare(fixture.store)
                if original_state == "requested":
                    record_dispatch(fixture.store, "rehearsal")
                with self.assertRaises(ServiceDeployOutcomeUnknown):
                    prepare(
                        fixture.store,
                        request_fingerprint="unbound-repair",
                        target_type="compose",
                        target_id="isolated-control-plane",
                        image_reference=IMAGE,
                        deployment_marker="fresh-unbound-marker",
                    )
                for marker, target in (("wrong", "isolated-control-plane"), (MARKER, "wrong")):
                    with self.assertRaises(ServiceDeployOutcomeUnknown):
                        prepare(
                            fixture.store,
                            request_fingerprint="repair",
                            target_type="compose",
                            target_id=target,
                            image_reference=IMAGE,
                            deployment_marker="repair-marker",
                            supersedes_deployment_marker=marker,
                        )
                repair = dict(
                    request_fingerprint="repair",
                    target_type="compose",
                    target_id="isolated-control-plane",
                    image_reference=IMAGE,
                    deployment_marker="repair-marker",
                    supersedes_deployment_marker=MARKER,
                )
                _, running, dispatch = prepare(fixture.store, **repair)
                self.assertTrue(dispatch)
                self.assertFalse(running)
                with self.assertRaises(ServiceDeployOutcomeUnknown):
                    prepare(fixture.store, **repair)
                record_dispatch(fixture.store, "repair")
                self.assertFalse(prepare(fixture.store, **repair)[2])
                with patch.dict(os.environ, _replacement_env()):
                    confirm_startup(fixture.store)
                self.assertEqual(read_status(fixture.store)["state"], "requested")
                with patch.dict(
                    os.environ,
                    {**_replacement_env(), "LAUNCHPLANE_DEPLOYMENT_MARKER": "repair-marker"},
                ):
                    confirm_startup(fixture.store)
                    self.assertFalse(read_status(fixture.store)["admission_paused"])
                fixture.doCleanups()

    def test_refused_repair_restores_prior_uncertainty_and_worker_generation(self) -> None:
        from control_plane.service_deploy_drain import (
            ServiceDeployPreEffectRefused,
            record_pre_effect_refusal,
        )

        for settled in (False, True):
            with self.subTest(settled=settled):
                fixture = self.fixture("odoo")
                _prepare(fixture.store)
                if settled:
                    record_dispatch(fixture.store, "rehearsal")
                    self.start_replacement_api(fixture)
                repair = dict(
                    request_fingerprint="refused-repair",
                    target_type="compose",
                    target_id="isolated-control-plane",
                    image_reference=IMAGE,
                    deployment_marker="repair-marker",
                    supersedes_deployment_marker=MARKER,
                )
                prepare(fixture.store, **repair)
                record_pre_effect_refusal(fixture.store, "refused-repair")
                self.assertEqual(read_status(fixture.store)["request_fingerprint"], "rehearsal")
                with self.assertRaises(ServiceDeployPreEffectRefused):
                    prepare(fixture.store, **repair)
                with patch.dict(os.environ, _replacement_env()):
                    self.assertEqual(read_status(fixture.store)["admission_paused"], not settled)
                with patch.dict(
                    os.environ,
                    {**_replacement_env(), "LAUNCHPLANE_DEPLOYMENT_MARKER": "older-worker"},
                ):
                    self.assertTrue(read_status(fixture.store)["admission_paused"])
                fixture.doCleanups()

    def test_a_new_intent_cannot_reuse_a_previous_worker_generation_marker(self) -> None:
        fixture = self.fixture("odoo")
        _prepare(fixture.store)
        record_dispatch(fixture.store, "rehearsal")
        self.start_replacement_api(fixture)
        with self.assertRaises(ValueError):
            _prepare(fixture.store, "new-intent-old-marker")
        self.assertEqual(read_status(fixture.store)["request_fingerprint"], "rehearsal")

    def test_expired_heartbeat_does_not_prove_an_admitted_effect_has_finished(self) -> None:
        fixture = self.fixture("odoo")
        fixture.accept()
        fixture.advance()
        operation = fixture.store.claim_next_verireel_prod_backup_gate_operation_record(
            lease_owner="capture-still-running",
            lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            claimed_at=datetime.now(UTC).isoformat(),
        )
        assert operation is not None
        fixture.store.write_verireel_prod_backup_gate_operation_record(
            operation.model_copy(update={"lease_expires_at": "2000-01-01T00:00:00Z"})
        )
        _, running, dispatch = _prepare(fixture.store)
        self.assertEqual(running, (operation.operation_id,))
        self.assertFalse(dispatch, "heartbeat loss cannot authorize killing a provider effect")

    def test_abandoned_pre_effect_drain_expires_without_expiring_dispatch(self) -> None:
        fixture = self.fixture("odoo")
        fixture.accept()
        fixture.advance()
        operation = fixture.store.claim_next_verireel_prod_backup_gate_operation_record(
            lease_owner="admitted",
            lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            claimed_at=datetime.now(UTC).isoformat(),
        )
        assert operation is not None
        record, _, dispatch = _prepare(fixture.store)
        self.assertFalse(dispatch)
        with fixture.store._session_factory() as session:
            row = session.get(LaunchplaneServiceDeployDrainRow, "service")
            assert row is not None
            row.payload = record.model_copy(
                update={"expires_at": "2000-01-01T00:00:00Z"}
            ).model_dump()
            session.commit()
        self.assertFalse(read_status(fixture.store)["admission_paused"])
