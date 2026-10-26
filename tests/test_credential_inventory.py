from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import argparse
import plistlib
from pathlib import Path
import subprocess
import sys
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Any, Literal
from types import ModuleType

from pydantic import ValidationError

from control_plane import credential_inventory_collection as collector
from control_plane.credential_inventory import (
    COLLECTION_MAX_AGE,
    INVENTORY_ROUTE,
    CredentialCollection,
    collection_receipt_id,
    inventory_receipt,
    inventory_view,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client
from tests.test_authz_administration_read import (
    _database_app,
    _local_admin_policy,
    _github_human_policy,
    _human_session_manager,
    _browser_headers,
)
from control_plane.service_auth import GitHubHumanIdentity


def collection_payload(now: datetime) -> dict[str, object]:
    return {
        "attempted_at": now.isoformat(),
        "status": "collected",
        "snapshot": {
            "generated_at": now.isoformat(),
            "observations": [
                {
                    "source": "host-keys",
                    "status": "partial",
                    "reason": "access_filtered",
                    "records": [
                        {
                            "name": "example/key",
                            "restrictions": ["restrict"],
                            "purpose": "Unknown",
                            "rotation_due": now.date().isoformat(),
                        }
                    ],
                }
            ],
            "findings": [
                {"source": "host-keys", "name": "example/key", "kind": "unknown_key_found"}
            ],
        },
    }


def collection_at(now: datetime) -> CredentialCollection:
    return CredentialCollection.model_validate(collection_payload(now))


class CredentialInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)

    def test_metadata_contract_rejects_values_at_every_depth_and_false_absence(self) -> None:
        collection = collection_at(self.now)
        for path in (
            (),
            ("snapshot",),
            ("snapshot", "observations", 0),
            ("snapshot", "observations", 0, "records", 0),
            ("snapshot", "findings", 0),
        ):
            with self.subTest(path=path):
                payload = collection.model_dump(mode="json")
                target: Any = payload
                for key in path:
                    target = target[key]
                target["value"] = "private-fixture"
                with self.assertRaises(ValidationError):
                    CredentialCollection.model_validate(payload)
        payload = collection.model_dump(mode="json")
        payload["snapshot"]["findings"][0]["kind"] = "missing"
        with self.assertRaises(ValidationError):
            CredentialCollection.model_validate(payload)
        payload = collection.model_dump(mode="json")
        payload["snapshot"]["observations"].append(payload["snapshot"]["observations"][0])
        with self.assertRaises(ValidationError):
            CredentialCollection.model_validate(payload)

    def test_freshness_uses_collection_time_and_findings_stay_with_collector(self) -> None:
        original = collection_at(self.now)
        receipt = inventory_receipt(original, now=self.now, trace_id="fixture")
        view = inventory_view(receipt, receipt, now=self.now)
        self.assertEqual(view.coverage, "partial")
        self.assertFalse(view.stale)
        self.assertEqual(view.findings, original.snapshot.findings if original.snapshot else [])
        expired = inventory_view(
            receipt, receipt, now=self.now + COLLECTION_MAX_AGE + timedelta(seconds=1)
        )
        self.assertTrue(expired.stale)
        self.assertEqual(view.findings, expired.findings)
        empty = inventory_view(None, None, now=self.now)
        self.assertTrue(empty.stale)
        self.assertEqual(empty.coverage, "unknown")
        with self.assertRaises(ValueError):
            inventory_receipt(
                collection_at(self.now + timedelta(days=1)), now=self.now, trace_id="future"
            )

    def test_evidence_replay_and_late_old_delivery_preserve_latest_and_last_success(self) -> None:
        self.now = self.now.replace(microsecond=100000)
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/inventory.db")
            self.addCleanup(store.close)
            store.ensure_schema()
            successful = inventory_receipt(
                collection_at(self.now), now=self.now, trace_id="success"
            )
            self.assertTrue(store.write_idempotency_record_once(successful))
            self.assertFalse(store.write_idempotency_record_once(successful))
            failure = CredentialCollection(
                attempted_at=self.now + timedelta(microseconds=1), status="failed", snapshot=None
            )
            failed = inventory_receipt(
                failure, now=self.now + timedelta(minutes=1), trace_id="failure"
            )
            store.write_idempotency_record_once(failed)
            old = inventory_receipt(
                collection_at(self.now - timedelta(days=1)), now=self.now, trace_id="delayed"
            )
            store.write_idempotency_record_once(old)
            latest = store.read_latest_credential_inventory()
            self.assertEqual(latest, failed)
            prior = store.read_latest_credential_inventory(successful=True)
            self.assertEqual(prior, successful)
            view = inventory_view(latest, prior, now=self.now + timedelta(minutes=2))
            self.assertEqual(view.collection_status, "failed")
            self.assertEqual(view.snapshot, collection_at(self.now).snapshot)

    def test_publisher_accepts_partial_and_drift_exit_codes_but_never_failed_or_unsafe_output(
        self,
    ) -> None:
        snapshot = collection_at(self.now).snapshot
        assert snapshot is not None
        for code in (0, 1, 2):
            with (
                self.subTest(code=code),
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], code, snapshot.model_dump_json().encode(), b""
                    ),
                ),
            ):
                result = collector.collect_inventory(
                    collector=Path("collector.py"), catalog=Path("catalog")
                )
                self.assertEqual(result.status, "collected")
        unsafe = snapshot.model_dump(mode="json")
        unsafe["value"] = "private-fixture"
        for code, content in (
            (3, snapshot.model_dump_json()),
            (0, json.dumps(unsafe)),
            (0, "broken"),
        ):
            with (
                self.subTest(code=code, content=content),
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], code, content.encode(), b"private error"
                    ),
                ),
            ):
                self.assertEqual(
                    collector.collect_inventory(
                        collector=Path("collector.py"), catalog=Path("catalog")
                    ).status,
                    "failed",
                )
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("collector", 1)):
            self.assertEqual(
                collector.collect_inventory(
                    collector=Path("collector.py"), catalog=Path("catalog")
                ).status,
                "failed",
            )

    def test_publisher_confirms_only_its_own_receipt_and_schedule_preserves_explicit_paths(
        self,
    ) -> None:
        collection = collection_at(self.now)
        helper = ModuleType("operator_fixture")
        receipt = collection_receipt_id(collection)
        with (
            patch.object(
                helper,
                "resolve_settings",
                return_value={"service_url": "https://fixture.invalid"},
                create=True,
            ),
            patch.object(
                helper,
                "request_launchplane",
                return_value={"status": "ok", "receipt_id": receipt},
                create=True,
            ) as request,
        ):
            self.assertEqual(
                collector.publish_inventory(collection, helper=helper, config=None), receipt
            )
            self.assertEqual(request.call_args.kwargs["body"], collection.model_dump(mode="json"))
            request.return_value = {"status": "ok", "receipt_id": "private-fixture"}
            with self.assertRaises(ValueError):
                collector.publish_inventory(collection, helper=helper, config=None)
            with patch.object(
                helper, "resolve_settings", return_value={"service_url": "https://fixture.invalid"}
            ) as settings:
                request.return_value = {"status": "ok", "receipt_id": receipt}
                collector.publish_inventory(
                    collection,
                    helper=helper,
                    config="settings.json",
                    env_config="existing-auth.env",
                )
                self.assertEqual(settings.call_args.args[0].env_config, "existing-auth.env")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                collector=root / "collector.py",
                catalog=root / "catalog",
                manifest=root / "metadata.json",
                config=str(root / "operator.json"),
                env_config=None,
            )
            output = root / "schedule.plist"
            with patch.object(shutil, "which", return_value="/usr/local/bin/uv"):
                with self.assertRaises(ValueError):
                    collector.write_launch_agent(args, output)
                args.env_config = str(root / "existing-auth.env")
                collector.write_launch_agent(args, output)
                with output.open("rb") as stream:
                    job = plistlib.load(stream)
                for path in (
                    args.collector,
                    args.catalog,
                    args.manifest,
                    Path(args.config),
                    Path(args.env_config),
                ):
                    self.assertIn(str(path.resolve()), job["ProgramArguments"])
                with self.assertRaises(FileExistsError):
                    collector.write_launch_agent(args, output)

    def test_empty_rotation_dates_are_unknown_and_transport_tracebacks_are_withheld(self) -> None:
        for due in (None, ""):
            payload = collection_at(self.now).model_dump(mode="json")
            payload["snapshot"]["observations"][0]["records"][0]["rotation_due"] = due
            parsed = CredentialCollection.model_validate(payload)
            assert parsed.snapshot is not None
            self.assertEqual(parsed.snapshot.observations[0].records[0].rotation_due, "Unknown")
        from http.client import IncompleteRead

        for error in (IncompleteRead(b"private-fixture"), SyntaxError("private-fixture")):
            with (
                patch.object(
                    sys, "argv", ["publisher", "--collector", "fixture", "--catalog", "catalog"]
                ),
                patch.object(collector, "collect_inventory", return_value=collection_at(self.now)),
                patch.object(collector, "operator_helper", side_effect=error),
                patch("builtins.print") as printed,
            ):
                self.assertEqual(collector.main(), 2)
                self.assertNotIn("private-fixture", str(printed.call_args_list))


class CredentialInventoryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_requires_current_named_administrator(self) -> None:
        roles: tuple[Literal["admin", "read_only"], ...] = ("admin", "read_only")
        for role in roles:
            manager, _sessions = _human_session_manager()
            human = GitHubHumanIdentity(
                login="fixture",
                github_id=101,
                name="",
                email="",
                organizations=frozenset(),
                teams=frozenset(),
                role=role,
            )
            with (
                self.subTest(role=role),
                _database_app(_github_human_policy(role=role), human_session_manager=manager) as (
                    _store,
                    _record,
                    app,
                ),
            ):
                headers, _session_id, _csrf = _browser_headers(manager, human)
                async with lifespan_client(app) as client:
                    response = await client.get(INVENTORY_ROUTE, headers=headers)
                    self.assertEqual(
                        response.status_code, 200 if role == "admin" else 403, response.text
                    )

    async def test_authenticated_director_collection_replay_failures_and_sanitized_validation(
        self,
    ) -> None:
        with _database_app(_local_admin_policy(), director_agent=True) as (_store, _record, app):
            async with lifespan_client(app) as client:
                headers = {"Authorization": "Bearer reader-token"}
                payload = collection_payload(datetime.now(timezone.utc))
                before = await client.get(INVENTORY_ROUTE, headers=headers)
                self.assertEqual(before.status_code, 200, before.text)
                self.assertEqual(before.json()["inventory"]["collection_status"], "never")
                first = await client.post(INVENTORY_ROUTE, json=payload, headers=headers)
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(
                    first.json()["receipt_id"],
                    collection_receipt_id(CredentialCollection.model_validate(payload)),
                )
                replay = await client.post(INVENTORY_ROUTE, json=payload, headers=headers)
                self.assertEqual(first.json()["receipt_id"], replay.json()["receipt_id"])
                unsafe = dict(payload, value="never-echo-this-private-fixture")
                rejected = await client.post(INVENTORY_ROUTE, json=unsafe, headers=headers)
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertNotIn(unsafe["value"], rejected.text)
                failed = await client.post(
                    INVENTORY_ROUTE,
                    headers=headers,
                    json={
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                        "status": "failed",
                        "snapshot": None,
                    },
                )
                self.assertEqual(failed.status_code, 200, failed.text)
                read = await client.get(INVENTORY_ROUTE, headers=headers)
                self.assertEqual(read.headers["cache-control"], "no-store")
                self.assertEqual(read.json()["inventory"]["collection_status"], "failed")
                self.assertIsNotNone(read.json()["inventory"]["snapshot"])

    async def test_product_scoped_agent_and_anonymous_cannot_read_or_publish(self) -> None:
        with _database_app(_local_admin_policy()) as (_store, _record, app):
            async with lifespan_client(app) as client:
                for headers in ({}, {"Authorization": "Bearer reader-token"}):
                    with self.subTest(headers=headers):
                        read = await client.get(INVENTORY_ROUTE, headers=headers)
                        self.assertIn(read.status_code, (401, 403))
                        write = await client.post(
                            INVENTORY_ROUTE,
                            headers=headers,
                            json=collection_payload(datetime.now(timezone.utc)),
                        )
                        self.assertIn(write.status_code, (401, 403))
