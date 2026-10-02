import unittest

from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.deployment_record import IntegrationKeyReadbackEvidence
from control_plane.contracts.secret_record import SecretBinding, SecretSharingReason
from control_plane.integration_key_readback import integration_key_readback

_CONTEXT = "repairshopr-sync"
_TIMESTAMP = "2026-10-02T00:00:00Z"


class _ReadbackStore:
    def __init__(
        self,
        *,
        bindings: tuple[SecretBinding, ...],
        policies: tuple[RuntimeKeySafetyPolicyRecord, ...] | None = None,
    ) -> None:
        self.bindings = bindings
        self.policies = (
            policies
            if policies is not None
            else (
                RuntimeKeySafetyPolicyRecord(
                    record_id="policy-1",
                    source="test",
                    updated_at=_TIMESTAMP,
                    rules=(
                        RuntimeSecretSafetyRule(binding_key="UNRELATED", secret_class="testing"),
                    ),
                ),
            )
        )

    def list_runtime_key_safety_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = tuple(record for record in self.policies if not status or record.status == status)
        return records[:limit] if limit is not None else records

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        return tuple(
            binding
            for binding in self.bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )


def _binding(binding_key: str, *, instance: str = "testing", **update: object) -> SecretBinding:
    return SecretBinding(
        binding_id=f"binding-{binding_key.lower()}",
        secret_id=f"secret-{binding_key.lower()}",
        integration="runtime_environment",
        binding_key=binding_key,
        context=_CONTEXT,
        instance=instance,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
    ).model_copy(update=update)


_REASON = SecretSharingReason(
    kind="read_only_source",
    reason="Testing imports from the production account.",
    evidence="The Client confirmed a read-only token on 2026-10-02.",
)


class IntegrationKeyReadbackTests(unittest.TestCase):
    def _readback(
        self, *bindings: SecretBinding, instance: str = "testing"
    ) -> IntegrationKeyReadbackEvidence:
        return integration_key_readback(
            record_store=_ReadbackStore(bindings=bindings),
            context_name=_CONTEXT,
            instance_name=instance,
        )

    def test_unreasoned_shared_production_key_is_reported_not_refused(self) -> None:
        evidence = self._readback(
            _binding("REPAIRSHOPR_API_TOKEN", declared_secret_class="shared_safe"),
            _binding("CONTACT_ALERT_DISCORD_WEBHOOK_URL"),
        )

        self.assertEqual(evidence.status, "reported")
        self.assertEqual(evidence.checked_binding_keys, ("REPAIRSHOPR_API_TOKEN",))
        self.assertEqual(
            [(finding.binding_key, finding.code) for finding in evidence.findings],
            [("REPAIRSHOPR_API_TOKEN", "sharing_reason_missing")],
        )

    def test_reasoned_shared_production_key_passes(self) -> None:
        evidence = self._readback(
            _binding(
                "REPAIRSHOPR_API_TOKEN",
                declared_secret_class="shared_safe",
                sharing_reason=_REASON,
            )
        )

        self.assertEqual(evidence.status, "pass")
        self.assertEqual(evidence.findings, ())

    def test_unclassified_production_key_fails(self) -> None:
        evidence = self._readback(_binding("STRIPE_SECRET_KEY", instance=""))

        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.findings[0].code, "unclassified_binding")

    def test_lane_without_integration_keys_or_policy(self) -> None:
        self.assertEqual(
            self._readback(_binding("CONTACT_ALERT_DISCORD_WEBHOOK_URL")).status, "skipped"
        )
        self.assertEqual(
            integration_key_readback(
                record_store=_ReadbackStore(bindings=(), policies=()),
                context_name=_CONTEXT,
                instance_name="testing",
            ).status,
            "unavailable",
        )
        self.assertEqual(
            integration_key_readback(
                record_store=object(), context_name=_CONTEXT, instance_name="testing"
            ).status,
            "unavailable",
        )


if __name__ == "__main__":
    unittest.main()
