import { useEffect, useState } from "react";

import { mintClientSignin, readClientSigninUsers, type ClientSigninLookup, type ClientSigninUser } from "./api";
import { safeExternalUrl } from "./url";

export function ClientSignupLinks({ repository, pullRequest, generationId, instance, laneUrl, onOpened }: {
  repository: string;
  pullRequest?: number;
  generationId: string;
  instance: "preview" | "testing";
  laneUrl: string;
  onOpened?: () => void;
}) {
  const [users, setUsers] = useState<ClientSigninUser[]>([]);
  const [runtimeBinding, setRuntimeBinding] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lookup: ClientSigninLookup = { repository, pull_request: pullRequest, generation_id: generationId, instance };
  useEffect(() => {
    const controller = new AbortController();
    let sequence = 0;
    const refresh = () => {
      const request = ++sequence;
      setUsers([]);
      setRuntimeBinding("");
      setError("");
      void readClientSigninUsers({ repository, pull_request: pullRequest, generation_id: generationId, instance }, controller.signal)
        .then(response => {
          if (!controller.signal.aborted && request === sequence) {
            setUsers(response.users);
            setRuntimeBinding(response.runtime_binding);
          }
        }).catch(() => {
          if (!controller.signal.aborted && request === sequence) setUsers([]);
        });
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      controller.abort();
      window.removeEventListener("focus", refresh);
    };
  }, [repository, pullRequest, generationId, instance, laneUrl]);

  const unavailable = () => {
    setUsers([]);
    setError("Sign-in is unavailable. Refresh the review.");
  };
  const signin = async (user: ClientSigninUser) => {
    const destination = window.open("about:blank", "_blank");
    if (!destination) {
      unavailable();
      return;
    }
    destination.opener = null;
    setBusy(true);
    setError("");
    try {
      const response = await mintClientSignin({ ...lookup, xmlid: user.xmlid, reviewed_runtime_binding: runtimeBinding });
      const lane = safeExternalUrl(laneUrl);
      const url = safeExternalUrl(response.sign_in_url);
      if (!lane || !url || url.protocol !== "https:" || url.origin !== lane.origin || url.pathname !== "/web/signup") {
        destination.close();
        unavailable();
        return;
      }
      destination.location.replace(url.toString());
      onOpened?.();
    } catch {
      destination.close();
      unavailable();
    } finally {
      setBusy(false);
    }
  };
  return <>{users.map(user => <button className="button" key={user.xmlid} type="button" disabled={busy || !runtimeBinding} onClick={() => void signin(user)}>Sign in as {user.name}</button>)}{error ? <p role="alert">{error}</p> : null}</>;
}
