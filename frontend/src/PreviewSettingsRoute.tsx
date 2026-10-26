import { useEffect, useState } from "react";

import { readProductEnvironmentConfigStatus } from "./api";
import { ManagedSecretsChangePanel, RuntimeSettingsChangePanel } from "./ProductConfigForms";
import { InlineError } from "./ProductOps";
import { AppLink, productEnvironmentPath } from "./router";
import type { EnvironmentView } from "./router";
import type { ProductEnvironmentConfigStatus } from "./generated/openapi.ts";

export function PreviewSettingsRoute({ product, view, refreshToken }: {
  product: string;
  view: EnvironmentView;
  refreshToken: number;
}) {
  const [config, setConfig] = useState<ProductEnvironmentConfigStatus | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setConfig(null);
    setError("");
    void readProductEnvironmentConfigStatus(product, "preview", controller.signal)
      .then(response => { if (!controller.signal.aborted) setConfig(response.config_status); })
      .catch(() => { if (!controller.signal.aborted) setError("Preview settings could not be loaded. Refresh to try again."); });
    return () => controller.abort();
  }, [product, refreshToken, revision]);
  useEffect(() => {
    document.querySelector<HTMLElement>("[data-route-heading]")?.focus();
  }, [product, view]);
  const secrets = view === "managed-secrets";
  return <article className="environment-workspace" data-lane="preview">
    <h1 tabIndex={-1} data-route-heading>Preview {secrets ? "secrets" : "settings"}</h1>
    <p>These settings apply to every preview of this product. Preview values take precedence over the testing template. Changes apply on the next preview refresh.</p>
    <nav className="environment-view-nav" aria-label="Preview settings">
      <AppLink to={productEnvironmentPath(product, "preview", "runtime-settings")} aria-current={!secrets ? "page" : undefined}>Runtime settings</AppLink>
      <AppLink to={productEnvironmentPath(product, "preview", "managed-secrets")} aria-current={secrets ? "page" : undefined}>Managed secrets</AppLink>
    </nav>
    {error ? <InlineError message={error} traceId="" /> : !config ? <p role="status">Loading preview settings…</p> : secrets
      ? <ManagedSecretsChangePanel key={`${product}:secrets:${revision}`} config={config} fixtureMode="" onApplied={() => setRevision(value => value + 1)} />
      : <RuntimeSettingsChangePanel key={`${product}:settings:${revision}`} config={config} fixtureMode="" onApplied={() => setRevision(value => value + 1)} />}
  </article>;
}
