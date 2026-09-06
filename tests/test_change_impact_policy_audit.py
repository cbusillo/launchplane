from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import event

from control_plane.change_impact_policy_audit import derive_change_impact_policy_audit
from control_plane.change_impact_service import ChangeImpactPolicyConflictError
from control_plane.contracts.change_impact_audit import ChangeImpactPolicyAuditRecord
from control_plane.service_auth import LocalAdminIdentity, LocalOperatorIdentity
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client
from tests.test_change_impact import _policy
from control_plane.http_routes.change_impact import (
    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
    CHANGE_IMPACT_POLICY_READ_ROUTE,
)
from tests.test_change_impact_http import (
    _ChangeImpactStore,
    _EvidenceProvider,
    _actions_identity,
    _app,
    _human_identity,
    _repository_evidence,
)


def _audit(subject: str = "operator:first", revision: int = 1) -> ChangeImpactPolicyAuditRecord:
    policy = _policy(
        revision=revision,
        supersedes_record_id=_policy().record_id if revision > 1 else None,
    )
    return ChangeImpactPolicyAuditRecord(
        record_id=policy.record_id,
        policy_digest=policy.policy_digest,
        actor_kind="local_operator",
        actor_subject=subject,
        trace_id="trace-original",
        recorded_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


class ChangeImpactPolicyAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(self.directory.name) / 'policy.sqlite'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)

    def test_original_audit_survives_replay_and_supersession(self) -> None:
        first = _policy()
        original = _audit()
        self.store.compare_and_write_change_impact_policy_record_with_audit(
            first, audit=original, expected_current_record_id="", expected_current_policy_digest=""
        )
        replay = self.store.compare_and_write_change_impact_policy_record_with_audit(
            first,
            audit=_audit("operator:second"),
            expected_current_record_id="",
            expected_current_policy_digest="",
        )
        self.assertEqual(replay.audit, original)
        self.assertEqual(replay.status, "replayed")
        self.assertEqual(self.store.write_change_impact_policy_record(first), "replayed")
        self.assertEqual(self.store.read_change_impact_policy_audit(first.record_id), original)
        second = _policy(revision=2, supersedes_record_id=first.record_id)
        self.store.compare_and_write_change_impact_policy_record_with_audit(
            second,
            audit=_audit("operator:second", revision=2),
            expected_current_record_id=first.record_id,
            expected_current_policy_digest=first.policy_digest,
        )
        self.assertEqual(self.store.read_change_impact_policy_audit(first.record_id), original)
        self.assertEqual(
            self.store.read_change_impact_policy_record(first.record_id),
            first.model_copy(update={"status": "superseded"}),
        )
        self.assertEqual(self.store.read_change_impact_policy_record(second.record_id), second)

    def test_legacy_replay_never_backfills_original_writer(self) -> None:
        first = _policy()
        self.store.write_change_impact_policy_record(first)
        result = self.store.compare_and_write_change_impact_policy_record_with_audit(
            first, audit=_audit(), expected_current_record_id="", expected_current_policy_digest=""
        )
        self.assertEqual(result.status, "replayed")
        self.assertIsNone(result.audit)
        self.assertIsNone(self.store.read_change_impact_policy_audit(first.record_id))

    def test_audit_insert_failure_rolls_back_predecessor_update(self) -> None:
        first = _policy()
        self.store.compare_and_write_change_impact_policy_record_with_audit(
            first, audit=_audit(), expected_current_record_id="", expected_current_policy_digest=""
        )
        second = _policy(revision=2, supersedes_record_id=first.record_id)

        def reject_insert(_connection: object, _cursor: object, statement: str, *_: object) -> None:
            if statement.startswith("INSERT INTO launchplane_change_impact_policies"):
                raise RuntimeError("injected policy audit insert failure")

        event.listen(self.store._engine, "before_cursor_execute", reject_insert)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected policy audit"):
                self.store.compare_and_write_change_impact_policy_record_with_audit(
                    second,
                    audit=_audit("operator:second", revision=2),
                    expected_current_record_id=first.record_id,
                    expected_current_policy_digest=first.policy_digest,
                )
        finally:
            event.remove(self.store._engine, "before_cursor_execute", reject_insert)
        self.assertEqual(self.store.list_change_impact_policy_records(), (first,))
        self.assertEqual(self.store.read_change_impact_policy_audit(first.record_id), _audit())

    def test_audit_identity_must_match_the_persisted_policy(self) -> None:
        with self.assertRaises(ChangeImpactPolicyConflictError):
            self.store.compare_and_write_change_impact_policy_record_with_audit(
                _policy(),
                audit=_audit(revision=2),
                expected_current_record_id="",
                expected_current_policy_digest="",
            )
        self.assertEqual(self.store.list_change_impact_policy_records(), ())


class ChangeImpactPolicyAuditHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_actor_dry_run_replay_and_readback(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{directory}/policy.sqlite"
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            actor = LocalOperatorIdentity(subject="operator:trusted", token_label="do-not-persist")
            app = _app(
                store=store, provider=_EvidenceProvider(_repository_evidence()), identity=actor
            )
            policy = _policy()
            body = {"record": policy.model_dump(mode="json"), "mode": "dry_run"}
            async with lifespan_client(app) as client:
                dry_run = await client.post(CHANGE_IMPACT_POLICY_APPLY_ROUTE, json=body)
                self.assertEqual(dry_run.status_code, 202, dry_run.text)
                self.assertEqual(dry_run.json()["result"]["attribution_status"], "not_applied")
                self.assertEqual(store.list_change_impact_policy_records(), ())
                forged = await client.post(
                    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
                    json={**body, "audit": _audit().model_dump(mode="json")},
                )
                self.assertEqual(forged.status_code, 422)
                body["mode"] = "apply"
                applied = await client.post(CHANGE_IMPACT_POLICY_APPLY_ROUTE, json=body)
                self.assertEqual(applied.status_code, 202, applied.text)
                result = applied.json()["result"]
                self.assertEqual(result["audit"]["actor_subject"], actor.subject)
                self.assertEqual(result["record"], policy.model_dump(mode="json"))
                self.assertNotIn(actor.token_label, applied.text)
                replay = await client.post(CHANGE_IMPACT_POLICY_APPLY_ROUTE, json=body)
                self.assertEqual(replay.json()["result"]["audit"], result["audit"])
                read = await client.get(
                    CHANGE_IMPACT_POLICY_READ_ROUTE, params={"repository_id": "1001"}
                )
                self.assertEqual(read.json()["read_model"]["audit"], result["audit"])
                self.assertEqual(read.json()["read_model"]["attribution_status"], "attributed")

    async def test_incapable_store_and_unsupported_identity_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _ChangeImpactStore(Path(directory))
            for identity, status in (
                (LocalAdminIdentity(subject="admin:test", token_label="test"), 503),
                (_human_identity(), 403),
            ):
                with self.subTest(identity=type(identity).__name__):
                    app = _app(
                        store=store,
                        provider=_EvidenceProvider(_repository_evidence()),
                        identity=identity,
                    )
                    async with lifespan_client(app) as client:
                        response = await client.post(
                            CHANGE_IMPACT_POLICY_APPLY_ROUTE,
                            json={"record": _policy().model_dump(mode="json")},
                        )
                    self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(store.list_change_impact_policy_records(), ())

    def test_identity_projection_uses_only_named_verified_fields(self) -> None:
        for identity, kind in (
            (LocalAdminIdentity(subject="admin:test", token_label="secret-label"), "local_admin"),
            (
                _actions_identity(repository="example/policy-admin", repository_id="9001"),
                "github_actions",
            ),
        ):
            with self.subTest(kind=kind):
                audit = derive_change_impact_policy_audit(
                    identity=identity, record=_policy(), trace_id="trace"
                )
                self.assertEqual(audit.actor_kind, kind)
                self.assertEqual(audit.actor_subject, identity.subject)
                self.assertNotIn("raw_claims", audit.model_dump())
                self.assertNotIn("token_label", audit.model_dump())
