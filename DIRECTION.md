# Direction

This file is the current direction for Launchplane. When an issue, milestone,
or other document disagrees with it, this file wins and the other source is
corrected or closed. Issues are a work list, not instructions.

## Purpose

Launchplane is the small control layer that lets agents build, preview,
deploy, promote, back up, restore, and merge every product, and record a
Client's accept-or-reject decision. SellYourOutboard and VeriReel are the only
real live production sites; the CM website is next; every other product's
"prod" is not live.

Judge every change by one question: can a product be maintained without
anyone touching Launchplane? Work that adds upkeep to Launchplane itself needs
a strong reason. Prefer deleting a concept to adding one.

A product repository builds its own artifacts from its own files, with
provenance Launchplane can verify; it never calls Launchplane, and Launchplane
never supplies its build inputs. Launchplane reacts to source-control events,
verifies which repository and commit an artifact came from, and deploys it
with the site's runtime settings and secrets. The artifact is the only handoff
between them.

Launchplane needs no caller grant for the work it starts from source-control
events: verifying a build and deploying it to that site's previews and testing
lane. Requests from people or other agents still need grants. This does not
replace the Director's approval at a stop boundary, a Client's release
acceptance, or a backup gate.

Code and tests are upkeep. A change that deletes code or tests without losing
a behavior needs no other reason. A test earns its place by catching a real
regression, not by restating the code or its wording.

Only an admin or Launchplane merges. Clients can veto a change, never merge
one. The merge train is the delivery path; when the train itself is broken,
merge through the protected branch and record why in the pull request.

Each product records its Client: the person whose business it serves, whose
acceptance releases it. A production release needs the Client's acceptance,
unless the Client is the Director, the person whose direction this file
follows; then the Director's standing direction is the acceptance. Either way
the release runs the same gated path: verified backup, release record,
post-deploy checks, automatic rollback. Admin is a permission, not a role; the
Director normally holds it.

## Stop Boundaries

An agent asks the Director before:

- deploying to, promoting, or changing a real live site (SellYourOutboard,
  VeriReel, and the CM website once it launches)
- restoring or deleting data, or weakening a backup gate
- creating credentials, granting access, or changing who can merge
- spending money or creating paid resources
- anything a Client should weigh in on

Everything else is ordinary engineering and needs no ceremony, including
work on products that are not live.

Reading is never a stop. The Director's agents may read every Launchplane
record and ask only before a write, a grant, or a change.

## Journey

Three real CM website changes in a row go pull request → preview → testing →
production: the agent marks the change, Launchplane @mentions Justin, Justin
approves the release checklist, a backup is taken, and nobody touches
Launchplane by hand. Whatever blocks that run is the next piece of work.

## Retired

- ordinary-agent delegated delivery (enrollment, sessions, leases,
  authorization schema v3); its code is deleted, not extended
- guessing who must approve: change-impact routing, `product-owner` policy
  records, `owner-acceptance` grants and exact bindings, shadow mode, and the
  manager, delegate, and waiver roles
- a GitHub approval standing in for a Client's decision in Launchplane
- Every Code; Codex Lab runs agent work, and old identifiers stay only until
  their readers move
- hardware-key authorization recovery, disposable canaries, and the dev lane
- billing and collections, and general planning or work graphs inside
  Launchplane
- the blanket authorization freeze; granting access is a stop boundary instead
- product repositories calling Launchplane: workflow-identity grants, pinned
  reusable Launchplane workflows, and Launchplane-held build settings
- caller-grant rules for the preview and testing deploys Launchplane starts
  from source-control events

A retired concept comes back only through a direction change, in a shape that
fits this file.

## Milestones

- `Product repos never call Launchplane` proves the CM website's previews,
  testing deploys, and releases run from its own builds with no Launchplane
  workflow, grant, or build setting referenced by its repository; ends if a
  Launchplane change forces a product repository change, or a product change
  needs a new Launchplane grant.
- `CM website live through Launchplane` proves the journey once: Client
  acceptance at release is built, and three changes in a row go through with
  Justin; ends if Justin has to use GitHub, the Director touches Launchplane
  by hand, or the old approval code still decides a merge.
- `Merge train that just works` proves an agent's merge lands in one pass
  without a wedge, a hand merge, or main going red on its own; ends if the
  friction log gains the same entry twice.
- `Real prod safe on SellYourOutboard and VeriReel` proves every promotion to
  a live site is preceded by a verified backup and both products promote
  cleanly; ends if a live promotion happens without one.
- `Retired machinery deleted` proves the retired designs above are gone from
  the code; ends if a deletion breaks a live product.
