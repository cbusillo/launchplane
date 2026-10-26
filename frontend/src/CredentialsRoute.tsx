import { KeyRound } from "lucide-react";
import { useCallback, useEffect } from "react";

import { readCredentialInventory } from "./api";
import { useEngineeringResource } from "./engineering-resource";
import {
  EngineeringResourceControls,
  EngineeringResourceGate,
  EngineeringRouteFrame,
} from "./EngineeringRouteUi";
import { formatTime } from "./format";
import type { CredentialInventoryResponse } from "./generated/openapi.ts";
import "./credentials.css";

const FINDING_LABELS = {
  unknown_key_found: "Unknown key found",
  unclassified_file_found: "Unclassified file found",
  missing: "Missing from a checked source",
  presence_unknown: "Presence unknown",
  rotation_due: "Rotation due",
};

export function CredentialsRoute({ admin }: { admin: boolean }) {
  const loader = useCallback((signal: AbortSignal) => readCredentialInventory(signal), []);
  const { state, refresh, cancel } = useEngineeringResource<CredentialInventoryResponse>(
    loader, "credential-inventory", admin,
  );
  useEffect(() => {
    if (!admin) return;
    const timer = window.setInterval(refresh, 60_000);
    return () => window.clearInterval(timer);
  }, [admin, refresh]);
  const inventory = state.data?.inventory;
  return <EngineeringRouteFrame view="credentials" title="Credentials" icon={KeyRound}
    description="Credential names, fingerprints and restrictions, with collection coverage and drift. Values stay in their existing stores."
    actions={admin ? <EngineeringResourceControls state={state} refresh={refresh} cancel={cancel} refreshLabel="Refresh credentials" /> : undefined}>
    {!admin ? <p role="alert">Sign in as an administrator to view credentials.</p> : <>
      <EngineeringResourceGate state={state} refresh={refresh} noun="credential inventory">{() => null}</EngineeringResourceGate>
      {inventory ? <div className="credential-inventory">
        <section className="credentials-summary" aria-label="Collection status" role="status">
          <h2>{inventory.collection_status === "never" ? "No collection received" : inventory.collection_status === "failed" ? "Latest collection failed" : inventory.stale ? "Collection is stale" : "Latest collection received"}</h2>
          <p>{inventory.findings.length} findings. Source coverage: {inventory.coverage}.</p>
          {inventory.last_attempt_at ? <p>Last attempt: {formatTime(inventory.last_attempt_at)}.</p> : null}
          {inventory.snapshot ? <p>Snapshot: {formatTime(inventory.snapshot.generated_at)}.</p> : <p>Inventory and credential presence are unknown until a collection arrives.</p>}
          {inventory.stale && inventory.snapshot ? <p role="alert">No fresh snapshot within 26 hours. The retained inventory may have changed.</p> : null}
          {inventory.collection_status === "failed" ? <p role="alert">The last attempt did not produce an inventory. Any retained rows describe the earlier snapshot.</p> : null}
          {inventory.coverage !== "complete" ? <p>Unreturned credentials in inaccessible or partial sources remain unknown.</p> : null}
        </section>
        <section aria-label="Drift and rotation">
          <h2>Drift and rotation</h2>
          {inventory.findings.length ? <ul className="credentials-findings">{inventory.findings.map((finding, index) => <li key={`${finding.source}:${finding.name}:${finding.kind}:${index}`}>
            <strong>{FINDING_LABELS[finding.kind]}</strong><span>{finding.source}</span><code>{finding.name}</code>
          </li>)}</ul> : <p>{inventory.stale || inventory.collection_status !== "collected" || inventory.coverage !== "complete" ? "No findings recorded. Collection and coverage limits still apply." : "No findings in the named sources."}</p>}
        </section>
        {inventory.snapshot?.observations.map(observation => <section key={observation.source} aria-label={observation.source}>
          <h2>{observation.source}</h2>
          <p>Coverage: <strong>{observation.status}</strong>{observation.reason ? ` (${observation.reason})` : ""}.</p>
          {observation.trace ? <p>Reference: <code>{observation.trace}</code></p> : null}
          {observation.records.length ? <div className="credentials-table-scroll"><table>
            <caption>Observed credential metadata</caption>
            <thead><tr><th>Name or fingerprint</th><th>Restrictions</th><th>Purpose</th><th>Rotation due</th></tr></thead>
            <tbody>{observation.records.map((record, index) => <tr key={`${record.name}:${index}`}>
              <td><code>{record.name}</code></td><td>{record.restrictions.join(", ") || "Not recorded"}</td><td>{record.purpose}</td><td>{record.rotation_due}</td>
            </tr>)}</tbody>
          </table></div> : <p>No credentials observed. Coverage above determines what this establishes.</p>}
        </section>)}
      </div> : null}
    </>}
  </EngineeringRouteFrame>;
}
