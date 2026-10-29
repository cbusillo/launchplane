from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import os
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.product_incident_read_model import (
    build_product_environment_incident_detail,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
)
from control_plane.storage.postgres import (
    LaunchplanePublicIngressObservationRow,
    PostgresRecordStore,
)
from tests import test_public_ingress_direct_outbox as direct_outbox
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _profile


class HistoricalRetirementCompatibilityTests(unittest.TestCase):
    def test_legacy_empty_retirement_is_readable_without_inventing_target_evidence(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "historical-retirement-test-key"},
                clear=True,
            )
        )
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "records.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            result = direct_outbox.DirectNotificationOutboxTests._probe(
                store, 503, "2026-05-29T12:00:00Z"
            )
            incident = result.incidents[0]
            payload = result.records[0].model_dump(mode="json")
            payload.update(
                record_id="legacy-retirement",
                check_name=RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
                check_kind="provider",
                status="skipped",
                purpose="reconciliation",
                failure_code="monitoring_intent_changed",
                targets=[],
                observed_at="2026-05-29T12:01:00Z",
                incident_id=incident.incident_id,
            )
            with store._session_factory() as session:
                session.add(
                    LaunchplanePublicIngressObservationRow(
                        record_id=payload["record_id"],
                        product=incident.product,
                        context=incident.context,
                        instance=incident.instance,
                        status=payload["status"],
                        observed_at=payload["observed_at"],
                        incident_id=incident.incident_id,
                        check_token=RETIRED_PRODUCTION_DELIVERY_LAG_CHECK,
                        check_kind="provider",
                        payload=payload,
                    )
                )
                session.commit()
            detail = build_product_environment_incident_detail(
                record_store=store,
                product=incident.product,
                environment="prod",
                incident_id=incident.incident_id,
            )
            historical = next(
                row for row in detail.observations if row.record_id == payload["record_id"]
            )
            self.assertEqual(historical.status, "skipped")
            loaded = store.read_public_ingress_observation_record(str(payload["record_id"]))
            self.assertEqual(loaded.targets, ())
            for purpose, kind, status, failure in (
                ("probe", "provider", "pass", None),
                ("probe", "provider", "fail", "http_error"),
                ("probe", "provider", "skipped", "monitoring_intent_changed"),
                ("reconciliation", "provider", "pass", None),
                ("reconciliation", "provider", "fail", "http_error"),
                ("reconciliation", "public_http", "skipped", "monitoring_intent_changed"),
            ):
                with (
                    self.subTest(purpose=purpose, kind=kind, status=status),
                    self.assertRaises(ValidationError),
                ):
                    PublicIngressObservationRecord.model_validate(
                        {
                            **payload,
                            "purpose": purpose,
                            "check_kind": kind,
                            "status": status,
                            "failure_code": failure,
                        }
                    )
