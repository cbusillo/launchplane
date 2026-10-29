---
title: Service Runtime Settings
---

# Service runtime settings

`POST /v1/service/runtime-settings` sets one declared non-secret key in
Launchplane's own runtime-environment record (`scope=context`,
`context=launchplane`). No product profile owns that record, so the
product-config route refuses it with `product_config_lane_not_owned`; this
route is the supported writer.

The declared keys and their value checks live in `DECLARED_SERVICE_SETTINGS` in
[`service_runtime_settings.py`](../control_plane/service_runtime_settings.py).
Each value is checked by its declared parser, so an invalid value
is refused before review. Any other key, including a secret-shaped one, is
refused. Secrets stay in [managed secret records](secrets.md).

`LAUNCHPLANE_ONBOARDING_GITHUB_APP_CLIENT_ID` prepares the existing onboarding
App selector for [service adoption](secrets.md#client-credential-input).
Its value must be a nonempty identifier of at most 128 ASCII letters, digits or
underscores. Recording it does not switch the current onboarding workflows away
from their Actions inputs, prove an installation, or create any credential.

The request names `key`, `value` and `reason`. `mode: dry-run` returns
`result.plan_digest` and writes nothing. `mode: apply` repeats the same request with
`expected_plan_digest` and an `Idempotency-Key` header. Apply refuses when the
record changed since the review, and writes the setting and its idempotency
receipt together. Other keys in the record stay as they are. Repeating an apply
with the same key and request returns the original result, marked `replayed`
with its `original_trace_id`. A different request under that key is refused.
Responses carry key names and a SHA-256 of the value,
never the value. Read the result back with the names-only
`GET /v1/contexts/launchplane/runtime-settings`.

Callers need `launchplane_service.runtime_settings.plan` for a dry run and
`launchplane_service.runtime_settings.apply` for apply, on product and context
`launchplane`. See [standing permission](authorization-authority.md#standing-agent-read-plan-and-proven-projection-permission)
for the Director's agents. Terminal-agent credentials are refused. The route
sets values; it does not remove keys.
