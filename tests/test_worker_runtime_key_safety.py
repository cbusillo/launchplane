import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.worker_runtime_key_safety import (
    enforce_worker_runtime_key_safety,
)


WORKER_BINDING_KEY = "EXAMPLE_PROD_WORKER_PRIVATE_KEY"


class WorkerRuntimeKeySafetyTests(unittest.TestCase):
    def _database_url(self, root: Path) -> str:
        return f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"

    def _write_binding(
        self,
        store: PostgresRecordStore,
        *,
        context: str,
        instance: str,
    ) -> None:
        store.write_secret_binding(
            SecretBinding(
                binding_id="example-prod-worker-private-key-binding",
                secret_id="example-prod-worker-private-key",
                integration="runtime_environment",
                binding_key=WORKER_BINDING_KEY,
                context=context,
                instance=instance,
                status="configured",
                created_at="2026-07-11T00:00:00Z",
                updated_at="2026-07-11T00:00:00Z",
            )
        )

    def _write_policy(
        self,
        store: PostgresRecordStore,
        *,
        binding_key: str,
        secret_class: RuntimeSecretClass,
    ) -> RuntimeKeySafetyPolicyRecord:
        policy = RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-worker-test",
            status="active",
            source="test",
            updated_at="2026-07-11T00:00:00Z",
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key=binding_key,
                    secret_class=secret_class,
                    allowed_contexts=("example",),
                    allowed_instances=("prod",),
                ),
            ),
        )
        store.write_runtime_key_safety_policy_record(policy)
        return policy

    def test_context_scoped_unsafe_binding_is_enforced(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = self._database_url(root)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                self._write_binding(store, context="example", instance="")
                self._write_policy(
                    store,
                    binding_key=WORKER_BINDING_KEY,
                    secret_class="testing",
                )
            finally:
                store.close()

            with patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaises(click.ClickException) as caught:
                    enforce_worker_runtime_key_safety(
                        context_name="example",
                        instance_name="prod",
                        allowed_worker_keys=(WORKER_BINDING_KEY,),
                        operation_name="example prod worker",
                    )

        self.assertIn(f"secret_class_not_allowed[{WORKER_BINDING_KEY}]", str(caught.exception))

    def test_global_unclassified_binding_is_named_with_policy_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = self._database_url(root)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                self._write_binding(store, context="", instance="")
                policy = self._write_policy(
                    store,
                    binding_key="UNRELATED_SECRET",
                    secret_class="prod_only",
                )
            finally:
                store.close()

            with patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaises(click.ClickException) as caught:
                    enforce_worker_runtime_key_safety(
                        context_name="example",
                        instance_name="prod",
                        allowed_worker_keys=(WORKER_BINDING_KEY,),
                        operation_name="example prod worker",
                    )

        message = str(caught.exception)
        self.assertIn(f"unclassified_binding[{WORKER_BINDING_KEY}]", message)
        self.assertIn(policy.record_id, message)
        self.assertIn(policy.policy_sha256, message)


if __name__ == "__main__":
    unittest.main()
