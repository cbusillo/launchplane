# Preview runtime settings

Odoo previews inherit their configured template lane's settings, then apply the
product profile's `preview.override_env`. Keep preview-specific values in that
layer rather than changing the template or shared context.

From this repository, use `scripts/set-preview-override.py` with the installed
Launchplane admin helper and existing private admin configuration. It
edits one key, preserves every other profile field, and prints no supplied
value. It creates no scope or grant and starts no deployment.

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
edits, or evidence changing anything beyond the selected preview key. A save
records intent; runtime delivery and login verification remain separate.

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
