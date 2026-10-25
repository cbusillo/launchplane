# Preview runtime settings

Odoo previews inherit their configured template lane's settings, then apply the
product profile's `preview.override_env`. Keep preview-specific values in that
layer rather than changing the template or shared context.

From this repository, use `scripts/set-preview-override.py` with the installed
Launchplane write-action helper and its existing private configuration. The
service-authenticated Director agent can conditionally set one nonsecret key
on an existing enabled Odoo preview. Deletion and edits to other profile
fields still require full product-profile authority, apart from the existing
verified-image scan activation, the one-way [clean-snapshot opt-in](clean-testing-snapshots.md#product-transition-and-custody)
and separate [testing storage requirements](service-boundary.md#testing-storage-requirements).
Snapshot opt-in can hold new copies before the testing budget is qualified;
capture and snapshot use still require that budget. It does not use the runtime-key helper below. The helper preserves
every other profile field and prints no supplied value. It creates no scope
or grant and starts no deployment.

Write the string value to a mode-0600 file outside Git checkouts. The helper
trims surrounding whitespace. The evidence file must also be outside the
checkout and must not already exist for a new dry run.

```sh
uv run python scripts/set-preview-override.py dry-run \
  --operator-helper /path/to/installed/launchplane-write-action.py \
  --product example-site --key ODOO_PROXY_MODE \
  --value-file /private/preview-value.txt \
  --evidence-file /private/preview-review.json --reason 'Configure previews'
```

Inspect the private evidence, then use `apply` instead of `dry-run`, adding
`--reviewed-dry-run --idempotency-key <new-key-for-this-review>`. Keep that key
for this review's retries; a new review needs a new key. Use `readback` with the same
files to verify an uncertain outcome without writing. Preserve the files and
original key after an uncertain apply. A denial stops the operation.

The profile GET supplies a strong `ETag`; the helper preserves this opaque
revision and submits it as `If-Match` to the existing profile POST. The service rejects a stale
revision before saving and retains atomic compare-and-write protection.
Same-key idempotent replay precedes the revision precondition. The helper
refuses services without revision support, changed input, concurrent profile
edits, or evidence changing anything beyond the selected preview key.
The service reconstructs the one-key change from its stored profile and
compares the entire replacement before its atomic save. Recognized credential
keys, managed-secret destinations and credential payloads are refused on this
Director-agent path. Encoded addon payloads must contain only literal
nonsecret addon settings for the preview. Plain override transports and
business-integration keys are refused; those use their managed settings
routes. An existing encoded payload must meet the same restrictions before
replacement. A save records intent; runtime delivery and login verification
remain separate.

Refusals include a fixed `reason` code without input values. For
`profile_changed_since_review`, read the changed profile and create a new dry
run. `conditional_save_unsupported` means the read supplied no single strong revision.
`private_file_permissions` requires mode-0600 file permissions. An
`outcome_unknown` or `accepted_unverified` result requires readback with the
original files and idempotency key; do not start another write.
Readback does not require revision support or a matching local profile model.

For Authentik, use `ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64`: base64-encoded UTF-8
JSON in the existing `OdooPostDeployPayload` format, with literal addon settings
for `authentik_sso.client_id`, `base_url`, and `provider_name`. Keep the Odoo
provider name `Authentik`. This implicit consumer has no client-secret input.
Website-bootstrap and import-source composition must preserve these addon
settings before runtime delivery; see [PR #4585](https://github.com/cbusillo/launchplane/pull/4585).
Set `ODOO_PROXY_MODE` and `LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED` with separate
one-key reviews when needed. The artifact must support the runtime setting.
