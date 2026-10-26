from datetime import UTC, datetime
from collections.abc import Callable
from control_plane.contracts.odoo_prod_promotion_operation import OdooProdPromotionOperationRecord
from control_plane.contracts.odoo_prod_rollback_operation import OdooProdRollbackOperationRecord
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from types import SimpleNamespace
import io
import base64
import json
import sys
import subprocess
import unittest
from unittest.mock import MagicMock, Mock, patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooCutoverRecord
from control_plane.contracts.prepared_public_site import PublicPauseRecord, WriterObservation
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.dokploy.slot_maintenance import maintenance_payload, payload_digest
from control_plane.dokploy.cutover_transport import CutoverHostTransport
from control_plane.odoo_cutover_plan import cutover_identity
from control_plane.odoo_cutover_recovery import startup_recovery_pause, publication_recovery_pause
from control_plane.prepared_public_site import prepare_public_site
from tests.test_odoo_cutover import IsolatedCutoverProvider
from tests.test_odoo_cutover_website_settings import snapshot
from tests.test_odoo_prod_promotion_operation import _operation, _rollback_operation, _store


class CutoverStartupRecoveryTests(unittest.TestCase):
    def test_failed_canonical_maintenance_attempt_cannot_start_a_second_registry_child(
        self,
    ) -> None:
        from control_plane.dokploy.slot_maintenance import run_planned_slot_maintenance

        with TemporaryDirectory() as temporary:
            provider = IsolatedCutoverProvider(Path(temporary) / "site", database_changing=True)
            self.addCleanup(provider.engine.dispose)
            plan = provider.cutover
            cursor = MagicMock()
            cursor.__enter__.return_value = cursor
            boundary = [
                ("database.uuid", plan.new.runtime_identity.database_uuid),
                ("odoo_devkit.credential_boundary_commit", "original"),
            ]
            cursor.fetchall.return_value = boundary
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.cursor.return_value = cursor
            child = Mock(
                return_value=subprocess.CompletedProcess([], 1, stdout=b"failed", stderr=b"")
            )
            resolver = Mock(return_value={"install_modules": [], "update_modules": []})

            def execute(argv: tuple[str, ...], stdin: str | None) -> str:
                if argv[1] == "inspect":
                    return plan.new.image
                assert stdin is not None
                program = argv[argv.index("-c") + 1]
                output = io.StringIO()
                with (
                    patch("sys.stdin", io.StringIO(stdin)),
                    patch("sys.argv", ["python", argv[-1]]),
                    patch("sys.path", list(sys.path)),
                    patch.dict(
                        "sys.modules",
                        {
                            "psycopg2": SimpleNamespace(connect=lambda **kwargs: connection),
                            "odoo_release_plan": SimpleNamespace(resolve_plan=resolver),
                        },
                    ),
                    patch.dict(
                        "os.environ",
                        {
                            "ODOO_DB_HOST": "isolated",
                            "ODOO_DB_USER": "fixture",
                            "ODOO_DB_PASSWORD": "inert",
                        },
                    ),
                    patch("subprocess.run", child),
                    redirect_stdout(output),
                ):
                    try:
                        exec(program, {})
                    except SystemExit as error:
                        if error.code not in (None, 0):
                            raise
                return output.getvalue()

            def remote(**kwargs: object) -> None:
                payload = kwargs["payload"]
                assert isinstance(payload, dict)
                receive = kwargs["receive"]
                assert callable(receive)

                def command(
                    argv: tuple[str, ...], **arguments: object
                ) -> subprocess.CompletedProcess[str]:
                    try:
                        stdin = arguments["input"]
                        assert stdin is None or isinstance(stdin, str)
                        result = execute(argv, stdin)
                    except Exception:
                        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
                    return subprocess.CompletedProcess(argv, 0, stdout=result, stderr="")

                output = io.StringIO()
                with (
                    patch(
                        "sys.argv",
                        [
                            "python",
                            base64.b64encode(json.dumps(payload).encode()).decode(),
                            "frame:",
                        ],
                    ),
                    patch("subprocess.run", side_effect=command),
                    redirect_stdout(output),
                ):
                    exec(str(kwargs["program"]), {})
                for line in output.getvalue().splitlines():
                    receive(json.loads(line.removeprefix("frame:")))

            transport = CutoverHostTransport(
                host="https://provider.invalid", token="inert", server_id="fixture", guard=Mock()
            )
            directory = Path(temporary) / "state"
            with (
                patch(
                    "control_plane.dokploy.slot_maintenance.slot_run_directory",
                    return_value=str(directory),
                ),
                patch("control_plane.dokploy.cutover_transport._read_remote", side_effect=remote),
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "maintenance failed"):
                        run_planned_slot_maintenance(
                            plan, runner_container="fixture", execute=transport
                        )
                for incomplete in (
                    "started",
                    json.dumps({"state": "failed", "plan_sha256": "foreign"}),
                ):
                    (directory / "attempt.json").write_text(incomplete)
                    (directory / "run.log").write_text("partial")
                    with self.subTest(incomplete=incomplete), self.assertRaises(RuntimeError):
                        run_planned_slot_maintenance(
                            plan, runner_container="fixture", execute=transport
                        )
                (directory / "attempt.json").write_text("started")
                (directory / "receipt.json").write_text(json.dumps({"state": "passed"}))
                with self.assertRaises(RuntimeError):
                    run_planned_slot_maintenance(
                        plan, runner_container="fixture", execute=transport
                    )
            child.assert_called_once()

    def test_only_original_held_operation_can_reclaim_proven_interrupted_startup(self) -> None:
        for rollback in (False, True):
            with self.subTest(rollback=rollback), TemporaryDirectory() as temporary:
                store = _store(temporary)
                self.addCleanup(store.close)
                provider = IsolatedCutoverProvider(Path(temporary) / "fixture")
                self.addCleanup(provider.engine.dispose)
                operation = _rollback_operation() if rollback else _operation()
                release = "original-release" if rollback else _operation().request.request_id
                pair = provider.cutover
                slots = [
                    slot.model_copy(
                        update={
                            "binding": slot.binding.model_copy(
                                update={
                                    "product_id": operation.product,
                                    "environment_id": operation.instance,
                                    "release_id": release,
                                }
                            ),
                            "runtime_identity": slot.runtime_identity.model_copy(
                                update={
                                    "product": operation.product,
                                    "context": operation.context,
                                    "instance": operation.instance,
                                    "release_id": release if slot == pair.new else "previous",
                                }
                            ),
                        }
                    )
                    for slot in (pair.old, pair.new)
                ]
                pair = OdooCutoverPlan.model_validate(
                    {**pair.model_dump(), "release_id": release, "old": slots[0], "new": slots[1]}
                )
                pair = OdooCutoverPlan.model_validate(
                    {**pair.model_dump(), "website_settings": snapshot(pair)}
                )
                provider.plan = provider.plan.model_copy(update={"binding": pair.old.binding})
                provider.binding = pair.old.binding
                site = prepare_public_site(provider.plan, provider)
                receipt = provider.maintenance(pair).model_copy(
                    update={"evidence_id": payload_digest(maintenance_payload(pair))}
                )
                backup = provider.backup(pair)
                identity = cutover_identity(
                    operation.product, operation.context, operation.instance, release
                )
                pause_id = identity + "-rollback" if rollback else identity
                now = datetime.now(UTC)
                pause = PublicPauseRecord(
                    pause_id=pause_id,
                    binding=pair.old.binding,
                    content_digest=site.content_digest,
                    began_at=now,
                    drained_at=now,
                    state="drained",
                    served_mode="prepared-public",
                    writers=tuple(
                        WriterObservation(
                            binding=pair.old.binding,
                            pause_id=pause_id,
                            writer=writer,
                            fenced=True,
                            active_jobs=0,
                            evidence_id="native-observation",
                        )
                        for writer in pair.writers
                    ),
                )
                progress = OdooCutoverRecord(
                    record_id=identity,
                    plan=pair,
                    began_at=now,
                    updated_at=now,
                    step="rolling_back" if rollback else "maintaining",
                    maintenance=receipt,
                    backup=backup,
                    public_site=site,
                    website_settings_attempted=True,
                    rollback_pause=pause if rollback else None,
                    pause=None if rollback else pause,
                )
                self.assertEqual(startup_recovery_pause(progress), pause)
                for step in (
                    ("rolling_back",)
                    if rollback
                    else ("maintaining", "ready", "switching", "resuming")
                ):
                    self.assertEqual(
                        publication_recovery_pause(progress.model_copy(update={"step": step})),
                        pause,
                    )
                held = operation.model_copy(
                    update={
                        "status": "reconciliation_required",
                        "phase": "rollback_started" if rollback else "promotion_started",
                        "attempt": 1,
                        "error_code": "operation_reconciliation_required",
                        "error_message": "Original startup completion was lost.",
                    }
                )
                claim: Callable[
                    ..., OdooProdPromotionOperationRecord | OdooProdRollbackOperationRecord | None
                ]
                if isinstance(held, OdooProdRollbackOperationRecord):
                    held = held.model_copy(
                        update={
                            "target": _rollback_operation().target.model_copy(
                                update={"artifact_id": pair.production.artifact_id}
                            )
                        }
                    )
                    store.create_odoo_prod_rollback_operation_record_if_no_active_lane(held)
                    claim = store.claim_odoo_cutover_rollback_reconciliation_record
                else:
                    store.create_odoo_prod_promotion_operation_record_if_no_active_lane(held)
                    claim = store.claim_odoo_cutover_reconciliation_record
                deployment = DeploymentRecord(
                    record_id="deployment-cutover-" + identity,
                    context=operation.context,
                    instance=operation.instance,
                    source_git_ref=pair.candidate.source_commit,
                    deploy=DeploymentEvidence(
                        deploy_mode="atomic-two-slot", target_type="compose", target_name="isolated"
                    ),
                    odoo_cutover=progress,
                )
                missing_receipt = progress.model_copy(update={"maintenance": None})
                self.assertIsNone(startup_recovery_pause(missing_receipt))
                self.assertIsNone(publication_recovery_pause(missing_receipt))
                store.write_deployment_record(
                    deployment.model_copy(update={"odoo_cutover": missing_receipt})
                )
                observed = claim(
                    lease_owner="receipt-reader",
                    lease_expires_at="2026-10-27T00:05:00Z",
                    claimed_at="2026-10-27T00:00:00Z",
                )
                if rollback:
                    self.assertIsNone(observed)
                else:
                    assert observed is not None
                    assert isinstance(held, OdooProdPromotionOperationRecord)
                    self.assertEqual(observed.attempt, held.attempt)
                    self.assertEqual(observed.phase, held.phase)
                    self.assertEqual(observed.checkpoints, held.checkpoints)
                    store.write_odoo_prod_promotion_operation_record(held)
                for invalid in (
                    progress.model_copy(update={"backup": None}),
                    progress.model_copy(
                        update={
                            "rollback_pause" if rollback else "pause": pause.model_copy(
                                update={"ended_at": now}
                            )
                        }
                    ),
                    progress.model_copy(
                        update={
                            "rollback_pause" if rollback else "pause": pause.model_copy(
                                update={"writers": ()}
                            )
                        }
                    ),
                ):
                    self.assertIsNone(startup_recovery_pause(invalid))
                    self.assertIsNone(publication_recovery_pause(invalid))
                    store.write_deployment_record(
                        deployment.model_copy(update={"odoo_cutover": invalid})
                    )
                    self.assertIsNone(
                        claim(
                            lease_owner="worker",
                            lease_expires_at="2026-10-27T00:05:00Z",
                            claimed_at="2026-10-27T00:00:00Z",
                        )
                    )
                if rollback:
                    plain_plan = OdooCutoverPlan.model_validate(
                        {**pair.model_dump(), "website_settings": None}
                    )
                    plain = progress.model_copy(
                        update={
                            "plan": plain_plan,
                            "maintenance": provider.maintenance(plain_plan),
                            "website_settings_attempted": False,
                            "rollback_pause": None,
                            "public_site": None,
                        }
                    )
                    self.assertIsNone(startup_recovery_pause(plain))
                    store.write_deployment_record(
                        deployment.model_copy(update={"odoo_cutover": plain})
                    )
                    plain_claim = claim(
                        lease_owner="plain-inverse",
                        lease_expires_at="2026-10-27T00:05:00Z",
                        claimed_at="2026-10-27T00:00:00Z",
                    )
                    assert plain_claim is not None
                    self.assertEqual(plain_claim.operation_id, held.operation_id)
                    self.assertEqual(plain_claim.attempt, held.attempt)
                    self.assertEqual(plain_claim.checkpoints, held.checkpoints)
                    assert isinstance(held, OdooProdRollbackOperationRecord)
                    store.write_odoo_prod_rollback_operation_record(held)
                if not rollback:
                    from control_plane.workflows.odoo_stable_operation_worker import (
                        DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
                    )

                    assert isinstance(held, OdooProdPromotionOperationRecord)
                    exhausted = held.model_copy(
                        update={"attempt": DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS}
                    )
                    store.write_odoo_prod_promotion_operation_record(exhausted)
                    store.write_deployment_record(deployment)
                    self.assertIsNone(
                        claim(
                            lease_owner="exhausted-startup",
                            lease_expires_at="2026-10-27T00:05:00Z",
                            claimed_at="2026-10-27T00:00:00Z",
                        )
                    )
                    store.write_deployment_record(
                        deployment.model_copy(update={"odoo_cutover": missing_receipt})
                    )
                    observation = claim(
                        lease_owner="original-outcome-reader",
                        lease_expires_at="2026-10-27T00:05:00Z",
                        claimed_at="2026-10-27T00:00:00Z",
                    )
                    assert observation is not None
                    self.assertEqual(observation.attempt, exhausted.attempt)
                    store.write_odoo_prod_promotion_operation_record(held)
                    store.write_deployment_record(
                        deployment.model_copy(
                            update={
                                "odoo_cutover": missing_receipt.model_copy(
                                    update={"error": "ValueError"}
                                )
                            }
                        )
                    )
                    self.assertIsNone(
                        claim(
                            lease_owner="definite-maintenance-failure",
                            lease_expires_at="2026-10-27T00:05:00Z",
                            claimed_at="2026-10-27T00:00:00Z",
                        )
                    )
                store.write_deployment_record(deployment)
                claimed = claim(
                    lease_owner="worker",
                    lease_expires_at="2026-10-27T00:05:00Z",
                    claimed_at="2026-10-27T00:00:00Z",
                )
                assert claimed is not None
                self.assertEqual(claimed.operation_id, operation.operation_id)
                self.assertEqual(claimed.idempotency_key, operation.idempotency_key)
                self.assertEqual(claimed.attempt, held.attempt + (0 if rollback else 1))
                self.assertEqual(claimed.phase, held.phase)
                self.assertEqual(claimed.checkpoints, held.checkpoints)
                self.assertIsNone(
                    claim(
                        lease_owner="other",
                        lease_expires_at="2026-10-27T00:05:00Z",
                        claimed_at="2026-10-27T00:00:00Z",
                    )
                )
