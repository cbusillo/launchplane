import unittest
from pydantic import SecretStr
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Any

from control_plane.contracts.odoo_business_hours import OdooBusinessHoursPolicy
from control_plane.odoo_diagnostic_identity import OdooDiagnosticIdentity
from control_plane.odoo_business_hours import (
    calendar_window,
    read_business_hours_window,
    read_company_calendar,
)
from tests.test_release_review import profile


def calendar() -> dict[str, object]:
    return {
        "tz": "America/New_York",
        "two_weeks_calendar": False,
        "flexible_hours": False,
        "duration_based": False,
    }


def attendance(day: int, start: float = 9, end: float = 17) -> dict[str, object]:
    return {
        "dayofweek": str(day),
        "hour_from": start,
        "hour_to": end,
        "week_type": False,
        "display_type": False,
        "resource_id": False,
    }


class BusinessHoursTests(unittest.TestCase):
    def test_company_calendar_is_read_with_company_scope_and_truncation_refuses(self) -> None:
        connection = MagicMock()
        connection.selection.base_url = "https://odoo.example.invalid"
        connection.credentials = {
            "ODOO_DB_NAME": "example",
            "ODOO_ADMIN_LOGIN": "admin",
            "ODOO_ADMIN_PASSWORD": "inert-test-password",
        }
        connection.selection.identity.product = "example"
        connection.selection.identity.context = "example"
        connection.selection.identity.instance = "testing"
        connection.selection.identity.environment_kind = "stable"
        connection.diagnostic_identity = OdooDiagnosticIdentity(
            product="example",
            context="example",
            instance="testing",
            database="example",
            login="diagnostic-reader",
            password=SecretStr("diagnostic-password"),
            user_id=7,
        )
        now = datetime(2026, 10, 13, 15, tzinfo=UTC)
        rows = [attendance(now.weekday())]
        rows[0].pop("resource_id")
        seen: list[dict[str, Any]] = []

        def rpc(
            _opener: object, _origin: str, path: str, params: dict[str, Any], **_kwargs: object
        ) -> object:
            if path == "/web/session/authenticate":
                self.assertEqual(params["login"], "diagnostic-reader")
                self.assertEqual(params["password"], "diagnostic-password")
                return {"uid": 7}
            if path == "/web/session/destroy":
                return None
            if params["method"] == "fields_get":
                return {"resource_id": {"type": "many2one"}} if "resource_id" in rows[0] else {}
            seen.append(params)
            if params["model"] == "resource.calendar.attendance":
                if any(field not in rows[0] for field in params["kwargs"]["fields"]):
                    raise ValueError("Odoo rejected an unsupported attendance field.")
            responses: dict[str, list[dict[str, object]]] = {
                "res.company": [{"resource_calendar_id": [17, "Company calendar"]}],
                "resource.calendar": [calendar()],
                "resource.calendar.attendance": rows,
                "resource.calendar.leaves": [],
            }
            result = responses[params["model"]]
            return [{field: row[field] for field in params["kwargs"]["fields"]} for row in result]

        with (
            patch("control_plane.odoo_business_hours._rpc", side_effect=rpc),
            patch("control_plane.odoo_business_hours._http_json"),
            patch("control_plane.odoo_business_hours.require_runtime_identity"),
            patch("control_plane.odoo_business_hours.odoo_compose_has_domain", return_value=True),
        ):
            config, actual, leaves = read_company_calendar(
                connection, company_id=7, observed_at=now
            )
            self.assertEqual(actual, rows)
            self.assertTrue(calendar_window(config, actual, leaves, observed_at=now).blocked_reason)
            for call in seen:
                self.assertEqual(call["method"], "search_read")
                self.assertEqual(call["kwargs"]["context"]["allowed_company_ids"], [7])
            closing = calendar_window(config, actual, leaves, observed_at=now).next_release_at
            rows[0]["resource_id"] = False
            personal = attendance(now.weekday(), 0, 24) | {"resource_id": [99, "Personal schedule"]}
            rows.append(personal)
            config, actual, leaves = read_company_calendar(
                connection, company_id=7, observed_at=now
            )
            self.assertEqual(
                calendar_window(config, actual, leaves, observed_at=now).next_release_at, closing
            )
            rows.extend(attendance(1) for _ in range(200))
            with self.assertRaises(ValueError):
                read_company_calendar(connection, company_id=7, observed_at=now)

    def test_unbound_origin_never_receives_odoo_credentials(self) -> None:
        connection = MagicMock()
        connection.selection.base_url = "https://wrong.example.invalid"
        with (
            patch("control_plane.odoo_business_hours.odoo_compose_has_domain", return_value=False),
            patch("control_plane.odoo_business_hours._rpc") as authenticate,
        ):
            with self.assertRaises(ValueError):
                read_company_calendar(connection, company_id=7, observed_at=datetime.now(UTC))
            authenticate.assert_not_called()

    def test_closing_in_calendar_timezone_and_meal_break_is_still_business_hours(self) -> None:
        now = datetime(2026, 10, 13, 16, 30, tzinfo=UTC)
        rows = [attendance(now.weekday(), end=12), attendance(now.weekday(), start=13)]
        held = calendar_window(calendar(), rows, [], observed_at=now)
        self.assertTrue(held.blocked_reason)
        closing = now.replace(hour=21, minute=0)
        self.assertEqual(datetime.fromisoformat(held.next_release_at), closing)
        self.assertFalse(calendar_window(calendar(), rows, [], observed_at=closing).blocked_reason)
        self.assertFalse(
            calendar_window(
                calendar(), rows, [], observed_at=now + timedelta(days=1)
            ).blocked_reason
        )

    def test_company_holiday_is_outside_hours_and_partial_leave_opens_a_window(self) -> None:
        now = datetime(2026, 10, 13, 15, tzinfo=UTC)
        rows = [attendance(now.weekday())]
        leave: dict[str, object] = {
            "date_from": "2026-10-13 00:00:00",
            "date_to": "2026-10-14 00:00:00",
        }
        self.assertFalse(calendar_window(calendar(), rows, [leave], observed_at=now).blocked_reason)
        leave = {"date_from": "2026-10-13 17:00:00", "date_to": "2026-10-13 19:00:00"}
        held = calendar_window(calendar(), rows, [leave], observed_at=now)
        self.assertEqual(datetime.fromisoformat(held.next_release_at), now.replace(hour=17))

    def test_alternating_weeks_and_dst_fallback(self) -> None:
        now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        row = attendance(now.weekday(), 1, 2)
        row["week_type"] = str(((now.date().toordinal() - 1) // 7) % 2)
        config = calendar() | {"two_weeks_calendar": True}
        held = calendar_window(config, [row], [], observed_at=now)
        closing = now.replace(hour=7, minute=0)
        self.assertEqual(datetime.fromisoformat(held.next_release_at), closing)
        self.assertTrue(
            calendar_window(config, [row], [], observed_at=now + timedelta(hours=1)).blocked_reason
        )
        self.assertFalse(
            calendar_window(config, [row], [], observed_at=now + timedelta(days=7)).blocked_reason
        )

    def test_missing_flexible_truncated_or_invalid_evidence_never_supplies_a_window(self) -> None:
        now = datetime(2026, 10, 13, 15, tzinfo=UTC)
        for config, rows in (
            (calendar(), []),
            (calendar() | {"flexible_hours": True}, [attendance(1)]),
            (calendar(), [attendance(1, 17, 9)]),
            (calendar() | {"tz": "unknown/timezone"}, [attendance(1)]),
        ):
            with self.subTest(config=config, rows=rows), self.assertRaises(ValueError):
                calendar_window(config, rows, [], observed_at=now)

    def test_prelaunch_and_unselected_products_never_read_odoo(self) -> None:
        configured = profile().model_copy(
            update={
                "production_use": "prelaunch",
                "odoo_business_hours": OdooBusinessHoursPolicy(company_id=7),
            }
        )
        with patch("control_plane.odoo_business_hours.select_stable_runtime") as read:
            self.assertFalse(
                read_business_hours_window(
                    store=object(), profile=configured, control_plane_root=Path(".")
                ).applies
            )
            self.assertFalse(
                read_business_hours_window(
                    store=object(), profile=profile(), control_plane_root=Path(".")
                ).applies
            )
            read.assert_not_called()

    def test_unavailable_calendar_has_a_bounded_public_reason_and_no_schedule(self) -> None:
        configured = profile().model_copy(
            update={
                "production_use": "live",
                "odoo_business_hours": OdooBusinessHoursPolicy(company_id=7),
            }
        )
        held = read_business_hours_window(
            store=object(), profile=configured, control_plane_root=None
        )
        self.assertTrue(held.applies)
        self.assertTrue(held.blocked_reason)
        self.assertFalse(held.next_release_at)
