"""Private, lane-bound comparison of the complete provider-target reference."""

import hmac
import json

import click

from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.secrets import keyed_secret_payload_fingerprint


class DokployTargetComparisonEvidenceUnavailableError(ValueError):
    def __init__(self) -> None:
        super().__init__(
            "Complete expected-current provider evidence is unavailable. Repeat supported "
            "inspection with comparison-key configuration available and carry its comparison "
            "fingerprint into the dry run; no record repair or grant is implied."
        )


def provider_target_comparison_fingerprint(record: ProviderTargetRecord) -> str:
    return keyed_secret_payload_fingerprint(
        json.dumps(
            {
                "context": record.context,
                "instance": record.instance,
                "reference": record.to_deployed_target_reference().model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        purpose="dokploy-domain-authority-comparison-v1",
    )


def require_provider_target_expectation(
    *,
    current: ProviderTargetRecord,
    expected: DeployedTargetReference,
    fingerprint: str,
    target: DokployTargetRecord,
    target_id: DokployTargetIdRecord,
) -> None:
    reference = current.to_deployed_target_reference()
    if fingerprint:
        # A fingerprint never excuses stale visible identity or conflicting evidence.
        if expected.model_copy(
            update={"provider_evidence": reference.provider_evidence}
        ) == reference and (
            not expected.provider_evidence
            or expected.provider_evidence == reference.provider_evidence
        ):
            try:
                current_fingerprint = provider_target_comparison_fingerprint(current)
            except click.ClickException as error:
                raise DokployTargetComparisonEvidenceUnavailableError() from error
            if hmac.compare_digest(fingerprint.encode(), current_fingerprint.encode()):
                return
    elif reference == expected:
        return
    elif (
        not expected.provider_evidence
        and current.provider_evidence
        != ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        ).provider_evidence
        and expected.model_copy(update={"provider_evidence": reference.provider_evidence})
        == reference
    ):
        raise DokployTargetComparisonEvidenceUnavailableError()
    raise ValueError(
        "Dokploy domain authority repair provider-target expectation did not match current authority."
    )
