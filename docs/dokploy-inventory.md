---
title: Read-only Dokploy inventory
---

`GET /v1/dokploy/inventory?limit=25` enumerates every project and environment
visible to Launchplane's configured Dokploy credential, including empty projects,
empty environments, untracked/self-hosting workloads and database-only targets.
It reads the provider directly rather than inferring inventory from product or
preview records. It does not prove that a target is running or healthy.

The authenticated route uses the existing global `dokploy_target.inspect`
authorization boundary for product/context `launchplane`, including the existing
Director-agent read role. A scoped `driver.read` permission does not authorize
fleet enumeration. No new grants or credentials are created by this source change.

The response contains `trace_id` and an `inventory` page. Each project has
`project_id`, `name` and `environments`; each environment has `environment_id`,
`name` and `targets`. Targets contain only `target_id`, `category`, `name` and
`placement`. Placement is either `server` with provider `server_id`/`server_name`,
or `dokploy_host` with null server fields. The local host's OS/guest hostname is
unverified; server names are Dokploy display labels. Correlate these with the
supported host inventory before calling them physical guest identities.
Application, compose, PostgreSQL, MySQL, MariaDB, MongoDB, Redis and libSQL targets
are covered. Env values, compose source, descriptions, addresses and credentials
are never returned. Provider/configuration errors return a fixed value-free
503 with a trace, never raw provider error text or a partial-success inventory.

`project.all` is an unpaginated provider read. Launchplane validates its complete
project/environment/target membership, sorts by project ID, then reads target
details and server labels only for the selected project page. `total_projects`,
`total_environments`, `total_targets`, `offset`, `limit`, `next_offset`,
`inventory_digest`, `scope=provider_credential_visible` and
`provider_pagination=unpaginated_project_all` describe coverage explicitly.

Follow `next_offset` with the first page's `inventory_digest` and the same limit
until it is null. A continuation without a digest is HTTP 400. Membership or
project/environment name changes invalidate it with HTTP 409; restart from zero.
The digest binds these root-snapshot facts, not a transaction across live target
names/placement reads. A moved/disappearing target or unreadable server rejects
the entire page. Completeness is limited to the provider credential's visibility,
not every organization/account or Docker runtime container outside Dokploy.

The route bounds the snapshot to 2,000 projects, 10,000 environments and 20,000
targets. Page size is 1–100 projects (default 25), with at most 500 detail targets.
Each page has a 25-second provider-call budget (each call at most 10 seconds).
Budget overflow is unavailable, not truncation. Reduce `limit` for a dense page;
a single over-budget project requires a later capability change. Missing target
collections or placement, malformed IDs/names and duplicate identities also fail
closed. An explicit empty list is valid; an absent list is unknown.

The companion codex-skills caller is
`uv run <launchplane-skill>/scripts/launchplane-write-action.py dokploy-inventory-read --limit 25`.
Continue with `--offset N --inventory-digest DIGEST`. This helper is a bounded
local extension until the public agent contract projects the route. Runtime use
requires normal service delivery; this source task authorizes no deployment,
provider change, record change or naming mutation. After delivery, the naming
inventory session reruns the read and reconciles provider labels with guest data.
