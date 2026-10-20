import { useEffect, useState } from "react";
import { readProductEnvironment } from "./api";
import { loadDevFixtures, type DevFixtureMode } from "./dev-fixture-loader";
import type { HostNetworkCapacityObservation } from "./generated/openapi.ts";

export function HostNetworkCapacity({ product, environment, available, fixtureMode, refreshToken }: {
  product: string;
  environment: string;
  available: boolean;
  fixtureMode: DevFixtureMode;
  refreshToken: number;
}) {
  const [observation, setObservation] = useState<HostNetworkCapacityObservation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setObservation(null);
    setError("");
    setLoading(false);
    if (!available && !fixtureMode) return () => controller.abort();
    setLoading(true);
    void (async () => {
      try {
        const capacity = fixtureMode
          ? (await loadDevFixtures()).hostNetworkCapacityForFixture(fixtureMode)
          : (await readProductEnvironment(product, environment, controller.signal, true)).host_network_capacity;
        if (active) setObservation(capacity ?? null);
      } catch {
        if (active) setError("Host network capacity could not be read.");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; controller.abort(); };
  }, [product, environment, available, fixtureMode, refreshToken, refresh]);

  const capacity = observation?.capacity;
  return <div className="host-network-capacity" data-status={loading ? "loading" : observation?.status ?? "unavailable"}>
    <h3>Host network capacity</h3>
    {loading ? <p role="status">Reading host capacity…</p> : <>
      {capacity ? <p><strong>{capacity.free_slots} free of {capacity.total_slots} bridge networks</strong></p> : <p>{error || observation?.reason || "No host capacity observation is available."}</p>}
      {capacity && observation?.reason ? <p role="status">{observation.reason}</p> : null}
      {observation?.server_id ? <p>Host: <span className="mono">{observation.server_id}</span></p> : null}
      {observation ? <p>For a new private network: {observation.required_networks} needed + {observation.safety_margin} spare.</p> : null}
      {observation?.observed_at ? <p>Read at <time dateTime={observation.observed_at}>{new Date(observation.observed_at).toLocaleString()}</time>. This is a snapshot.</p> : null}
    </>}
    {available || fixtureMode ? <button className="secondary-button" type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Refresh host capacity</button> : null}
  </div>;
}
