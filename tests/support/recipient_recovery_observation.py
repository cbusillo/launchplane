"""Exercise the same terminal-witness classification on both recovery purposes."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from typing import Any
from unittest.mock import patch

import yaml

from control_plane.production_recipient import (
    RecipientAuthorityChanged,
    authority_snapshot,
    custody,
)
from control_plane.production_recipient_recovery import RECOVERY_ROUTE


PROVIDER_DRIFT = (
    "retained_environment",
    "recipient_identity",
    "recipient_isolation",
    "provider_source",
    "provider_yaml_source",
    "provider_environment",
    "provider_labels",
    "provider_label_identity",
    "provider_render",
)
AUTHORITY_DRIFT = (
    "inactive",
    "public_website",
    "public_hosts",
    "pending_hosts",
    "shared_target",
    "lane_ownership",
    "shared_ownership",
    "driver",
    "extensions",
    "commit_authority_race",
)
UNREADABLE_OBSERVATIONS = (
    "retained_malformed",
    "recipient_malformed",
    "retained_truncated",
    "missing_recipient_environment",
    "missing_recipient_source_type",
    "missing_recipient_auto_deploy",
    "malformed_source",
    "malformed_yaml_source",
    "malformed_yaml_converted",
    "converted_read_error",
    "malformed_converted",
    "malformed_services",
    "malformed_labels",
    "malformed_label_values",
    "malformed_environment",
    "malformed_flags",
    "provider_read_error",
    "authority_read_error",
    "missing_profile",
)


@contextmanager
def observation_change(fixture: Any, scenario: str) -> Iterator[None]:
    store = fixture.fixture.store
    record = fixture.record
    provider = fixture.fixture.provider
    original_provider = deepcopy(provider)
    with ExitStack() as changes:
        changes.callback(lambda: provider.update(original_provider))
        payload = provider[record.target_id]
        if scenario == "retained_environment":
            provider[record.retained_target_id.target_id]["env"] += "\nCHANGED=observed"
        elif scenario == "retained_malformed":
            provider[record.retained_target_id.target_id] = {}
        elif scenario == "recipient_malformed":
            provider[record.target_id] = {}
        elif scenario == "retained_truncated":
            retained = provider[record.retained_target_id.target_id]
            provider[record.retained_target_id.target_id] = {
                key: retained[key] for key in ("composeId", "appName")
            }
        elif scenario.startswith("missing_recipient_"):
            key = {
                "missing_recipient_environment": "environmentId",
                "missing_recipient_source_type": "sourceType",
                "missing_recipient_auto_deploy": "autoDeploy",
            }[scenario]
            payload.pop(key)
        elif scenario == "recipient_identity":
            payload["autoDeploy"] = True
        elif scenario == "recipient_isolation":
            payload["domains"] = [{"host": "changed.invalid"}]
        elif scenario in {"provider_source", "provider_yaml_source"}:
            document = json.loads(payload["composeFile"])
            document["services"][record.request.application_service]["command"] = ["changed"]
            payload["composeFile"] = (
                yaml.safe_dump(document)
                if scenario == "provider_yaml_source"
                else json.dumps(document)
            )
        elif scenario == "provider_environment":
            payload["env"] += "\nCHANGED=observed"
        elif scenario == "malformed_yaml_source":
            payload["composeFile"] = "services: ["
        elif scenario == "malformed_yaml_converted":
            changes.enter_context(
                patch(
                    "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
                    return_value="services: [",
                )
            )
        elif scenario == "converted_read_error":
            changes.enter_context(
                patch(
                    "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
                    side_effect=TimeoutError("Conversion read unavailable"),
                )
            )
        elif scenario == "malformed_source":
            payload["composeFile"] = "{"
        elif scenario == "malformed_environment":
            payload["env"] = {"unreadable": "environment"}
        elif scenario == "malformed_flags":
            payload["autoDeploy"] = "unreadable"
        elif scenario in {
            "provider_labels",
            "provider_label_identity",
            "provider_render",
            "malformed_converted",
            "malformed_services",
            "malformed_labels",
            "malformed_label_values",
        }:
            document = json.loads(payload["composeFile"])
            service = document["services"][record.request.application_service]
            if scenario == "provider_labels":
                service["labels"] = {"public.routing": "changed"}
            elif scenario == "provider_label_identity":
                service["labels"] = {
                    "dokploy.application.id": "another-recipient",
                    "dokploy.service": record.request.application_service,
                }
            elif scenario == "provider_render":
                service["command"] = ["changed"]
            elif scenario == "malformed_labels":
                service["labels"] = 42
            elif scenario == "malformed_label_values":
                service["labels"] = {"dokploy.application.id": {"unreadable": "label"}}
            elif scenario == "malformed_services":
                document["services"] = []
            else:
                document = []
            changes.enter_context(
                patch(
                    "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
                    return_value=json.dumps(document),
                )
            )
        elif scenario == "provider_read_error":
            changes.enter_context(
                patch(
                    "control_plane.production_recipient.api.fetch_dokploy_target_payload",
                    side_effect=TimeoutError("Provider read unavailable"),
                )
            )
        elif scenario == "authority_read_error":
            changes.enter_context(
                patch.object(
                    store,
                    "read_product_profile_record",
                    side_effect=RuntimeError("Authority unreadable"),
                )
            )
        elif scenario == "missing_profile":
            changes.enter_context(
                patch.object(
                    store,
                    "read_product_profile_record",
                    side_effect=FileNotFoundError("Profile unreadable"),
                )
            )
        elif scenario == "shared_target":
            production = store.read_dokploy_target_id_record(
                context_name=record.context, instance_name="prod"
            )
            other = record.retained_target_id.model_copy(
                update={"context": "other-lane", "target_id": production.target_id}
            )
            store.write_dokploy_target_id_record(other)
        elif scenario in {"public_hosts", "pending_hosts"}:
            target = store.read_dokploy_target_record(
                context_name=record.context, instance_name="prod"
            )
            changes.callback(store.write_dokploy_target_record, target)
            fields = target.model_dump(mode="json")
            if scenario == "public_hosts":
                fields["public_hosts"] = ["changed.invalid"]
            else:
                fields["pending_public_host_effects"] = [
                    {
                        "host": "changed.invalid",
                        "provider_host": "changed.invalid",
                        "target_id": record.target_id,
                        "runtime_port": 8000,
                    }
                ]
            store.write_dokploy_target_record(type(target).model_validate(fields))
        elif scenario == "owned_row_race":
            changed = False

            def read_then_change(*args: Any) -> Any:
                nonlocal changed
                snapshot = authority_snapshot(*args)
                if not changed:
                    changed = True
                    inventory = store.read_environment_inventory(
                        context_name=record.context, instance_name="prod"
                    )
                    store.write_environment_inventory(
                        inventory.model_copy(update={"updated_at": "changed"})
                    )
                return snapshot

            changes.enter_context(
                patch(
                    "control_plane.production_recipient_published_observation.authority_snapshot",
                    side_effect=read_then_change,
                )
            )
        elif scenario == "commit_authority_race":
            writer = store.write_production_recipient

            def change_then_write(**kwargs: Any) -> None:
                replacement = kwargs["replacement"]
                if replacement.get("recovery_proof") or any(
                    key.startswith("published_recovery:") for key in replacement["execution"]
                ):
                    target = store.read_dokploy_target_record(
                        context_name=record.context, instance_name="prod"
                    )
                    store.write_dokploy_target_record(
                        target.model_copy(update={"updated_at": "changed"})
                    )
                writer(**kwargs)

            changes.enter_context(
                patch.object(store, "write_production_recipient", side_effect=change_then_write)
            )
        else:
            profile = store.read_product_profile_record(record.product)
            changes.callback(store.write_product_profile_record, profile)
            fields = profile.model_dump(mode="json")
            if scenario == "inactive":
                fields["lifecycle_state"] = "retired"
                fields["preview"]["enabled"] = False
            elif scenario == "public_website":
                fields["public_website"] = "required"
            elif scenario == "lane_ownership":
                fields["lanes"] = [lane for lane in fields["lanes"] if lane["instance"] != "prod"]
            elif scenario == "shared_ownership":
                fields["product"] = "other-product"
            elif scenario == "driver":
                fields["driver_id"] = "odoo"
            elif scenario == "extensions":
                next(lane for lane in fields["lanes"] if lane["instance"] == "prod")[
                    "generic_web_extensions"
                ] = {"maintenance": [{"name": "changed", "command": "true"}]}
            else:
                raise AssertionError("Unknown observation scenario: " + scenario)
            store.write_product_profile_record(type(profile).model_validate(fields))
        yield


def assert_observation_scenarios(case: Any, scenarios: tuple[str, ...], *, qualifies: bool) -> None:
    """Give each row isolated records; PostgreSQL delegates supply the normal clone factory."""
    for scenario in scenarios:
        database_factory = case.scenario_database
        with (
            case.subTest(scenario=scenario),
            nullcontext("") if database_factory is None else database_factory() as database_url,
        ):
            fixture = type(case)()
            fixture.database_url = database_url
            fixture.setUp()
            try:
                _assert_observation(fixture, scenario, qualifies=qualifies)
            finally:
                fixture.doCleanups()


def assert_optional_provider_fields_remain_qualified(fixture: Any) -> None:
    """Omitted and null optional fields retain the provider's established semantics."""
    provider = fixture.fixture.provider
    retained = provider[fixture.record.retained_target_id.target_id]
    recipient = provider[fixture.record.target_id]
    for key in ("domains", "mounts", "serviceNetworks", "randomize", "isolatedDeployment"):
        recipient.pop(key, None)
    with patch(
        "control_plane.production_recipient_recovery.run_recipient_program",
        side_effect=fixture.host,
    ):
        fixture.assertTrue(fixture.run_recovery().response_payload["result"]["recovered"])
    for key in ("domains", "mounts", "serviceNetworks", "randomize", "isolatedDeployment"):
        recipient[key] = None
    for key in (
        "environmentId",
        "sourceType",
        "composeType",
        "autoDeploy",
        "domains",
        "mounts",
        "serviceNetworks",
    ):
        retained[key] = None
    fixture.assertTrue(fixture.adapter.current()[1].recovered)


def _assert_observation(fixture: Any, scenario: str, *, qualifies: bool) -> None:
    adapter = fixture.adapter
    store = fixture.fixture.store
    binding = adapter.request.reviewed_plan_sha256
    terminal: dict[str, str] = {}
    calls: list[str] = []
    observable = False

    def host(_root: Any, _record: Any, spec: Any, _program: Any, checkpoint: Any) -> Any:
        calls.append("inspect" if spec.get("inspect") else "apply")
        checkpoint("original_capture_witness")
        if not spec.get("inspect"):
            if adapter.request.purpose == "published":
                terminal.update(fixture.evidence(spec))
            else:
                terminal.update(
                    {
                        "recipient_recovery_ok": "true",
                        "recipient_recovery_binding": binding,
                        "recipient_recovery_capture": "f" * 64,
                        "recipient_recovery_empty": "false",
                        "recipient_recovery_epoch": str(
                            int(datetime.now(timezone.utc).timestamp())
                        ),
                    }
                )
            raise TimeoutError("Original host reply was lost")
        if not observable:
            raise TimeoutError("Original witness is not readable yet")
        return dict(terminal)

    def reservation() -> Any:
        return store.read_idempotency_record(
            scope=adapter.scope,
            route_path=RECOVERY_ROUTE.format(product=adapter.product),
            idempotency_key=adapter.original_key,
        )

    with patch(
        "control_plane.production_recipient_recovery.run_recipient_program", side_effect=host
    ):
        fixture.assertEqual(fixture.run_recovery().status, "reconcile_required")
        original_reservation = reservation()
        original = custody(store, adapter.product, adapter.request.context)
        assert original is not None
        owner = original_attempt_owner(fixture, original, binding)
        observable = True
        with observation_change(fixture, scenario):
            result = fixture.run_recovery()
            if qualifies:
                fixture.assertEqual(result.status, "reconcile_required")
                fixture.assertEqual(
                    store.list_held_provider_target_reservations(), (reservation(),)
                )
                fixture.assertEqual(fixture.run_recovery().status, "reconcile_required")
                second = type(store)(database_url=store.database_url)
                try:
                    other = replace(adapter, store=second, original_key="competing-key")
                    competing = (
                        fixture.run_recovery(other)
                        if adapter.request.purpose == "published"
                        else fixture.run_recovery(other, second)
                    )
                    fixture.assertEqual(competing.status, "target_busy")
                finally:
                    second.close()
            else:
                fixture.assertEqual(result.status, "adopted")
                fixture.assertFalse(result.response_payload["result"]["recovered"])
                fixture.assertEqual(result.response_payload["result"]["plan_sha256"], binding)
                saved = custody(store, adapter.product, adapter.request.context)
                assert saved is not None
                fixture.assertEqual(saved.recovery_proof, original.recovery_proof)
                fixture.assertEqual(
                    saved.published_recovery_proofs, original.published_recovery_proofs
                )
        if qualifies:
            result = fixture.run_recovery()
            fixture.assertEqual(result.status, "adopted")
            fixture.assertTrue(result.response_payload["result"]["recovered"])
            fixture.assertEqual(result.response_payload["result"]["plan_sha256"], binding)
        saved = custody(store, adapter.product, adapter.request.context)
        assert saved is not None
        if owner is not None:
            fixture.assertEqual(saved.execution[owner[0]], owner[1])
        completed = reservation()
        fixture.assertEqual(completed.created_at, original_reservation.created_at)
        fixture.assertEqual(
            completed.provider_effect_started_at, original_reservation.provider_effect_started_at
        )
        fixture.assertEqual(store.list_held_provider_target_reservations(), ())
        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=AssertionError("Replay must not contact the host"),
        ):
            replay = fixture.run_recovery()
        fixture.assertEqual(replay.status, "replayed")
        fixture.assertEqual(replay.response_payload, result.response_payload)
    fixture.assertEqual(calls.count("apply"), 1)


def assert_equivalent_yaml_source_qualifies(fixture: Any) -> None:
    payload = fixture.fixture.provider[fixture.record.target_id]
    payload["composeFile"] = yaml.safe_dump(json.loads(payload["composeFile"]))
    with patch(
        "control_plane.production_recipient_recovery.run_recipient_program",
        side_effect=fixture.host,
    ):
        completed = fixture.run_recovery()
    fixture.assertTrue(completed.response_payload["result"]["recovered"])
    fixture.assertTrue(fixture.adapter.current()[1].recovered)


def original_attempt_owner(
    fixture: Any, record: Any, binding: str
) -> tuple[str, dict[str, Any]] | None:
    if fixture.adapter.request.purpose != "published":
        return None
    entries = [
        (key, value)
        for key, value in record.execution.items()
        if isinstance(value, dict)
        and value.get("binding") == binding
        and value.get("original_key") == fixture.adapter.original_key
        and value.get("operation")
    ]
    fixture.assertEqual(len(entries), 1)
    return entries[0]


def assert_committed_receipt_survives_later_drift(fixture: Any, *, recovered: bool = True) -> None:
    adapter = fixture.adapter
    store = fixture.fixture.store
    binding = adapter.request.reviewed_plan_sha256
    provider = fixture.fixture.provider
    retained = fixture.record.retained_target_id.target_id
    original_retained = deepcopy(provider[retained])
    terminal: dict[str, str] = {}
    observable = False
    calls: list[str] = []
    committed = False
    writer = store.write_production_recipient

    def host(root: Any, record: Any, spec: Any, program: Any, checkpoint: Any) -> Any:
        calls.append("inspect" if spec.get("inspect") else "apply")
        if not spec.get("inspect"):
            terminal.update(fixture.host(root, record, spec, program, checkpoint))
            raise TimeoutError("Original completed reply lost")
        if not observable:
            raise TimeoutError("Original witness not observable yet")
        checkpoint("original_terminal_witness")
        return dict(terminal)

    def write_then_drift(**kwargs: Any) -> None:
        nonlocal committed
        writer(**kwargs)
        record = custody(store, adapter.product, adapter.request.context)
        assert record is not None
        proof = (
            record.recovery_proof
            if adapter.request.purpose == "bootstrap"
            else record.published_recovery_proofs.get(binding)
        )
        if proof:
            committed = True
            provider[retained]["env"] += "\nAFTER_COMMIT=changed"

    with patch(
        "control_plane.production_recipient_recovery.run_recipient_program", side_effect=host
    ):
        fixture.assertEqual(fixture.run_recovery().status, "reconcile_required")
        original = custody(store, adapter.product, adapter.request.context)
        assert original is not None
        owner = original_attempt_owner(fixture, original, binding)
        observable = True
        try:
            with patch.object(store, "write_production_recipient", side_effect=write_then_drift):
                completed = fixture.run_recovery()
            fixture.assertTrue(committed)
            fixture.assertEqual(completed.status, "adopted")
            result = completed.response_payload["result"]
            fixture.assertEqual(result["recovered"], recovered)
            fixture.assertEqual(result["plan_sha256"], binding)
            saved = custody(store, adapter.product, adapter.request.context)
            assert saved is not None
            proof = (
                saved.recovery_proof
                if adapter.request.purpose == "bootstrap"
                else saved.published_recovery_proofs[binding]
            )
            fixture.assertEqual(result["capture_sha256"], proof["capture_sha256"])
            fixture.assertEqual(result["recovered_at"], proof["recovered_at"])
            if owner is not None:
                fixture.assertEqual(saved.execution[owner[0]], owner[1])
            with fixture.assertRaises(RecipientAuthorityChanged):
                adapter.current()
            with patch(
                "control_plane.production_recipient_recovery.run_recipient_program",
                side_effect=AssertionError("Receipt replay must not read the provider"),
            ):
                replay = fixture.run_recovery()
            fixture.assertEqual(replay.status, "replayed")
            fixture.assertEqual(replay.response_payload, completed.response_payload)
        finally:
            provider[retained] = original_retained
        fixture.assertEqual(adapter.current()[1].recovered, recovered)
        fixture.assertEqual(store.list_held_provider_target_reservations(), ())
    fixture.assertEqual(calls.count("apply"), 1)


def assert_json_number_source_qualifies(case: Any) -> None:
    """Prepare and recover a valid source whose JSON exponent differs from YAML 1.1."""
    from tests.test_production_recipient import ProductionRecipientTests

    original_setup = ProductionRecipientTests.setUp
    original_run = ProductionRecipientTests.run_operation

    def source_setup(fixture: Any) -> None:
        original_setup(fixture)
        fixture.document["services"]["web"]["environment"]["RATE"] = 1e-7
        fixture.body = fixture.body.model_copy(
            update={"mode": "dry-run", "reviewed_plan_sha256": ""}
        )
        fixture.adapter.request = fixture.body
        reviewed = fixture.adapter.current()
        fixture.body = fixture.body.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": reviewed.plan_sha256}
        )
        fixture.adapter.request = fixture.body

    def converted(fixture: Any, **kwargs: Any) -> str:
        document = json.loads(fixture.provider[kwargs["compose_id"]]["composeFile"])
        return yaml.safe_dump(document, allow_unicode=True)

    def prepare(fixture: Any, *args: Any, **kwargs: Any) -> Any:
        with patch(
            "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
            side_effect=lambda **kw: converted(fixture, **kw),
        ):
            result = original_run(fixture, *args, **kwargs)
        fixture.assertEqual(result.status, "completed")
        return result

    database_factory = case.scenario_database
    with (
        nullcontext("") if database_factory is None else database_factory() as database_url,
        patch.object(ProductionRecipientTests, "setUp", source_setup),
        patch.object(ProductionRecipientTests, "run_operation", prepare),
    ):
        fixture = type(case)()
        fixture.database_url = database_url
        try:
            fixture.setUp()
            with (
                patch(
                    "control_plane.production_recipient.compose.fetch_dokploy_converted_compose_file",
                    side_effect=lambda **kw: converted(fixture.fixture, **kw),
                ),
                patch(
                    "control_plane.production_recipient_recovery.run_recipient_program",
                    side_effect=fixture.host,
                ),
            ):
                completed = fixture.run_recovery()
                fixture.assertTrue(completed.response_payload["result"]["recovered"])
                fixture.assertTrue(fixture.adapter.current()[1].recovered)
                fixture.assertEqual(len(fixture.calls), 1)
                with patch(
                    "control_plane.production_recipient_recovery.run_recipient_program",
                    side_effect=AssertionError("Receipt replay must not contact the host"),
                ):
                    replay = fixture.run_recovery()
                fixture.assertEqual(replay.response_payload, completed.response_payload)
        finally:
            fixture.doCleanups()
