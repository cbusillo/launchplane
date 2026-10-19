# Retry an exhausted testing artifact

When an Odoo testing artifact exhausts its automatic deploy attempts after a
control-plane failure, use `POST /v1/products/{product}/testing-retry` on the
deployed Launchplane service. This control re-arms the reconciler for the exact
desired artifact, retaining existing data. It does not rebuild the product.
Use the current service contract's `rearm_testing_retry` operation with an
authenticated caller that already has `odoo_target_replacement_apply.execute`
for that product's testing lane. No new grant is part of this operation.

1. Read the product's reconcile requests and take `desired_artifact_id` and
   `desired_commit` from its testing plan. Fix the underlying failure first.
2. GET that route with query parameters `artifact_id`, `source_git_ref` (the
   desired commit), and a concrete `reason`. Review uses `product_environment.read`
   and needs no write grant or CSRF token. A write-capable caller may also POST
   these inputs with `mode: "dry-run"` and `instance: "testing"`.
   Review the returned artifact, source, last failed operation and `plan_sha256`.
   This step writes no record and queues nothing.
3. POST those artifact/source/reason inputs with `mode: "apply"`, `instance: "testing"` and
   `reviewed_plan_sha256` equal to that digest. Supply one unique
   `Idempotency-Key` header. Retain this exact request and key.
4. Read the testing reconcile request and its queued operation, then the
   product environment and activity. An accepted re-arm is a queue wakeup,
   not evidence of a successful deploy or healthy testing lane.

If a response is lost, repeat the exact apply request with its original key.
Launchplane looks up the committed receipt before rechecking mutable build or
lane evidence. A replay cannot replenish the budget or queue another request,
even after the original operation finishes or the desired build advances.
Changing the request under the same key refuses with `idempotency_key_reused`.

The service verifies that the requested artifact/source is still the newest
verified desired testing build and that its reconciler attempt chain is
exhausted. Apply compares the reviewed inventory and testing release tuple
independently, including either record's absence, as well as product ownership
and target identity. Either source moving refuses with `testing_retry_stale`;
they need not agree with one another to be fenced. Review fresh evidence rather
than substituting a new key for an uncertain operation.

The re-arm, reconcile wakeup and original-key receipt commit together. Failed
operation history stays intact. The reconciler starts a new bounded budget
after the reviewed failed operation, under its existing stable keys and own
authorization. It rechecks verified builds, forward movement, image scanning,
staff holds and active/unknown operations. A changed inventory, release tuple,
profile or target identity invalidates that re-arm boundary. A re-armed deploy
uses `data_source_mode: "existing"` and refuses empty data. An incomplete fresh
testing restore must finish through its own supported recovery first.

This route accepts only testing lanes. It neither promotes to production nor
clears old operations, restores data, changes access, or lifts a staff hold.
The service and its worker must both run this version before use. Re-arm metadata
lives in a separate record; the queue payload keeps its existing format so an
older Launchplane image can still read and claim work after emergency rollback.
