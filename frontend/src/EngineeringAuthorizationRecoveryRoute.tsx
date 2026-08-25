import { KeyRound } from "lucide-react";
import { useCallback, useState } from "react";

import {
  enrollAuthorizationRecoveryKey,
  LaunchplaneApiError,
  readAuthorizationRecoveryKeyProof,
  readAuthorizationRecoveryStatus,
  revokeAuthorizationRecoveryKey,
  verifyAuthorizationRecoveryKey,
  type AuthorizationRecoveryBrowserStatusResponse,
  type AuthorizationRecoveryProofResponse,
} from "./api";
import type { DevFixtureMode } from "./dev-fixture-loader";
import {
  EngineeringBoundaryNote,
  EngineeringEmpty,
  EngineeringResourceControls,
  EngineeringResourceGate,
  EngineeringRouteFrame,
} from "./EngineeringRouteUi";
import {
  useEngineeringResource,
  type EngineeringLoadReason,
} from "./engineering-resource";
import { formatTime } from "./format";

export function EngineeringAuthorizationRecoveryRoute({
  fixtureMode,
}: {
  fixtureMode: DevFixtureMode;
}) {
  const loader = useCallback(
    async (
      signal: AbortSignal,
      _reason: EngineeringLoadReason,
    ): Promise<AuthorizationRecoveryBrowserStatusResponse> => {
      if (fixtureMode) {
        return fixture();
      }
      return readAuthorizationRecoveryStatus(signal);
    },
    [fixtureMode],
  );
  const resource = useEngineeringResource(
    loader,
    `authorization-recovery:${fixtureMode}`,
  );
  const [keyId, setKeyId] = useState("");
  const [custodySlot, setCustodySlot] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [signature, setSignature] = useState("");
  const [proof, setProof] = useState<AuthorizationRecoveryProofResponse | null>(
    null,
  );
  const [notice, setNotice] = useState("");

  const mutate = async (operation: () => Promise<unknown>) => {
    setNotice("");
    try {
      await operation();
      setSignature("");
      setPublicKey("");
      setProof(null);
      resource.refresh();
      setNotice(
        "The browser request completed. Refreshing durable readiness evidence.",
      );
    } catch (error) {
      setNotice(
        error instanceof LaunchplaneApiError
          ? `${error.message}${error.traceId ? ` Trace: ${error.traceId}` : ""}`
          : "Authorization recovery request was rejected.",
      );
    }
  };

  return (
    <EngineeringRouteFrame
      actions={
        <EngineeringResourceControls
          cancel={resource.cancel}
          refresh={resource.refresh}
          refreshLabel="Refresh recovery"
          state={resource.state}
        />
      }
      description="Browser-human public-key lifecycle and durable recovery evidence."
      icon={KeyRound}
      title="Engineering Authorization Recovery"
      view="authorization-recovery"
    >
      <EngineeringBoundaryNote title="Hardware signatures are recovery authority">
        This page stores and verifies public hardware-key material only. It
        never shows a stored full key after enrollment and never prepares,
        signs, applies, or executes total-lockout recovery.
      </EngineeringBoundaryNote>
      {notice ? <p className="engineering-resource-note">{notice}</p> : null}
      <EngineeringResourceGate<AuthorizationRecoveryBrowserStatusResponse>
        noun="authorization recovery"
        refresh={resource.refresh}
        state={resource.state}
      >
        {(data) => (
          <div className="engineering-stack">
            <section className="engineering-card">
              <h2>Readiness</h2>
              <p>
                Bootstrap:{" "}
                <strong>{String(data.readiness.bootstrap_status)}</strong> ·
                Independent active custody slots:{" "}
                <strong>
                  {String(data.readiness.independent_custody_slot_count)}
                </strong>{" "}
                · Ready: <strong>{String(data.readiness.ready)}</strong>
              </p>
              <div className="engineering-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Key ID</th>
                      <th>Custody</th>
                      <th>Fingerprint</th>
                      <th>Status</th>
                      <th>Lifecycle</th>
                    </tr>
                  </thead>
                  <tbody>{keyRows(data)}</tbody>
                </table>
              </div>
            </section>
            <section className="engineering-card">
              <h2>Enroll pending hardware key</h2>
              <p>
                Enter an explicit key ID and independent custody slot. The
                browser sends the supplied public key once for enrollment;
                subsequent reads are fingerprint-only.
              </p>
              <div className="engineering-form-grid">
                <label>
                  Key ID
                  <input
                    value={keyId}
                    onChange={(event) => setKeyId(event.target.value)}
                  />
                </label>
                <label>
                  Custody slot
                  <input
                    value={custodySlot}
                    onChange={(event) => setCustodySlot(event.target.value)}
                  />
                </label>
              </div>
              <label>
                Hardware public key
                <textarea
                  value={publicKey}
                  onChange={(event) => setPublicKey(event.target.value)}
                  rows={3}
                />
              </label>
              <button
                disabled={!keyId || !custodySlot || !publicKey}
                onClick={() =>
                  void mutate(() =>
                    enrollAuthorizationRecoveryKey({
                      key_id: keyId,
                      custody_slot: custodySlot,
                      public_key: publicKey,
                    }),
                  )
                }
              >
                Enroll pending key
              </button>
            </section>
            <section className="engineering-card">
              <h2>Proof of possession</h2>
              <p>
                Select a pending key ID, read exact bytes, sign them on the
                hardware key outside Launchplane, then paste the SSHSIG envelope
                or base64 encoding.
              </p>
              <div className="engineering-form-grid">
                <label>
                  Key ID
                  <input
                    value={keyId}
                    onChange={(event) => setKeyId(event.target.value)}
                  />
                </label>
                <button
                  disabled={!keyId}
                  onClick={() =>
                    void readAuthorizationRecoveryKeyProof(keyId)
                      .then(setProof)
                      .catch(() => setNotice("Proof input is unavailable."))
                  }
                >
                  Read exact proof bytes
                </button>
              </div>
              {proof ? (
                <textarea
                  aria-label="Exact proof signing bytes"
                  readOnly
                  rows={6}
                  value={proof.signing_input_text}
                />
              ) : null}
              <label>
                SSHSIG proof
                <textarea
                  value={signature}
                  onChange={(event) => setSignature(event.target.value)}
                  rows={4}
                />
              </label>
              <button
                disabled={!keyId || !signature}
                onClick={() =>
                  void mutate(() =>
                    verifyAuthorizationRecoveryKey(keyId, signature),
                  )
                }
              >
                Verify proof
              </button>
            </section>
            <section className="engineering-card">
              <h2>Revoke key</h2>
              <p>
                Revocation is rejected when it would leave fewer than two
                independent active custody slots.
              </p>
              <button
                disabled={!keyId}
                onClick={() =>
                  void mutate(() => revokeAuthorizationRecoveryKey(keyId))
                }
              >
                Revoke selected key
              </button>
            </section>
            <section className="engineering-card">
              <h2>Recent redacted evidence</h2>
              <h3>Audit</h3>
              {data.audits.length ? (
                <ul>
                  {data.audits.map((audit) => (
                    <li key={audit.audit_id}>
                      {formatTime(audit.recorded_at)} · {audit.event} ·{" "}
                      {audit.status} · {audit.reason_code || "recorded"}
                    </li>
                  ))}
                </ul>
              ) : (
                <EngineeringEmpty
                  detail="No authorization recovery audit records are available yet."
                  icon={KeyRound}
                  title="No recovery audit evidence"
                />
              )}
              <h3>Alerts</h3>
              {data.alerts.length ? (
                <ul>
                  {data.alerts.map((alert) => (
                    <li key={alert.delivery_id}>
                      {formatTime(alert.updated_at)} · {alert.state} ·{" "}
                      {alert.action || "pending"}
                    </li>
                  ))}
                </ul>
              ) : (
                <EngineeringEmpty
                  detail="No durable recovery alert records are available yet."
                  icon={KeyRound}
                  title="No recovery alerts"
                />
              )}
            </section>
          </div>
        )}
      </EngineeringResourceGate>
    </EngineeringRouteFrame>
  );
}

function keyRows(data: AuthorizationRecoveryBrowserStatusResponse) {
  const keys = Array.isArray(data.readiness.keys) ? data.readiness.keys : [];
  return keys.map((value) => {
    const key = value as Record<string, string>;
    return (
      <tr key={key.key_id}>
        <td>{key.key_id}</td>
        <td>{key.custody_slot}</td>
        <td>{key.fingerprint_sha256}</td>
        <td>{key.status}</td>
        <td>
          {formatTime(key.revoked_at || key.activated_at || key.enrolled_at)}
        </td>
      </tr>
    );
  });
}

function fixture(): AuthorizationRecoveryBrowserStatusResponse {
  return {
    status: "ok",
    trace_id: "fixture",
    readiness: {
      bootstrap_status: "pending",
      independent_custody_slot_count: 0,
      ready: false,
      keys: [],
    },
    audits: [],
    alerts: [],
  };
}
