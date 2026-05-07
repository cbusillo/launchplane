import { AlertTriangle, KeyRound, Loader2, ShieldCheck } from "lucide-react";

export function AuthPanel({ checking }: { checking: boolean }) {
  const loginHref = `/auth/github/login?return_to=${encodeURIComponent(window.location.pathname || "/")}`;
  return (
    <section className="auth-panel" aria-labelledby="auth-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Operator access</p>
          <h1 id="auth-heading">Connect to Launchplane</h1>
        </div>
        <ShieldCheck size={22} aria-hidden="true" />
      </div>
      <div className="auth-form">
        <a
          className="button button-primary"
          href={loginHref}
          aria-disabled={checking}
        >
          {checking ? (
            <Loader2 className="spin" size={15} />
          ) : (
            <KeyRound size={15} />
          )}
          <span>{checking ? "Checking session" : "Sign in with GitHub"}</span>
        </a>
      </div>
    </section>
  );
}

export function ApiErrorPanel({
  message,
  traceId,
  onClearToken,
}: {
  message: string;
  traceId: string;
  onClearToken: () => void;
}) {
  return (
    <section className="alert-panel" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <div>
        <strong>{message}</strong>
        {traceId ? <code>{traceId}</code> : null}
      </div>
      <button className="button" type="button" onClick={onClearToken}>
        Sign out
      </button>
    </section>
  );
}
