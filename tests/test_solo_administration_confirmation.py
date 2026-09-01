from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from pydantic import ValidationError

from control_plane.contracts.solo_administration_confirmation import (
    SOLO_ADMINISTRATION_CONFIRMATION_TTL_SECONDS,
    SoloAdministrationConfirmationConflictError,
    SoloAdministrationConfirmationRecord,
    build_solo_administration_confirmation_id,
    consume_solo_administration_confirmation,
    expire_solo_administration_confirmation,
    issue_solo_administration_confirmation,
    revoke_solo_administration_confirmation,
    solo_administration_acknowledgement_sha256,
    solo_administration_confirmation_human_session_id_sha256,
    verify_solo_administration_acknowledgement,
)
from control_plane.storage.postgres import PostgresRecordStore


_ACTIVE_POLICY_SHA256 = "a" * 64
_CANDIDATE_POLICY_SHA256 = "b" * 64
_PLAN_SHA256 = "c" * 64
_SCOPE_SHA256 = "d" * 64
_KEY_SHA256 = "e" * 64
_ACKNOWLEDGEMENT_SHA256 = solo_administration_acknowledgement_sha256(
    "I acknowledge solo administration"
)
_SESSION_SHA256 = solo_administration_confirmation_human_session_id_sha256("human-session-1")
_SECRET_SHA256 = "f" * 64


def _record(
    *, created_at: str = "2026-08-31T12:00:00Z", **updates: object
) -> SoloAdministrationConfirmationRecord:
    values: dict[str, object] = {
        "active_policy_record_id": "launchplane-authz-policy-r00000000000000000001-aaaaaaaaaaaa",
        "active_policy_revision": 1,
        "active_policy_sha256": _ACTIVE_POLICY_SHA256,
        "candidate_policy_sha256": _CANDIDATE_POLICY_SHA256,
        "reviewed_plan_sha256": _PLAN_SHA256,
        "human_session_id_sha256": _SESSION_SHA256,
        "github_id": 123,
        "idempotency_scope_sha256": _SCOPE_SHA256,
        "idempotency_key_sha256": _KEY_SHA256,
        "acknowledgement_sha256": _ACKNOWLEDGEMENT_SHA256,
        "secret_sha256": _SECRET_SHA256,
        "created_at": created_at,
        "expires_at": "2026-08-31T12:05:00Z",
    }
    values["confirmation_id"] = build_solo_administration_confirmation_id(
        reviewed_plan_sha256=_PLAN_SHA256,
        human_session_id_sha256=_SESSION_SHA256,
        idempotency_scope_sha256=_SCOPE_SHA256,
        idempotency_key_sha256=_KEY_SHA256,
    )
    values.update(updates)
    return SoloAdministrationConfirmationRecord.model_validate(values)


def _issue_record(**updates: object) -> SoloAdministrationConfirmationRecord:
    values = _record().model_dump()
    values.update(updates)
    return issue_solo_administration_confirmation(
        active_policy_record_id=str(values["active_policy_record_id"]),
        active_policy_revision=int(values["active_policy_revision"]),
        active_policy_sha256=str(values["active_policy_sha256"]),
        candidate_policy_sha256=str(values["candidate_policy_sha256"]),
        reviewed_plan_sha256=str(values["reviewed_plan_sha256"]),
        human_session_id_sha256=str(values["human_session_id_sha256"]),
        github_id=int(values["github_id"]),
        idempotency_scope_sha256=str(values["idempotency_scope_sha256"]),
        idempotency_key_sha256=str(values["idempotency_key_sha256"]),
        acknowledgement_sha256=str(values["acknowledgement_sha256"]),
        secret_sha256=str(values["secret_sha256"]),
        created_at=str(values["created_at"]),
    )


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


class SoloAdministrationConfirmationContractTests(unittest.TestCase):
    def test_issue_is_immutable_and_has_exact_five_minute_ttl(self) -> None:
        record = _issue_record()

        self.assertEqual(
            record.expires_at,
            "2026-08-31T12:05:00+00:00",
        )
        self.assertEqual(SOLO_ADMINISTRATION_CONFIRMATION_TTL_SECONDS, 300)
        with self.assertRaises(ValidationError):
            record.state = "consumed"

    def test_acknowledgement_digest_verifies_constant_time_boundary(self) -> None:
        self.assertTrue(
            verify_solo_administration_acknowledgement(
                acknowledgement="I acknowledge solo administration",
                acknowledgement_sha256=_ACKNOWLEDGEMENT_SHA256,
            )
        )
        self.assertFalse(
            verify_solo_administration_acknowledgement(
                acknowledgement="different acknowledgement",
                acknowledgement_sha256=_ACKNOWLEDGEMENT_SHA256,
            )
        )

    def test_lifecycle_helpers_require_exact_binding_and_preserve_terminal_evidence(self) -> None:
        record = _issue_record()
        consumed = consume_solo_administration_confirmation(
            record,
            active_policy_record_id=record.active_policy_record_id,
            active_policy_revision=record.active_policy_revision,
            active_policy_sha256=record.active_policy_sha256,
            candidate_policy_sha256=record.candidate_policy_sha256,
            candidate_administrator_quorum=1,
            candidate_distinct_human_administrator_count=1,
            reviewed_plan_sha256=record.reviewed_plan_sha256,
            human_session_id_sha256=record.human_session_id_sha256,
            github_id=record.github_id,
            idempotency_scope_sha256=record.idempotency_scope_sha256,
            idempotency_key_sha256=record.idempotency_key_sha256,
            acknowledgement_sha256=record.acknowledgement_sha256,
            secret_sha256=record.secret_sha256,
            terminal_at="2026-08-31T12:01:00Z",
        )

        self.assertEqual(consumed.state, "consumed")
        self.assertEqual(consumed.terminal_at, "2026-08-31T12:01:00+00:00")
        with self.assertRaises(SoloAdministrationConfirmationConflictError):
            consume_solo_administration_confirmation(
                consumed,
                active_policy_record_id=record.active_policy_record_id,
                active_policy_revision=record.active_policy_revision,
                active_policy_sha256=record.active_policy_sha256,
                candidate_policy_sha256=record.candidate_policy_sha256,
                candidate_administrator_quorum=2,
                candidate_distinct_human_administrator_count=1,
                reviewed_plan_sha256=record.reviewed_plan_sha256,
                human_session_id_sha256=record.human_session_id_sha256,
                secret_sha256=record.secret_sha256,
                github_id=record.github_id,
                idempotency_scope_sha256=record.idempotency_scope_sha256,
                idempotency_key_sha256=record.idempotency_key_sha256,
                acknowledgement_sha256=record.acknowledgement_sha256,
                terminal_at="2026-08-31T12:01:00Z",
            )

        self.assertEqual(
            revoke_solo_administration_confirmation(
                _issue_record(), terminal_at="2026-08-31T12:01:00Z"
            ).state,
            "revoked",
        )
        self.assertEqual(
            expire_solo_administration_confirmation(
                _issue_record(), terminal_at="2026-08-31T12:05:00Z"
            ).state,
            "expired",
        )


class SoloAdministrationConfirmationStoreTests(unittest.TestCase):
    def test_store_is_idempotent_and_consumption_keeps_the_row(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'launchplane.sqlite3'}"
            command.upgrade(_alembic_config(database_url), "head")
            store = PostgresRecordStore(database_url=database_url)
            try:
                record = _issue_record()
                created, created_new = store.issue_solo_administration_confirmation(record)
                replayed, replayed_new = store.issue_solo_administration_confirmation(record)
                self.assertEqual(created, record)
                self.assertTrue(created_new)
                self.assertEqual(replayed, record)
                self.assertFalse(replayed_new)
                self.assertEqual(
                    tuple(
                        event.event_type
                        for event in store.list_solo_administration_confirmation_lifecycle_events(
                            confirmation_id=record.confirmation_id
                        )
                    ),
                    ("issued",),
                )

                consumed = store.consume_solo_administration_confirmation(
                    confirmation_id=record.confirmation_id,
                    active_policy_record_id=record.active_policy_record_id,
                    active_policy_revision=record.active_policy_revision,
                    active_policy_sha256=record.active_policy_sha256,
                    candidate_policy_sha256=record.candidate_policy_sha256,
                    candidate_administrator_quorum=1,
                    candidate_distinct_human_administrator_count=1,
                    reviewed_plan_sha256=record.reviewed_plan_sha256,
                    human_session_id_sha256=record.human_session_id_sha256,
                    secret_sha256=record.secret_sha256,
                    github_id=record.github_id,
                    idempotency_scope_sha256=record.idempotency_scope_sha256,
                    idempotency_key_sha256=record.idempotency_key_sha256,
                    acknowledgement_sha256=record.acknowledgement_sha256,
                    terminal_at="2026-08-31T12:01:00Z",
                )
                self.assertEqual(consumed.state, "consumed")
                self.assertEqual(
                    store.read_solo_administration_confirmation(record.confirmation_id),
                    consumed,
                )
            finally:
                store.close()

    def test_migration_creates_partial_issued_binding_index(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'launchplane.sqlite3'}"
            command.upgrade(_alembic_config(database_url), "head")
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                confirmation_columns = {
                    column["name"]
                    for column in inspector.get_columns(
                        "launchplane_solo_administration_confirmations"
                    )
                }
                self.assertIn("human_session_id_sha256", confirmation_columns)
                self.assertNotIn("human_session_id", confirmation_columns)
                self.assertIn(
                    "launchplane_solo_administration_confirmation_events",
                    inspector.get_table_names(),
                )
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(
                        "launchplane_solo_administration_confirmations"
                    )
                }
                self.assertIn(
                    "launchplane_solo_administration_confirmation_issued_binding_uq",
                    indexes,
                )
                self.assertTrue(
                    indexes["launchplane_solo_administration_confirmation_issued_binding_uq"][
                        "unique"
                    ]
                )
                self.assertIn(
                    "launchplane_solo_administration_confirmation_state_expiry_idx",
                    indexes,
                )
            finally:
                engine.dispose()

    def test_store_revoke_and_expire_are_terminal_and_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'launchplane.sqlite3'}"
            command.upgrade(_alembic_config(database_url), "head")
            store = PostgresRecordStore(database_url=database_url)
            try:
                revoked_record = _issue_record(idempotency_key_sha256="f" * 64)
                store.issue_solo_administration_confirmation(revoked_record)
                revoked = store.revoke_solo_administration_confirmation(
                    confirmation_id=revoked_record.confirmation_id,
                    terminal_at="2026-08-31T12:01:00Z",
                )
                self.assertEqual(revoked.state, "revoked")

                expired_record = _issue_record(idempotency_key_sha256="9" * 64)
                store.issue_solo_administration_confirmation(expired_record)
                expired = store.expire_solo_administration_confirmation(
                    confirmation_id=expired_record.confirmation_id,
                    terminal_at="2026-08-31T12:05:00Z",
                )
                self.assertEqual(expired.state, "expired")
                self.assertEqual(
                    store.read_solo_administration_confirmation(expired_record.confirmation_id),
                    expired,
                )
                self.assertEqual(
                    tuple(
                        event.event_type
                        for event in store.list_solo_administration_confirmation_lifecycle_events(
                            confirmation_id=revoked_record.confirmation_id
                        )
                    ),
                    ("issued", "revoked"),
                )
            finally:
                store.close()
