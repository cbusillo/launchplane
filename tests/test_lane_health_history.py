from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from tempfile import TemporaryDirectory
import json
import unittest
from click.testing import CliRunner

from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressCheckKind,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    health_monitor_freshness_seconds,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _product_environment_read_policy,
    _seed_product_environment_read_records,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request
from tests.support.stores import _sqlite_database_url

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
REQUIRED: tuple[tuple[PublicIngressCheckKind, str], ...] = (("public_http", "health"),)


def stamp(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def observation(
    name: str, seconds: float, *, status: str = "pass", check: str = "health"
) -> PublicIngressObservationRecord:
    identity = RuntimeIdentity(
        product="example-site",
        context="example-site",
        instance="prod",
        deployment_record_id="deploy-1",
        artifact_id="artifact-1",
        source_git_ref="source-1",
        image_reference="private-registry.invalid/private-image",
    )
    return PublicIngressObservationRecord.model_validate(
        {
            "record_id": name,
            "product": "example-site",
            "context": "example-site",
            "instance": "prod",
            "check_name": check,
            "check_kind": "public_http",
            "observed_at": stamp(seconds),
            "status": status,
            "failure_code": "http_error" if status == "fail" else None,
            "expected_runtime_identity": identity,
            "summary": "private diagnostic https://secret.invalid/path",
            "targets": [
                PublicIngressTargetObservation.model_validate(
                    {
                        "target": "health_url",
                        "url": "https://secret.invalid/health",
                        "status": status,
                        "failure_code": "http_error" if status == "fail" else None,
                        "runtime_identity_status": "match",
                        "observed_runtime_identity": identity,
                        "summary": "private diagnostic",
                    }
                )
            ],
        }
    )


class LaneHealthHistoryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_pagination_includes_passes_and_equal_timestamp_records(self) -> None:
        with TemporaryDirectory() as temporary:
            url = _sqlite_database_url(Path(temporary) / "records.sqlite")
            _seed_product_environment_read_records(url)
            store = PostgresRecordStore(database_url=url)
            self.addCleanup(store.close)
            for record in (
                observation("a", 0),
                observation("b", 60),
                observation("c", 60, status="fail"),
                observation("d", 120),
            ):
                store.write_public_ingress_observation_record(record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            path = "/v1/products/example-site/environments/prod/health-history"
            params = {"since": stamp(0), "until": stamp(180), "limit": 2}
            ids: list[str] = []
            for _ in range(3):
                response = await request(
                    app,
                    "GET",
                    path + "?" + urlencode(params),
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                page = response.json()["history"]
                ids.extend(item["record_id"] for item in page["observations"])
                serialized = json.dumps(page)
                self.assertNotIn("secret.invalid", serialized)
                self.assertNotIn("private-registry", serialized)
                if page["next_cursor"] is None:
                    break
                params["cursor"] = page["next_cursor"]
            self.assertEqual(ids, ["d", "c", "b", "a"])
            self.assertIsNone(page["next_cursor"])
            self.assertEqual(
                page["observations"][0]["targets"][0]["runtime_identity_status"], "match"
            )
            denied_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="other-lane"),
                record_store_factory=lambda: store,
            )
            denied = await request(
                denied_app,
                "GET",
                path + "?" + urlencode(params),
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(denied.status_code, 403)
            store.write_public_ingress_observation_record(
                observation("naive-record", 45).model_copy(
                    update={"observed_at": stamp(45).removesuffix("Z")}
                )
            )
            invalid_evidence = await request(
                app,
                "GET",
                path + "?" + urlencode({"since": stamp(0), "until": stamp(180)}),
                headers={"Authorization": "Bearer valid-token"},
                capture_server_error_response=True,
            )
            self.assertGreaterEqual(invalid_evidence.status_code, 500)
            self.assertLess(invalid_evidence.status_code, 600)


class LaneHealthHistoryAccountingTests(unittest.TestCase):
    def test_partial_or_mixed_pages_cannot_produce_a_percentage(self) -> None:
        from control_plane.cli_ingress import ingress
        from control_plane.lane_health_history import (
            LaneHealthHistory,
            project_observation,
            summarize_history_pages,
        )

        page = LaneHealthHistory(
            product="example-site",
            context="example-site",
            instance="prod",
            since=START,
            until=START + timedelta(seconds=60),
            cursor=None,
            next_cursor="next",
            observations=(project_observation(observation("pass", 0)),),
        )
        with self.assertRaises(ValueError):
            summarize_history_pages([page], required_checks=REQUIRED, identity_checks=REQUIRED)
        with self.assertRaises(ValueError):
            summarize_history_pages(
                [page, page.model_copy(update={"cursor": "wrong", "next_cursor": None})],
                required_checks=REQUIRED,
                identity_checks=REQUIRED,
            )
        complete = page.model_copy(update={"next_cursor": None})
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "page.json"
            path.write_text(
                json.dumps({"trace_id": "trace", "history": page.model_dump(mode="json")})
            )
            runner = CliRunner()
            refused = runner.invoke(
                ingress,
                [
                    "health-history-summary",
                    "--page",
                    str(path),
                    "--check",
                    "public_http:health",
                    "--identity-check",
                    "public_http:health",
                ],
            )
            self.assertNotEqual(refused.exit_code, 0)
            path.write_text(
                json.dumps({"trace_id": "trace", "history": complete.model_dump(mode="json")})
            )
            result = runner.invoke(
                ingress,
                [
                    "health-history-summary",
                    "--page",
                    str(path),
                    "--check",
                    "public_http:health",
                    "--identity-check",
                    "public_http:health",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(
                json.loads(result.output)["checks"][0]["health_and_identity_seconds"], 60
            )

    def test_equal_time_conflict_and_initial_absence_are_never_passes(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        result = summarize_observations(
            (observation("z-pass", 30), observation("a-fail", 30, status="fail")),
            since=START,
            until=START + timedelta(seconds=60),
            instance="prod",
            required_checks=REQUIRED,
            identity_checks=REQUIRED,
        )
        check = result.checks[0]
        self.assertEqual(check.health_pass_seconds, 0)
        self.assertEqual(check.health_fail_seconds, 30)
        self.assertEqual(check.health_unknown_seconds, 30)

    def test_time_denominator_counts_failure_and_missing_evidence(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        freshness = health_monitor_freshness_seconds("prod")
        end = freshness * 4
        records = (
            observation("pass", 0),
            observation("fail", freshness, status="fail"),
            observation("recover", freshness * 3),
        )
        result = summarize_observations(
            records,
            since=START,
            until=START + timedelta(seconds=end),
            instance="prod",
            required_checks=REQUIRED,
            identity_checks=REQUIRED,
        )
        check = result.checks[0]
        self.assertEqual(result.denominator_seconds, end)
        self.assertEqual(check.health_pass_seconds, freshness * 2)
        self.assertEqual(check.health_fail_seconds, freshness)
        self.assertEqual(check.health_unknown_seconds, freshness)
        self.assertEqual(check.health_and_identity_seconds, freshness * 2)
        self.assertEqual(check.health_pass_percent, 50)

    def test_identity_is_compared_and_reconciliation_cannot_extend_pass(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        good = observation("good", 0)
        bad = observation("bad", 30)
        observed = bad.targets[0].observed_runtime_identity
        assert observed is not None
        bad = bad.model_copy(
            update={
                "targets": (
                    bad.targets[0].model_copy(
                        update={
                            "observed_runtime_identity": observed.model_copy(
                                update={"artifact_id": "wrong"}
                            )
                        }
                    ),
                )
            }
        )
        reconciliation = observation("config", 60).model_copy(
            update={"purpose": "reconciliation", "status": "skipped"}
        )
        result = summarize_observations(
            (good, bad, reconciliation),
            since=START,
            until=START + timedelta(seconds=180),
            instance="prod",
            required_checks=REQUIRED,
            identity_checks=REQUIRED,
        )
        check = result.checks[0]
        self.assertEqual(check.health_and_identity_seconds, 30)
        self.assertEqual(check.health_pass_seconds, 60)
        self.assertEqual(check.health_unknown_seconds, 120)


def assert_history_time_bounds(testcase: unittest.TestCase, store: PostgresRecordStore) -> None:
    from control_plane.lane_health_history import read_lane_health_history

    offset = observation("offset", 0).model_copy(
        update={"observed_at": "2026-09-30T20:00:30-04:00"}
    )
    records = (
        observation("before", -30),
        observation("fraction", 30.5),
        offset,
        observation("tie", 30),
        observation("until", 60),
        observation("other", 45).model_copy(update={"instance": "testing"}),
    )
    for record in records:
        store.write_public_ingress_observation_record(record)
    pages = []
    cursor = None
    for _ in records:
        page = read_lane_health_history(
            store,
            product="example-site",
            context="example-site",
            instance="prod",
            since=START,
            until=START + timedelta(seconds=60),
            limit=1,
            cursor=cursor,
        )
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            break
    testcase.assertEqual(
        [item.record_id for page in pages for item in page.observations],
        ["fraction", "tie", "offset", "before"],
    )
    with testcase.assertRaises(ValueError):
        read_lane_health_history(
            store,
            product="example-site",
            context="example-site",
            instance="testing",
            since=START,
            until=START + timedelta(seconds=60),
            limit=1,
            cursor=pages[0].next_cursor,
        )


class LaneHealthHistoryStorageTests(unittest.TestCase):
    def test_time_bounds_and_cursor_use_instants(self) -> None:
        with TemporaryDirectory() as temporary:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary) / "records.sqlite")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            assert_history_time_bounds(self, store)


class LaneIntersectionTests(unittest.TestCase):
    def test_disjoint_failures_and_a_missing_required_check_cannot_hide(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        records = (
            observation("a-pass", 0, check="a"),
            observation("a-fail", 20, check="a", status="fail"),
            observation("a-recover", 40, check="a"),
            observation("b-pass", 0, check="b"),
            observation("b-fail", 60, check="b", status="fail"),
            observation("b-recover", 80, check="b"),
        )
        required: tuple[tuple[PublicIngressCheckKind, str], ...] = (
            ("public_http", "a"),
            ("public_http", "b"),
        )
        result = summarize_observations(
            records,
            since=START,
            until=START + timedelta(seconds=100),
            instance="prod",
            required_checks=required,
            identity_checks=required,
        )
        self.assertEqual(result.lane_health_pass_seconds, 60)
        self.assertEqual(result.lane_health_and_identity_seconds, 60)
        self.assertEqual(result.lane_health_fail_seconds, 40)
        missing = summarize_observations(
            records,
            since=START,
            until=START + timedelta(seconds=100),
            instance="prod",
            required_checks=(*required, ("private_http", "missing")),
            identity_checks=required,
        )
        self.assertEqual(missing.lane_health_pass_seconds, 0)
        self.assertEqual(missing.lane_health_unknown_seconds, 60)
        self.assertEqual(missing.missing_checks, (("private_http", "missing"),))


class HistoryStreamSelectionTests(unittest.TestCase):
    def test_unlisted_failure_requires_explicit_diagnostic_exclusion(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        records = (
            observation("health", 0),
            observation("source-fail", 0, check="source", status="fail").model_copy(
                update={"check_kind": "provider"}
            ),
            observation("source-pass", 30, check="source").model_copy(
                update={"check_kind": "provider"}
            ),
        )
        with self.assertRaises(ValueError):
            summarize_observations(
                records,
                since=START,
                until=START + timedelta(seconds=60),
                instance="prod",
                required_checks=REQUIRED,
                identity_checks=REQUIRED,
            )
        excluded: tuple[tuple[PublicIngressCheckKind, str], ...] = (("provider", "source"),)
        result = summarize_observations(
            records,
            since=START,
            until=START + timedelta(seconds=60),
            instance="prod",
            required_checks=REQUIRED,
            identity_checks=REQUIRED,
            excluded_checks=excluded,
        )
        self.assertEqual(result.lane_health_pass_seconds, 60)
        self.assertEqual(result.excluded_checks, excluded)
        source = next(check for check in result.checks if check.check_name == "source")
        self.assertEqual(source.health_fail_seconds, 30)
        included = summarize_observations(
            records,
            since=START,
            until=START + timedelta(seconds=60),
            instance="prod",
            required_checks=(*REQUIRED, *excluded),
            identity_checks=REQUIRED,
        )
        self.assertEqual(included.lane_health_pass_seconds, 30)
        with self.assertRaises(ValueError):
            summarize_observations(
                records,
                since=START,
                until=START + timedelta(seconds=60),
                instance="prod",
                required_checks=REQUIRED,
                identity_checks=REQUIRED,
                excluded_checks=REQUIRED,
            )

    def test_expired_and_reconciliation_only_checks_are_missing(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        required: tuple[tuple[PublicIngressCheckKind, str], ...] = (
            ("public_http", "expired"),
            ("public_http", "reconciled"),
        )
        records = (
            observation("expired", -health_monitor_freshness_seconds("prod") - 1, check="expired"),
            observation("reconciled", 0, check="reconciled").model_copy(
                update={"purpose": "reconciliation", "status": "skipped"}
            ),
        )
        result = summarize_observations(
            records,
            since=START,
            until=START + timedelta(seconds=60),
            instance="prod",
            required_checks=required,
            identity_checks=required,
        )
        self.assertEqual(result.missing_checks, required)
        self.assertEqual(result.lane_health_unknown_seconds, 60)
