# Direction

This file is the current direction for Launchplane. The Director's overall
direction in `cbusillo/direction` comes first. When an issue, milestone, or
other document here disagrees with this file, this file wins and the other
source is corrected or closed. Issues are a work list, not instructions.

## Purpose

Launchplane is the small control layer that lets agents build, preview,
deploy, promote, back up, restore, and merge every product, and record a
Client's accept-or-reject decision. A live site is the production lane of a
product recorded as live. A product is recorded as live when its Client uses
production for real business; a missing or wrong record does not override
the overall direction's stop on Client business systems.

Judge every change by one question: can a product be maintained without
anyone touching Launchplane? Work that adds upkeep to Launchplane itself needs
a strong reason. Prefer deleting a concept to adding one.

A product repository builds its own artifacts from its own files, with
provenance Launchplane can verify; it never calls Launchplane, and Launchplane
never supplies its build inputs. Launchplane reacts to source-control events,
verifies which repository and commit an artifact came from, and deploys it
with the site's runtime settings and secrets. The artifact is the only handoff
between them.

Launchplane should not depend on GitHub. GitHub is the git host we use
today; others may follow, including one we run ourselves. New code talks to
GitHub through Launchplane's own names for things (a change, a comment, a
check result, a merge); old code moves over only when other work touches it.
Adding another host is its own milestone, when the Director chooses it.

Launchplane needs no caller grant for the work it starts from source-control
events (verifying a build and deploying it to that site's previews and testing
lane) or from a Client's recorded release acceptance (the gated promotion).
Requests from people or other agents still need grants. This does not replace
the Director's approval at a stop boundary, a Client's release acceptance, or
a backup gate.

Code and tests are upkeep. A change that deletes code or tests without losing
a behavior needs no other reason. A test earns its place by catching a real
regression, not by restating the code or its wording.

Only an admin or Launchplane merges. Clients can veto a change, never merge
one. The merge train is the delivery path; when the train itself is broken,
merge through the protected branch and record why in the pull request.

Launchplane records each product's Client and runs every production release
through the same gated path: verified backup, release record, post-deploy
checks, automatic rollback. Who accepts a release is set in
`cbusillo/direction`, and that acceptance is what starts the release, so
accepting is a production action: the review says so in plain words, the
decision stays bound to the exact candidate it reviewed, an admin can hold
releases without editing a record, and an admin override is still a hand
promotion. A Client's issue or comment never merges, promotes, or counts as
acceptance. Admin is a permission, not a role; the Director normally holds it.

## Stop Boundaries

An agent asks the Director before:

- changing a real live site outside the Client's accepted release; a deploy or promotion there comes only from that accepted release, and rolling back to an earlier Client-accepted release is part of that gated path
- restoring or deleting a live site's data, or weakening a backup gate that protects a live site
- creating credentials that can reach a live site, granting access, or changing who can merge
- spending money or creating paid resources
- anything a Client should weigh in on; that question goes to the Client

Everything else is ordinary engineering and needs no ceremony, including
work on products that are not live. The overall direction owns the rule for
testing and preview lanes.

The overall direction owns who may read and plan, including Client agents.

## Journey

Three real CM website changes in a row go issue → pull request → preview →
testing → production: Justin opens the issue, the agent marks the change,
Launchplane @mentions Justin, Justin accepts the release checklist, a backup is
taken, Launchplane promotes, and nobody touches Launchplane by hand. Whatever
blocks that run is the next piece of work.

## Retired

- ordinary-agent delegated delivery (enrollment, sessions, leases,
  authorization schema v3); its code is deleted, not extended
- guessing who must approve: change-impact routing, `product-owner` policy
  records, `owner-acceptance` grants and exact bindings, shadow mode, and the
  manager, delegate, and waiver roles
- a GitHub approval standing in for a Client's decision in Launchplane
- Every Code and Codex Lab; old identifiers stay only until their readers
  move
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
  acceptance at release is built, and three changes in a row that Justin opens
  as issues reach production with no act by Justin beyond opening the issue
  and accepting, and none by the Director; ends if Justin has to use GitHub
  for anything beyond opening an issue, the Director touches Launchplane by
  hand, or the old approval code still decides a merge.
- `Merge train that just works` proves an agent's merge lands in one pass
  without a wedge, a hand merge, or main going red on its own; ends if the
  friction log gains the same entry twice.
- `Real prod safe on SellYourOutboard and VeriReel` proves every promotion to
  a live site is preceded by a verified backup and both products promote
  cleanly; ends if a live promotion happens without one.
- `Retired machinery deleted` proves the retired designs above are gone from
  the code; ends if a deletion breaks a live product.
