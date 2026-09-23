# Direction

This file is the current direction for Launchplane. When an issue, milestone,
or other document disagrees with it, this file wins and the other source is
corrected or closed. Issues are a work list, not instructions.

## Purpose

Launchplane is the small control layer that lets agents build, preview,
deploy, promote, back up, restore, and merge every product, and record a site
owner's accept-or-reject decision. SellYourOutboard and VeriReel are the only
real live production sites; the CM website is next; every other product's
"prod" is not live.

Judge every change by one question: can a product be maintained without
anyone touching Launchplane? Work that adds upkeep to Launchplane itself needs
a strong reason. Prefer deleting a concept to adding one.

Only the operator or Launchplane merges. Site owners can veto a change, never
merge one. The merge train is the delivery path; when the train itself is
broken, merge through the protected branch and record why in the pull request.

## Stop Boundaries

An agent asks the operator before:

- deploying to, promoting, or changing a real live site (SellYourOutboard,
  VeriReel, and the CM website once it launches)
- restoring or deleting data, or weakening a backup gate
- creating credentials, granting access, or changing who can merge
- spending money or creating paid resources
- anything a site owner should weigh in on

Everything else is ordinary engineering and needs no ceremony, including
work on products that are not live.

## Journey

Three real CM website changes in a row go pull request → preview → testing →
production: the agent marks the change, Launchplane @mentions Justin, Justin
approves the release checklist, a backup is taken, and nobody touches
Launchplane by hand. Whatever blocks that run is the next piece of work.

## Retired

- ordinary-agent delegated delivery (enrollment, sessions, leases,
  authorization schema v3); its code is deleted, not extended
- guessing who must approve: change-impact routing, product-owner policy
  records, owner-acceptance grants and exact bindings, shadow mode, and the
  manager, delegate, and waiver roles
- a GitHub approval standing in for a site owner's decision in Launchplane
- Every Code; Codex Lab runs agent work, and old identifiers stay only until
  their readers move
- hardware-key authorization recovery, disposable canaries, and the dev lane
- billing and collections, and general planning or work graphs inside
  Launchplane
- the blanket authorization freeze; granting access is a stop boundary instead

A retired concept comes back only through a direction change, in a shape that
fits this file.

## Milestones

- `CM website live through Launchplane` proves the journey once: owner
  approval at release is built, and three changes in a row go through with
  Justin; ends if Justin has to use GitHub, the operator touches Launchplane
  by hand, or the old approval code still decides a merge.
- `Merge train that just works` proves an agent's merge lands in one pass
  without a wedge, a hand merge, or main going red on its own; ends if the
  friction log gains the same entry twice.
- `Real prod safe on SellYourOutboard and VeriReel` proves every promotion to
  a live site is preceded by a verified backup and both products promote
  cleanly; ends if a live promotion happens without one.
- `Retired machinery deleted` proves the retired designs above are gone from
  the code; ends if a deletion breaks a live product.
