import { useEffect, useRef, useState } from "react";
import { readProductEnvironment } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { HostNetworkCapacityObservation } from "./generated/openapi.ts";

export function HostNetworkCapacity({ product, environment, available, fixtureMode }: {
  product: string;
  environment: string;
  available: boolean;
  fixtureMode: DevFixtureMode;
}) {
  const [observation, setObservation] = useState<HostNetworkCapacityObservation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const request = useRef<{ key: string; promise: Promise<HostNetworkCapacityObservation | null> } | null>(null);

  useEffect(() => {
    let active = true;
    setObservation(null);
    setError("");
    setLoading(false);
    if (!available && !fixtureMode) { request.current = null; return; }
    setLoading(true);
    void (async () => {
      try {
        const key = `${product}:${environment}:${fixtureMode}:${refresh}`;
        // Reuse the same in-flight read when React replays a mount effect.
        // Aborting fetch cannot cancel the synchronous host program on the server.
        if (request.current?.key !== key) {
          request.current = { key, promise: fixtureMode
            ? loadDevFixtures().then(fixtures => fixtures.hostNetworkCapacityForFixture(fixtureMode))
            : readProductEnvironment(product, environment, undefined, true).then(response => response.host_network_capacity ?? null) };
        }
        const capacity = await request.current.promise;
        if (active) setObservation(capacity ?? null);
      } catch {
        if (active) setError("Host network capacity could not be read.");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [product, environment, available, fixtureMode, refresh]);

  const capacity = observation?.capacity;
  return <div className="host-network-capacity" data-status={loading ? "loading" : observation?.status ?? "unavailable"}>
    <h3>Host network capacity</h3>
    {loading ? <p role="status">Reading host capacity…</p> : <>
      {capacity ? <p><strong><output aria-label="Free bridge networks">{capacity.free_slots}</output> free of <output aria-label="Total bridge networks">{capacity.total_slots}</output> bridge networks</strong></p> : <p>{error || observation?.reason || "No host capacity observation is available."}</p>}
      {capacity && observation?.reason ? <p role="status">{observation.reason}</p> : null}
      {observation?.server_id ? <p>Host: <span className="mono">{observation.server_id}</span></p> : null}
      {observation ? <p>For a new private network: {observation.required_networks} needed + {observation.safety_margin} spare.</p> : null}
      {observation?.observed_at ? <p>Read at <time dateTime={observation.observed_at}>{new Date(observation.observed_at).toLocaleString()}</time>. This is a snapshot.</p> : null}
    </>}
    {available || fixtureMode ? <button className="secondary-button" type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh host capacity</button> : null}
  </div>;
}
