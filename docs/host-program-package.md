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
Existing terminal delivery installs it for native reads and uploads. Scheduled
operations install it through disabled, manual package-bootstrap jobs on the
same selected server and provider identity as the operation. These jobs transfer
bounded archive chunks through a fixed installer; they do not restore, capture,
start containers or run product code. Installation verifies the archive digest
before atomic publication, refuses aliases and differing existing bytes, and
preserves earlier versions. A failed or uncertain installation stops delivery
before the operation schedule is written. No credential or access grant is added.

The operation schedule invokes `python3 <version>.pyz <action> --input <encoded
JSON>`. Its input references packaged helper members instead of carrying their
source. The package expands those references locally and preserves the existing
program's checks, effect ordering, arguments and output. Both the package and its
installer report the version; scheduled readback retains `launchplane_host_version`.
The provider's materialization limit still applies to command arguments and
bootstrap chunks. The compressed fresh-restore and rehearsal program envelopes
are removed.

The archive contains no runtime records, product settings, credentials or restore
data. Inputs continue to come from the operation's existing reviewed records.
An old archive is retained because an interrupted operation may still name it;
installing a new version does not authorize replaying or replacing that operation.

The existing program behavior tests execute the installed archive for restore,
rehearsal, resource and preview operations. Package tests exercise isolated
execution, version readback, corrupt/interrupted delivery, aliases and the real
provider materializer bound with the archive's actual bytes.
