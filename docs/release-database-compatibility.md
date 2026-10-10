---
title: Release Database Compatibility
---

Launchplane classifies Odoo releases from the **production-to-candidate artifact
pair**, independently of approval routing. `build_release_review` includes the
result in the release checklist. The saved Client decision retains it in its
existing DB-backed payload; the release-review API reads it back. Its exact
artifact IDs, image references and canonical manifest hashes bind the result to
the release tuple. The existing checklist digest binds Client acceptance to that artifact pair.
Derived classification evidence is excluded from the decision digest, so adding
it does not invalidate an existing acceptance of the same immutable tuple.
Historical saved checklists without the field retain their serialized shape;
they are not proof of compatibility. Newly compiled reviews classify legacy
artifacts conservatively without treating missing evidence as compatibility.

`compatible` means the producer declares that both versions can read **and
write** the unchanged database, and the full examined diff needs no install,
update or migration. Static releases still require fresh workers, warmed assets
and retention of references used by old pages. The result records those
requirements; it is not proof that warming or runtime readiness happened.
The cutover/readiness implementation must enforce them before overlap.

Missing production history, missing verification or complete declarations,
conflicting identities, unknown files, and changed opaque framework/base-image,
build or dependency inputs yield `database_changing`. An examined, hash-bound
opaque-input module plan permits targeted work on that conservative path.
Unknown evidence also
marks `module_plan_complete=false`: consumers must refuse to execute an
incomplete plan, rather than guessing modules or upgrading everything.
Classification does not waive any backup, Client-acceptance, credential or
traffic gate. This component neither deploys nor changes the current maintenance
executor. The targeted consumer and safe registry startup belong to
[odoo-devkit#198](https://github.com/cbusillo/odoo-devkit/issues/198); cutover
consumption belongs to [#3266](https://github.com/cbusillo/launchplane/issues/3266).
Generic-web's existing artifact does not supply these declarations; its separate
cutover work must retain a conservative result until it has verified proof.

## Additive Odoo producer contract

Schema-2 artifact manifests may carry `release_compatibility`, modeled in
`control_plane/contracts/artifact_release_compatibility.py`. It travels through
[verified build ingestion](artifact-provenance.md), with no product-to-Launchplane
call, secret or grant. Devkit's producer supplies it as part of #198. It is absent
from older artifacts; absence never means compatibility.

The declaration has version 1, `complete`, `read_write_compatible`, `sources` and
`modules`. `complete=true` attests to a complete inventory of all source files
used to build the artifact, including non-addon inputs. It is a reviewed build's
assertion, not a runtime inspection of the database. The consumer compares both
inventories, including additions/removals, rather than a truncated source-host
compare endpoint or the tenant diff alone. A producer must not set completeness
when it cannot enumerate its inputs.

Every source names `input_name`, exact `repository`, full `commit`, and `files`.
The consumer matches the source set and identities against the manifest:

| Input name | Manifest authority |
| --- | --- |
| `tenant` | Verified source build repository and source commit |
| `addon:<repository>` | Each addon source and exact ref |
| `base:runtime`, `base:devtools` | Each base image source repository/ref |
| `tool:<name>` | Each build tool's source repository/ref |
| `external:<repository>:<dependency_file_path>` | Each external compatibility input |

All are required, even when a source did not change. Selectors must agree with
the addon source refs. The producer must include upstream/framework/base input
files in the relevant source inventory; unchanged opaque digests are the only
safe equivalence for inputs whose contents cannot be compared. Base digest,
enterprise base digest, build flags, selector, platform, lock hash, package
inventory and external dependency changes remain conservative even if the
source file declarations suggest compatibility.

To examine an opaque change, both declarations supply `opaque_inputs_sha256`,
matching the fingerprint defined by `release_opaque_inputs_sha256` in
`control_plane/release_compatibility.py`. That function is the canonical
semantic projection and hashing specification. The producer supplies
`database_update_modules` naming the resolved update roots for its examined
inputs. `null` means unexamined; an explicit empty list asserts no module DB
work is required. Names must exist in the full module graph. Hash mismatch,
missing baseline hash or missing candidate plan keeps execution incomplete.
A matching plan stays `database_changing` and expands dependents, providing a
supported path for framework/base-image and Python dependency updates without
silently declaring overlap safe or copying the artifact install list.

Each file names a normalized relative `path`, content `sha256`, `kind`, optional
`module`, and (for an assets-only manifest) `manifest_database_sha256`:

- `static`: filesystem SCSS/CSS, JS, static QWeb XML, media or fonts under
  `static`. Requires warming and retained old references, not blanket `-u`.
- `code`: Python code declared read/write compatible with no required DB work.
  Models, migrations, data/views, initialization hooks and manifests cannot hide
  behind this category. A producer must classify registry hooks or code with
  schema/data effects conservatively.
- `manifest_assets`: `__manifest__.py`; its non-assets semantic hash covers
  **all parsed fields other than assets**, including data, dependencies,
  versions and hook declarations. Only an unchanged semantic hash on both
  sides allows an assets-only classification. Additions/removals or changed
  semantic hashes require a module update.
- `database_data`, `model`, `migration`, `dependency`: DB-stored XML/views/data,
  model/schema work, migration hooks, and dependencies requiring module work.
- `docs_ci`: docs, CI and tests that do not affect runtime behavior. A producer
  must not place executable runtime files here.
- `unknown`: unexamined input; never compatible or an executable complete plan.

No file may be omitted because it is hard to classify. Use `unknown` and retain
its hash instead. Conflicting categories/module ownership remain conservative.
The two complete declarations and their hashes are persisted in the immutable
artifacts; the release records the full changed-file evidence and reasons.

`modules` is the full resolved image module graph (`name`, `depends`), including
upstream/shared modules and every dependency. Duplicate module names or missing
graph dependencies are invalid. `odoo_install_modules` remains the required
installation set, not the update set. The consumer expands required installation
dependencies and records new requirements separately in `install_modules`.
Database changes seed `update_modules`, expanded through reverse dependencies
in the candidate graph, excluding new installs. `changed_modules` records direct
file modules separately, including static/code-only modules that need no update.
Unrelated modules do not enter the update list; docs/CI-only changes induce none.

The maintenance consumer must reconcile this image graph/requirement plan with
actual database module states before executing it: optional modules may already
be installed, and image availability alone does not establish installation.
It must record the final applied list/readback and refuse missing coverage.
`-u` must preserve editor-owned and `noupdate` data; this plan never authorizes
forcing an overwrite, unsetting `noupdate`, or running before the committed
credential boundary from [#3237's decision](https://github.com/cbusillo/launchplane/issues/3237#issuecomment-6091183633).
