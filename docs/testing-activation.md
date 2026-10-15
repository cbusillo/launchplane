---
title: Independent generic-web testing activation
---

`POST /v1/products/{product}/testing-activation` consumes the original caller's
completed independent-storage preparation receipt. It creates a new empty
project, environment and Compose recipient on the original placement. It never
starts, updates, stops or deletes the old recipient, changes production, creates
credentials, settles historical deployment effects or deploys an application.

The authenticated Director agent uses `scripts/activate-generic-web-testing.py`
with the installed `launchplane-write-action.py` helper. Keep its request and evidence files private,
outside the checkout. The request identifies the existing context, original
`preparation_key`, exact `source_commit`, volume-role to existing setting-key
mapping, existing port-setting keys and an administrative reason. It also names
`interlock_record_sha256`: the exact target-record digest from the supported
testing-hold read, explicitly identifying the retained hold as this operation's
staging interlock. Legacy reason prose never supplies that intent. An absent or
foreign hold, known staff use, a production alias, changed allocation or unsafe
carried key refuses activation. The original hold is copied unchanged to the new
recipient and retained in immutable old-target custody; its purpose is not relabeled.
Choose existing, unretired lane setting keys for the volume roles; activation
refuses a retired key rather than reviving a setting that runtime sync removes.

For a product explicitly declaring no public website, retained origin-domain
records stay on the old recipient and in custody. The replacement has no domains
or public routes. Declared public hosts, pending public-host effects, or origin
domains on a public product still require their own accepted cutover. Changing
the website declaration after review invalidates activation.

The service reads the product's Compose source and local includes from one
immutable repository commit through its existing source reader, verifies that
commit's product-owned release build, and carries only the lane's managed
configuration. Existing testing credentials must be lane scoped and classified
for testing. Shared keys require recorded read-only-source evidence. Opaque
comparison against the product's production provider environment prevents
testing credentials with production values. Values never enter client output.

The fixed host inspector requalifies the original allocation's empty local
backing directories, labels and all existing container mounts, including stopped
containers. Every requested host port must be absent from every container's
configured bindings and available to bind. Nothing inspects production data
contents or mounts production storage. The new provider source and environment
are read back, then its actual converted Compose is interpolated with those
same managed values and checked against the reviewed source. It can use only
the two qualified volumes, explicit independent ports and its private Compose
network. Host mounts, host capabilities, external networks, routing labels,
unclassified credentials and unqualified external `*_HOST`/`*_URL` environment
endpoints refuse it. Application code remains responsible for the behavior of
its declared read-only source integration.

Use `dry-run` with a fresh `--evidence-file`, inspect the public result, then
`apply --reviewed-dry-run --idempotency-key ORIGINAL` against that saved review.
The request's `instance` accepts only `testing`. The client supplies no target,
volume name, port value, secret value or credential source. PostgreSQL provides
cross-process executor and authority exclusion. Final recipient publication and
its custody receipt commit atomically, with the unchanged hold still set.

Keep the original key after interruption. `receipt` reads its immutable completed
result; `readback` checks the current published recipient. An original-key apply
may adopt a fully persisted, exactly matching configuration after a lost reply
and fresh storage/port readback. It does not redispatch creation. Missing creation
identities are recovered only from one unambiguous original namespace and
placement; only previously undispatched creation steps continue. Unknown absent
creates, drift, active work or incomplete evidence stay fenced. An interrupted
configuration can resume only when the unused recipient still exactly matches
its recorded pre-write configuration; a completed matching write is adopted.
Preserve resources and the original key; never delete or reallocate to retry.

The new recipient uses raw Compose from the reviewed immutable source commit.
Image deployments continue normally; later product Compose changes require a
separately reviewed source/configuration update. Publication persists only the
qualified volume and port overrides, so old recipient overrides cannot return
shared storage or production ports during ordinary reconciliation.

After separation is proved, use the existing supported testing-hold path:
`scripts/stage-testing-separation.py lift-dry-run`, then `lift-apply`, with the
unchanged hold's actual `--hold-purpose`, the activation's
`--activation-plan-sha256`, a fresh evidence file and stable lift key. The same
caller must still own that exact unchanged interlock. The service rechecks the
actual safe provider render and empty resources/ports before the lift, and
checks authority again while clearing it. A new or foreign staff hold remains
protected. This bounded continuation requests normal testing reconciliation;
an ordinary staging-hold lift without activation proof keeps its existing
no-reconcile behavior. A lost post-clear receipt can retry the original lift and
recover its reconcile request. Its dry-run reads metadata only; apply rechecks
the provider and executes the fixed read-only host inspection before clearing.

Only the exact retained reservation of a published qualified replacement stops
blocking the new recipient. The old reservation remains unchanged and continues
to fence the old target, with its unresolved outcome visible in the durable
reservation and activation custody receipt. The lane monitor excludes only
that proven old-recipient fence from the active lane's deploy-fence check; it
continues to report any fence on its actual new recipient. A
plain target repoint has no such effect. Existing incidents resolve through the
normal monitor when current health and runtime identity pass; they are never
manually closed or suppressed by activation.

Keep the new volumes and both recipients if qualification or deployment fails.
Rollback never restores shared production settings or starts the old containers.
Read back the original operation and fix the supported testing continuation.
The migration is additive and its downgrade preserves custody for recovery.
Qualification refusals name the fixed failing check in their public error code;
provider or unexpected exception text remains private. Keep the request trace
and original allocation/key while addressing that check, then plan again.
Actual testing acceptance still requires passing health, matching expected and
observed runtime identity and no open incident. Source tests and activation's
no-deploy receipt alone do not establish that acceptance.
