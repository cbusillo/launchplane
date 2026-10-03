---
title: Retired Change-Impact Records
---

Path-based approval detection is retired under [DIRECTION.md](../DIRECTION.md).
The classifier, generated-boundary inference, policy administration, and
evaluation endpoints have been deleted. Do not create or repair an impact policy
to unblock a merge. The agent marks a change for Client review using judgment;
the production release checklist supplies the Client approval boundary.

Merge admission reads current Git identities through `repository_evidence.py`.
Missing, malformed, incomplete, or changing provider evidence still refuses
admission. Engineering review uses the existing two independent authority slots,
without consulting path rules or affected-product detection.

`control_plane/contracts/retired_change_impact*.py` retains only historical payload and digest
compatibility needed by persisted policies, audits, and review records. Existing
tables, migrations, history, and storage import behavior are preserved. There is
no service route for applying or evaluating these retired policies. This change
does not delete records or mutate runtime grants.
