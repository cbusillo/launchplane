from __future__ import annotations

import unittest

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.product_incident_read_model import (
    ProductIncidentEnvironmentScope,
    ProductIncidentReadModelCapabilityError,
    build_product_environment_incident_detail,
    build_product_environment_incident_list,
    require_product_incident_read_store,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentMaterialFingerprint,
    PublicIngressIncidentRecord,
    PublicIngressIncidentReminderStateRecord,
    PublicIngressNotificationAttemptRecord,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    public_ingress_material_fingerprint_sha256,
)


NOW = "2026-07-28T14:00:00Z"


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "product": "example-product",
            "display_name": "Example Product",
            "repository": "example/example-product",
            "driver_id": "generic-web",
            "image": {"repository": ""},
            "lanes": (
                {
                    "instance": "prod",
                    "context": "example-product-prod",
                },
            ),
            "updated_at": "2026-07-28T13:00:00Z",
            "source": "test",
        }
    )


def _fingerprint(*, severity: str = "critical") -> PublicIngressIncidentMaterialFingerprint:
    return PublicIngressIncidentMaterialFingerprint.model_validate(
        {
            "check_kind": "public_http",
            "failure_code": "connection_timeout",
            "failure_layer": "network",
            "severity": severity,
            "affected_targets": ("health_url",),
            "route_authority": {
                "kind": "public_urls",
                "target_keys": ("health_url",),
            },
        }
    )


def _incident(
    *,
    incident_id: str = "incident-open",
    status: str = "open",
    severity: str = "critical",
    opened_at: str = "2026-07-28T12:00:00Z",
) -> PublicIngressIncidentRecord:
    fingerprint = _fingerprint(severity=severity)
    digest = public_ingress_material_fingerprint_sha256(fingerprint)
    resolved = status == "resolved"
    return PublicIngressIncidentRecord.model_validate(
        {
            "schema_version": 2,
            "incident_id": incident_id,
            "product": "example-product",
            "context": "example-product-prod",
            "instance": "prod",
            "check_name": "public-ingress",
            "check_kind": "public_http",
            "status": status,
            "opened_at": opened_at,
            "opened_observation_id": f"observation-{incident_id}-opened",
            "latest_observation_id": f"observation-{incident_id}-latest",
            "latest_observed_at": "2026-07-28T13:45:00Z",
            "failure_code": "connection_timeout",
            "resolved_at": "2026-07-28T13:50:00Z" if resolved else "",
            "resolved_observation_id": f"observation-{incident_id}-resolved" if resolved else "",
            "resolution_reason": "recovered" if resolved else "",
            "state_version": 2,
            "severity": severity,
            "material_fingerprint": fingerprint,
            "material_fingerprint_sha256": digest,
            "material_fingerprint_complete": True,
            "latest_material_event_id": f"event-{incident_id}",
            "latest_material_event": "resolved" if resolved else "opened",
            "latest_material_event_at": "2026-07-28T13:50:00Z" if resolved else opened_at,
            "notification_state": "active",
            "notification_state_changed_at": opened_at,
            "recovery_observation_threshold": 2,
            "consecutive_recovery_observations": 2 if resolved else 0,
            "summary": "Provider failed at https://private.example.invalid/healthz.",
        }
    )


def _observation(incident: PublicIngressIncidentRecord) -> PublicIngressObservationRecord:
    return PublicIngressObservationRecord(
        schema_version=2,
        record_id=incident.latest_observation_id,
        product=incident.product,
        context=incident.context,
        instance=incident.instance,
        check_name=incident.check_name,
        check_kind=incident.check_kind,
        monitoring_intent="public",
        observed_at=incident.latest_observed_at,
        status="fail",
        failure_code=incident.failure_code,
        targets=(
            PublicIngressTargetObservation(
                target="health_url",
                url="https://example.invalid/healthz",
                status="fail",
                failure_code=incident.failure_code,
                summary="The request timed out.",
            ),
        ),
        incident_id=incident.incident_id,
        incident_event_id=incident.latest_material_event_id,
        material_fingerprint=incident.material_fingerprint,
        material_fingerprint_sha256=incident.material_fingerprint_sha256,
        summary=incident.summary,
    )


class _Store:
    def __init__(self) -> None:
        self.profile = _profile()
        self.open_incident = _incident()
        self.resolved_incident = _incident(
            incident_id="incident-resolved",
            status="resolved",
            severity="warning",
            opened_at="2026-07-28T10:00:00Z",
        )
        self.incidents: tuple[PublicIngressIncidentRecord, ...] = (
            self.open_incident,
            self.resolved_incident,
        )
        self.observations = (_observation(self.open_incident),)
        self.events = (
            PublicIngressIncidentEventRecord(
                event_id=self.open_incident.latest_material_event_id,
                incident_id=self.open_incident.incident_id,
                event="opened",
                reason="incident_opened",
                occurred_at=self.open_incident.opened_at,
                observation_id=self.open_incident.opened_observation_id,
                material_fingerprint_sha256=self.open_incident.material_fingerprint_sha256,
                severity="critical",
                summary="Provider error included private.example.invalid internals.",
            ),
        )
        self.reminders = (
            PublicIngressIncidentReminderStateRecord(
                reminder_state_id="reminder-state-1",
                incident_id=self.open_incident.incident_id,
                policy_id="policy-1",
                status="active",
                material_event_id=self.open_incident.latest_material_event_id,
                interval_seconds=3600,
                window_anchor_at=self.open_incident.opened_at,
                next_reminder_at="2026-07-28T15:00:00Z",
                updated_at=NOW,
            ),
        )
        self.attempts = (
            PublicIngressNotificationAttemptRecord(
                attempt_id="attempt-1",
                incident_id=self.open_incident.incident_id,
                event="opened",
                policy_id="policy-1",
                destination_id="operator-private-destination",
                destination_kind="github_issue",
                delivery_status="delivered",
                attempted_at="2026-07-28T12:01:00Z",
                observation_id=self.open_incident.opened_observation_id,
                incident_event_id=self.open_incident.latest_material_event_id,
                external_url="https://github.com/example/example/issues/1",
                external_id="private-provider-id",
                action="created",
                error_message="provider rejected secret-bearing request metadata",
            ),
        )
        self.outbox = (
            OutboxDeliveryRecord(
                delivery_id="delivery-1",
                kind="public_ingress_notification",
                state="delivered",
                aggregate_type="public_ingress_incident",
                aggregate_id=self.open_incident.incident_id,
                dedupe_key="public_ingress_notification:private-dedupe-key",
                created_at="2026-07-28T12:00:30Z",
                updated_at="2026-07-28T12:01:00Z",
                next_attempt_at="2026-07-28T12:00:30Z",
                attempt=1,
                provider_operation_key="private-provider-operation",
                provider_id="github",
                external_id="private-provider-id",
                external_url="https://github.com/example/example/issues/1",
                action="created",
                payload={"incident_id": self.open_incident.incident_id},
            ),
        )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_public_ingress_incident_record(self, incident_id: str) -> PublicIngressIncidentRecord:
        for incident in self.incidents:
            if incident.incident_id == incident_id:
                return incident
        raise FileNotFoundError(incident_id)

    def list_public_ingress_incident_records(
        self, **kwargs: object
    ) -> tuple[PublicIngressIncidentRecord, ...]:
        status = str(kwargs.get("status", ""))
        records = tuple(
            incident for incident in self.incidents if not status or incident.status == status
        )
        limit = kwargs.get("limit")
        if limit is None:
            return records
        if not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        return records[:limit]

    def list_public_ingress_observation_records(
        self, **kwargs: object
    ) -> tuple[PublicIngressObservationRecord, ...]:
        incident_id = str(kwargs.get("incident_id", ""))
        records = tuple(
            record
            for record in self.observations
            if not incident_id or record.incident_id == incident_id
        )
        return records

    def list_public_ingress_incident_event_records(
        self, **kwargs: object
    ) -> tuple[PublicIngressIncidentEventRecord, ...]:
        incident_id = str(kwargs.get("incident_id", ""))
        return tuple(
            record for record in self.events if not incident_id or record.incident_id == incident_id
        )

    def list_public_ingress_incident_reminder_state_records(
        self, **kwargs: object
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
        incident_id = str(kwargs.get("incident_id", ""))
        return tuple(
            record
            for record in self.reminders
            if not incident_id or record.incident_id == incident_id
        )

    def list_public_ingress_notification_attempt_records(
        self, **kwargs: object
    ) -> tuple[PublicIngressNotificationAttemptRecord, ...]:
        incident_id = str(kwargs.get("incident_id", ""))
        return tuple(
            record
            for record in self.attempts
            if not incident_id or record.incident_id == incident_id
        )

    def list_outbox_delivery_records(self, **kwargs: object) -> tuple[OutboxDeliveryRecord, ...]:
        aggregate_id = str(kwargs.get("aggregate_id", ""))
        return tuple(
            record
            for record in self.outbox
            if not aggregate_id or record.aggregate_id == aggregate_id
        )


class ProductIncidentReadModelTests(unittest.TestCase):
    def test_capability_check_fails_closed(self) -> None:
        class PartialStore:
            def read_product_profile_record(self, product: str) -> None:
                del product

        with self.assertRaisesRegex(
            ProductIncidentReadModelCapabilityError,
            "read_public_ingress_incident_record",
        ):
            require_product_incident_read_store(PartialStore())

    def test_environment_list_orders_open_before_resolved_and_exposes_reminders(self) -> None:
        result = build_product_environment_incident_list(
            record_store=_Store(),
            product="example-product",
            environment="prod",
        )

        self.assertEqual(
            [incident.incident_id for incident in result.incidents],
            ["incident-open", "incident-resolved"],
        )
        self.assertEqual(result.incidents[0].failure_layer, "network")
        self.assertEqual(result.incidents[0].next_reminder_at, "2026-07-28T15:00:00Z")
        self.assertEqual(result.trust_state, "recorded")

    def test_detail_links_evidence_and_redacts_delivery_internals(self) -> None:
        detail = build_product_environment_incident_detail(
            record_store=_Store(),
            product="example-product",
            environment="prod",
            incident_id="incident-open",
        )

        self.assertEqual(detail.observations[0].record_id, "observation-incident-open-latest")
        self.assertEqual(detail.events[0].event, "opened")
        self.assertEqual(detail.reminders[0].status, "active")
        self.assertEqual(detail.notification_attempts[0].delivery_status, "delivered")
        self.assertEqual(detail.outbox_deliveries[0].state, "delivered")
        payload = detail.model_dump(mode="json")
        serialized = str(payload)
        self.assertNotIn("operator-private-destination", serialized)
        self.assertNotIn("private-provider-operation", serialized)
        self.assertNotIn("private-dedupe-key", serialized)
        self.assertNotIn("private-provider-id", serialized)
        self.assertNotIn("private.example.invalid", serialized)
        self.assertNotIn("policy-1", serialized)
        self.assertNotIn("secret-bearing", serialized)
        self.assertNotIn("payload", payload["outbox_deliveries"][0])

    def test_open_incident_limit_prioritizes_older_critical_incidents(self) -> None:
        store = _Store()
        warnings = tuple(
            _incident(
                incident_id=f"warning-{index:02d}",
                severity="warning",
                opened_at=f"2026-07-28T13:{index:02d}:00Z",
            )
            for index in range(21)
        )
        critical = _incident(
            incident_id="critical-older",
            severity="critical",
            opened_at="2026-07-28T10:00:00Z",
        )
        store.incidents = (*warnings, critical)

        result = build_product_environment_incident_list(
            record_store=store,
            product="example-product",
            environment="prod",
            status="open",
            limit=20,
        )

        self.assertEqual(len(result.incidents), 20)
        self.assertEqual(result.incidents[0].incident_id, critical.incident_id)

    def test_authorized_scope_avoids_a_second_profile_read(self) -> None:
        class NoProfileReadStore(_Store):
            def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
                raise AssertionError(f"Unexpected profile reread for {product}.")

        store = NoProfileReadStore()
        scope = ProductIncidentEnvironmentScope(
            product="example-product",
            display_name="Example Product",
            environment="prod",
            context="example-product-prod",
            instance="prod",
            recorded_at=NOW,
        )

        result = build_product_environment_incident_list(
            record_store=store,
            product="example-product",
            environment="prod",
            scope=scope,
        )

        self.assertEqual(result.context, scope.context)

    def test_detail_returns_not_found_for_another_environment(self) -> None:
        store = _Store()
        store.open_incident = store.open_incident.model_copy(update={"context": "other-context"})
        store.incidents = (store.open_incident, store.resolved_incident)

        with self.assertRaisesRegex(FileNotFoundError, "incident-open"):
            build_product_environment_incident_detail(
                record_store=store,
                product="example-product",
                environment="prod",
                incident_id="incident-open",
            )


if __name__ == "__main__":
    unittest.main()
