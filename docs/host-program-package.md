# Host program package

Fresh testing restores, native source capture and receiver uploads, upgrade
rehearsals, resource preparation and clean-snapshot previews use the same
standalone `launchplane-host` zipapp. It contains the reviewed host programs as
readable Python members and includes their nested copy, sanitization, integration
readback and website-settings helpers. It needs the host's existing Python and
Docker tools, without the service's Python dependencies.

The archive SHA-256 is its version. A source change produces a new immutable
archive through the normal reviewed Launchplane build. To export the artifact
for an isolated inspection or rehearsal:

```sh
uv run python -m control_plane.host_package --output state/launchplane-host.pyz
python3 state/launchplane-host.pyz version
```

The Dokploy driver installs the archive in
`$HOME/.local/state/launchplane-host/<version>.pyz` on the execution host.
Native reads and uploads load a verified action-only archive in memory over the
existing terminal stream, preserving their lack of persistent host-file effects.
Scheduled operations first inspect the installed archive. Remote targets install
through that existing terminal; Dokploy's primary server uses disabled manual
bootstrap jobs with a unique installation ID and request digest in each
package namespace,
so retained installation evidence does not fill a lane's bounded inventory. Their terminal histories stay
with the original operation's recovery evidence; the existing mutation observer
continues to record them. An
installed archive skips the upload. Installation runs before environment changes or source capture. Short authority guards cover
writes and sends without covering the observation wait. It transfers bounded chunks through a
fixed installer, without starting containers or running product code. The
installer verifies the digest before atomic publication, refuses aliases,
repairs corrupt code from the same verified artifact, and preserves earlier
versions. A failed or uncertain installation stops delivery before the operation
schedule is written. No credential or access grant is added.

The operation schedule invokes `python3 <version>.pyz <action> --input -`, with
JSON supplied on stdin. Its input references packaged helper members instead of carrying their
source. The package expands those references locally and preserves the existing
program's checks, effect ordering, arguments and output. Both the package and its
installer report the archive version; actions also report their program version.
Scheduled readback verifies both.
The provider's materialization limit still applies to command arguments and
bootstrap chunks. The compressed fresh-restore and rehearsal program envelopes
are removed.

The archive contains no runtime records, product settings, credentials or restore
data. Inputs continue to come from the operation's existing reviewed records.
Old archives are retained without automatic pruning. Installing a new version
does not authorize replaying or replacing an operation.

Each action also binds its dispatcher, program and referenced helpers. Retained storage
provenance compares that binding and its input, so an unrelated action update
does not invalidate an existing preparation. The previous exact resource heredoc
format remains recognizable. The installer can verify an older installed archive; missing original code is
refused rather than silently replaced. This does not add an operation-replay path.

The existing program behavior tests execute the installed archive for restore,
rehearsal, resource and preview operations. Package tests exercise isolated
execution, version readback, corrupt/interrupted delivery, aliases and the real
provider materializer bound with the archive's actual bytes.
