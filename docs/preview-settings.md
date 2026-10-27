# Preview runtime settings

Every product with previews enabled has a configuration target named `preview`,
in its configured preview context. It is a settings scope, not a stable lane or
an individual PR's runtime identity. Use the existing product-config UI and
`product-config-dry-run` / `product-config-apply` helpers with top-level
`instance: preview`. Plain settings use an `instance` runtime-environment record;
managed runtime secrets use `context_instance`, also with instance `preview`.
The product workspace's **Preview settings** link opens the same runtime-setting
and managed-secret forms used for stable environments. Expected secret bindings
can be declared for this context and instance through the existing expected-config
operation.

Preview refreshes retain their template baseline, then overlay only the exact
preview instance's settings and secrets. Global and context-shared secrets are
excluded. The existing runtime key-safety policy must permit a preview secret
before it is decrypted; production-only and testing-only classes cannot be
copied into this scope. `copy_from` supports the preview target through the same
product ownership, source-read authorization, version and sharing-evidence checks
as other product configuration. Preview settings never participate in testing
or production resolution. Changes take effect on the next preview refresh.

The release migration moves existing `preview.override_env` values into the
managed preview record and adds their preview-specific expected-key declarations.
It removes the values from the profile. Conflicting managed values, retired keys
or ambiguous product ownership abort the transaction, preserving both sources for
review. Product-profile writes reject nonempty `preview.override_env`; use the
managed product-config path for all subsequent changes. Historical profile
snapshots remain readable. The old one-key profile editor is retired.

For a plain setting, save a private product-config payload outside the checkout:

```json
{
  "product": "example-site",
  "context": "example-site",
  "instance": "preview",
  "reason": "Configure previews",
  "runtime_env": {"scope": "instance", "env": {"ODOO_PROXY_MODE": "True"}}
}
```

Use the installed write-action helper's `product-config-dry-run --payload-file`
and inspect its redacted result before `product-config-apply --payload-file`
with the same payload and a new idempotency key for that review. Keep private
inputs and the original key after an uncertain apply; reconcile the original
operation before another write. A denial stops that operation. Names-only reads
use `instance-runtime-settings-read` and `instance-secrets-read`, with the
product's preview context and `--instance preview`.

For Authentik, use `ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64`: base64-encoded UTF-8
JSON in the existing `OdooPostDeployPayload` format, with literal addon settings
for `authentik_sso.client_id`, `base_url`, and `provider_name`. Keep the Odoo
provider name `Authentik`. This implicit consumer has no client-secret input.
Website-bootstrap and import-source composition preserve explicitly managed
preview addon settings; template addon settings remain excluded. The artifact
must support the runtime setting.

This payload is an addon-settings manifest only: it must match the preview
context, instance `preview`, and deploy intent. Config parameters, website
settings and website bootstrap belong to the generated runtime payload and are
not accepted here. Store the manifest as a plain setting, with canonical managed
secret references for credential-valued addon settings; do not store the whole
encoded payload as a secret. Plain integration endpoints such as `SMTP_HOST`
are accepted, but credentials, including password-bearing connection strings,
require managed bindings. Integration credentials still need a matching existing
runtime key-safety rule that permits preview use. A rule cannot override a
binding declared production-only or testing-only.
